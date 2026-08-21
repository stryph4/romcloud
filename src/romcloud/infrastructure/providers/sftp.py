"""SFTP storage provider — a genuine protocol-level :class:`StorageProvider`.

Role-agnostic: the exact same class is used whether SFTP is configured as
the ROM source or as remote-data (see
:mod:`romcloud.bootstrap.container`). Only the *validated target state*
differs per configured instance/credentials/path — see
``probe_writable`` below and :class:`~romcloud.core.storage.StorageAccessResult`.

No local mount/filesystem is involved (unlike SMB, which is a real CIFS
kernel mount read through :class:`~romcloud.infrastructure.providers.local.LocalFilesystemProvider`).
Ordinary operations open a short-lived, bounded SSH/SFTP session. Catalog
refresh reuses one session only for the duration of each system scan and
then closes it; no persistent background connection or reconnect daemon is
maintained.

Host-key verification is always enforced. A caller must supply the
fingerprint trusted for this target (obtained once via
:func:`probe_host_key` during setup's first-connection trust flow);
connecting without one, or to a server presenting a different key, fails
closed with :class:`~romcloud.core.exceptions.ProviderHostKeyUnknownError`
or :class:`~romcloud.core.exceptions.ProviderHostKeyMismatchError`.
"""

from __future__ import annotations

import base64
import contextlib
import contextvars
import hashlib
import posixpath
import socket
import stat as stat_module
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Callable, Iterator, Optional

import paramiko

from romcloud.core.exceptions import (
    ProviderAuthError,
    ProviderError,
    ProviderHostKeyMismatchError,
    ProviderHostKeyUnknownError,
    ProviderNotReachableError,
    ProviderPermissionError,
    TransferError,
)
from romcloud.core.storage import (
    ProviderCapabilities,
    RemoteEntry,
    StorageAccessResult,
    StorageProvider,
)
from romcloud.infrastructure.logging import get_logger

log = get_logger("providers.sftp")

DEFAULT_CONNECT_TIMEOUT = 10.0
"""Bounds DNS/TCP connect, SSH banner exchange, and authentication."""

DEFAULT_OPERATION_TIMEOUT = 30.0
"""Bounds each individual list/stat/read call on the established channel."""

_CONNECT_RETRY_DELAY = 1.0
_PROBE_CONTENT = b"ROMCloud writable storage probe\n"

_SFTP_CAPABILITIES = ProviderCapabilities(
    has_filesystem_semantics=False, can_resume_download=False
)


@dataclass(frozen=True)
class SFTPCatalogScanMetrics:
    """Password/path-safe remote-operation totals for one system scan."""

    system: str
    connections: int
    directory_listings: int
    stat_calls: int
    recursive_directory_visits: int
    file_opens: int
    content_reads: int
    entries_examined: int
    elapsed_seconds: float


@dataclass
class _SFTPCatalogScanState:
    system: str
    client: paramiko.SSHClient
    sftp: paramiko.SFTPClient
    started_at: float
    connections: int = 1
    directory_listings: int = 0
    stat_calls: int = 0
    recursive_directory_visits: int = 0
    file_opens: int = 0
    content_reads: int = 0
    entries_examined: int = 0
    directory_cache: dict[str, tuple[paramiko.SFTPAttributes, ...]] = field(
        default_factory=dict
    )
    attributes_by_path: dict[str, paramiko.SFTPAttributes] = field(
        default_factory=dict
    )

    def finish(self) -> SFTPCatalogScanMetrics:
        return SFTPCatalogScanMetrics(
            system=self.system,
            connections=self.connections,
            directory_listings=self.directory_listings,
            stat_calls=self.stat_calls,
            recursive_directory_visits=self.recursive_directory_visits,
            file_opens=self.file_opens,
            content_reads=self.content_reads,
            entries_examined=self.entries_examined,
            elapsed_seconds=time.perf_counter() - self.started_at,
        )


def fingerprint_of(key: paramiko.PKey) -> str:
    """OpenSSH-style ``SHA256:...`` fingerprint of *key*."""
    digest = hashlib.sha256(key.asbytes()).digest()
    return "SHA256:" + base64.b64encode(digest).decode("ascii").rstrip("=")


def probe_host_key(
    host: str, port: int = 22, *, timeout: float = DEFAULT_CONNECT_TIMEOUT
) -> tuple[str, str]:
    """Observe the server's host key without authenticating.

    Used by setup's first-connection trust flow: obtain and display the key
    type/fingerprint *before* any credential is used, so the user can make
    an informed trust decision. Returns ``(key_type, fingerprint)``.
    """
    sock = socket.create_connection((host, port), timeout=timeout)
    transport = paramiko.Transport(sock)
    try:
        transport.start_client(timeout=timeout)
        key = transport.get_remote_server_key()
        if key is None:
            raise ProviderError(f"{host}:{port} did not present a host key")
        return key.get_name(), fingerprint_of(key)
    except (paramiko.SSHException, socket.error, OSError) as exc:
        raise ProviderNotReachableError(
            f"Could not reach {host}:{port} to read its host key: {exc}"
        ) from exc
    finally:
        transport.close()


class _PinnedHostKeyPolicy(paramiko.MissingHostKeyPolicy):
    """Fail closed unless the presented key matches the trusted fingerprint."""

    def __init__(self, host: str, trusted_fingerprint: Optional[str]) -> None:
        self._host = host
        self._trusted_fingerprint = trusted_fingerprint

    def missing_host_key(self, client, hostname, key) -> None:  # noqa: ANN001
        observed = fingerprint_of(key)
        if self._trusted_fingerprint is None:
            raise ProviderHostKeyUnknownError(
                f"No trusted host key is configured for {self._host}. "
                "Run setup again to review and trust its fingerprint.",
                fingerprint=observed,
                key_type=key.get_name(),
            )
        if observed != self._trusted_fingerprint:
            raise ProviderHostKeyMismatchError(
                f"Host key for {self._host} does not match the trusted "
                f"fingerprint ({self._trusted_fingerprint}); refusing to "
                "connect. Re-run setup only if this change is expected.",
                fingerprint=observed,
                key_type=key.get_name(),
            )
        # Matches the pinned fingerprint — accept for this session only.


class SFTPProvider(StorageProvider):
    """Storage provider backed directly by an SFTP account.

    ``probe_writable`` governs :meth:`validate_access`: a source-role
    instance (the default) never probes writes; a remote-data instance is
    constructed with ``probe_writable=True`` so its write-dependent
    subsystem (SaveSync/Library Sync) can validate the exact target before
    enabling write-dependent operations. This mirrors
    :class:`~romcloud.infrastructure.providers.local.WritableLocalFilesystemProvider`
    without needing a parallel class hierarchy.
    """

    PROVIDER_ID = "sftp"

    def __init__(
        self,
        *,
        host: str,
        username: str,
        port: int = 22,
        password: Optional[str] = None,
        private_key_path: Optional[str] = None,
        private_key_passphrase: Optional[str] = None,
        trusted_host_key_fingerprint: Optional[str] = None,
        probe_writable: bool = False,
        connect_timeout: float = DEFAULT_CONNECT_TIMEOUT,
        operation_timeout: float = DEFAULT_OPERATION_TIMEOUT,
    ) -> None:
        self._host = host
        self._username = username
        self._port = port
        self._password = password
        self._private_key_path = private_key_path
        self._private_key_passphrase = private_key_passphrase
        self._trusted_fingerprint = trusted_host_key_fingerprint
        self._probe_writable = probe_writable
        self._connect_timeout = connect_timeout
        self._operation_timeout = operation_timeout
        self._catalog_scan_state: contextvars.ContextVar[
            Optional[_SFTPCatalogScanState]
        ] = contextvars.ContextVar("romcloud_sftp_catalog_scan", default=None)
        self._transfer_session_state: contextvars.ContextVar[
            Optional[tuple[paramiko.SSHClient, paramiko.SFTPClient]]
        ] = contextvars.ContextVar("romcloud_sftp_transfer_session", default=None)
        self._catalog_scan_metrics: dict[str, SFTPCatalogScanMetrics] = {}

    @property
    def provider_id(self) -> str:
        return self.PROVIDER_ID

    @property
    def capabilities(self) -> ProviderCapabilities:
        return _SFTP_CAPABILITIES

    @property
    def catalog_scan_metrics(self) -> dict[str, SFTPCatalogScanMetrics]:
        """Most recent completed metrics for each system in this provider.

        Values contain only the canonical system ID and aggregate counts;
        credentials and remote paths are never retained.
        """
        return dict(self._catalog_scan_metrics)

    @contextlib.contextmanager
    def catalog_system_scan(self, system: str) -> Iterator[None]:
        """Reuse one bounded SFTP session and directory cache for a system."""
        if self._catalog_scan_state.get() is not None:
            # Catalog does not nest scans today, but treating a nested scope
            # as part of its parent is safer than opening a competing channel.
            yield
            return

        client, sftp = self._connect()
        state = _SFTPCatalogScanState(
            system=system,
            client=client,
            sftp=sftp,
            started_at=time.perf_counter(),
        )
        token = self._catalog_scan_state.set(state)
        try:
            yield
        finally:
            self._catalog_scan_state.reset(token)
            state.client.close()
            metrics = state.finish()
            self._catalog_scan_metrics[system] = metrics
            log.info(
                "SFTP catalog scan system=%s connections=%d listdir_attr=%d "
                "stat=%d directory_visits=%d file_opens=%d content_reads=%d "
                "entries_examined=%d elapsed_ms=%.1f",
                metrics.system,
                metrics.connections,
                metrics.directory_listings,
                metrics.stat_calls,
                metrics.recursive_directory_visits,
                metrics.file_opens,
                metrics.content_reads,
                metrics.entries_examined,
                metrics.elapsed_seconds * 1000,
            )

    # ── connection lifecycle ─────────────────────────────────────────────

    def _connect_once(self) -> tuple[paramiko.SSHClient, paramiko.SFTPClient]:
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(
            _PinnedHostKeyPolicy(self._host, self._trusted_fingerprint)
        )
        try:
            client.connect(
                self._host,
                port=self._port,
                username=self._username,
                password=self._password,
                key_filename=self._private_key_path,
                passphrase=self._private_key_passphrase,
                timeout=self._connect_timeout,
                banner_timeout=self._connect_timeout,
                auth_timeout=self._connect_timeout,
                allow_agent=False,
                look_for_keys=False,
            )
        except paramiko.AuthenticationException as exc:
            client.close()
            raise ProviderAuthError(
                f"{self._username}@{self._host}:{self._port} rejected the "
                "configured SFTP credentials"
            ) from exc
        except (ProviderHostKeyUnknownError, ProviderHostKeyMismatchError):
            client.close()
            raise
        except (paramiko.SSHException, socket.error, OSError) as exc:
            client.close()
            raise ProviderNotReachableError(
                f"Could not reach {self._host}:{self._port}: {exc}"
            ) from exc

        try:
            sftp = client.open_sftp()
        except (paramiko.SSHException, OSError) as exc:
            client.close()
            raise ProviderNotReachableError(
                f"SFTP session negotiation with {self._host}:{self._port} failed: {exc}"
            ) from exc
        sftp.get_channel().settimeout(self._operation_timeout)
        return client, sftp

    def _connect(self) -> tuple[paramiko.SSHClient, paramiko.SFTPClient]:
        """Connect with exactly one bounded retry for transient failures.

        Authentication and host-key failures are permanent for the current
        configuration and are never retried.
        """
        try:
            return self._connect_once()
        except ProviderNotReachableError:
            time.sleep(_CONNECT_RETRY_DELAY)
            return self._connect_once()

    class _Session:
        """Context manager guaranteeing the transport is always closed."""

        def __init__(self, provider: "SFTPProvider") -> None:
            self._provider = provider
            self._client: Optional[paramiko.SSHClient] = None

        def __enter__(self) -> paramiko.SFTPClient:
            self._client, sftp = self._provider._connect()
            return sftp

        def __exit__(self, *exc_info: object) -> None:
            if self._client is not None:
                self._client.close()

    def _session(self) -> "SFTPProvider._Session":
        state = self._catalog_scan_state.get()
        if state is not None:
            return SFTPProvider._BorrowedSession(state.sftp)
        transfer_state = self._transfer_session_state.get()
        if transfer_state is not None:
            return SFTPProvider._BorrowedSession(transfer_state[1])
        return SFTPProvider._Session(self)

    class _BorrowedSession:
        """Expose the active catalog session without closing it per call."""

        def __init__(self, sftp: paramiko.SFTPClient) -> None:
            self._sftp = sftp

        def __enter__(self) -> paramiko.SFTPClient:
            return self._sftp

        def __exit__(self, *exc_info: object) -> None:
            return None

    @contextlib.contextmanager
    def transfer_session(self) -> Iterator[None]:
        """Reuse one bounded SFTP connection for a logical game package."""
        if self._transfer_session_state.get() is not None:
            yield
            return
        client, sftp = self._connect()
        token = self._transfer_session_state.set((client, sftp))
        try:
            yield
        finally:
            self._transfer_session_state.reset(token)
            client.close()

    @staticmethod
    def _cache_key(path: str) -> str:
        return posixpath.normpath(str(path).replace("\\", "/"))

    def _listdir_attr(
        self, sftp: paramiko.SFTPClient, path: str
    ) -> tuple[paramiko.SFTPAttributes, ...]:
        state = self._catalog_scan_state.get()
        key = self._cache_key(path)
        if state is not None and key in state.directory_cache:
            return state.directory_cache[key]

        if state is not None:
            state.directory_listings += 1
            state.recursive_directory_visits += 1
        entries = tuple(sftp.listdir_attr(path))
        if state is not None:
            state.entries_examined += len(entries)
            state.directory_cache[key] = entries
            for entry in entries:
                state.attributes_by_path[
                    self._cache_key(posixpath.join(key, entry.filename))
                ] = entry
        return entries

    def _stat(
        self, sftp: paramiko.SFTPClient, path: str
    ) -> paramiko.SFTPAttributes:
        state = self._catalog_scan_state.get()
        key = self._cache_key(path)
        if state is not None:
            cached = state.attributes_by_path.get(key)
            if cached is not None:
                return cached
            state.stat_calls += 1
        return sftp.stat(path)

    def _record_file_open(self) -> None:
        state = self._catalog_scan_state.get()
        if state is not None:
            state.file_opens += 1

    def _record_content_read(self) -> None:
        state = self._catalog_scan_state.get()
        if state is not None:
            state.content_reads += 1

    # ── path safety ───────────────────────────────────────────────────────

    @staticmethod
    def _safe_join(root: str, relative: str) -> str:
        rel = PurePosixPath(str(relative).replace("\\", "/"))
        if rel.is_absolute() or any(part in ("", ".", "..") for part in rel.parts):
            raise ProviderError(f"Unsafe relative SFTP path: {relative!r}")
        base = root.rstrip("/") or "/"
        return posixpath.join(base, *rel.parts) if rel.parts else root

    # ── StorageProvider contract ──────────────────────────────────────────

    def is_reachable(self, root: str) -> bool:
        """Bounded existence/connectivity probe.

        Only collapses genuine unreachability/missing-path outcomes to
        ``False`` — authentication, permission, and host-key trust failures
        are distinct, actionable conditions and always propagate instead of
        being silently reported as "unreachable" (see
        :class:`~romcloud.core.exceptions.ProviderAuthError`,
        :class:`~romcloud.core.exceptions.ProviderPermissionError`,
        :class:`~romcloud.core.exceptions.ProviderHostKeyUnknownError`,
        :class:`~romcloud.core.exceptions.ProviderHostKeyMismatchError`).
        """
        try:
            with self._session() as sftp:
                self._stat(sftp, root)
            return True
        except ProviderNotReachableError:
            return False
        except OSError:
            return False

    def validate_access(self, root: str) -> StorageAccessResult:
        try:
            with self._session() as sftp:
                try:
                    sftp.listdir(root)
                except FileNotFoundError:
                    return StorageAccessResult(
                        True, False, detail=f"configured remote path does not exist: {root}"
                    )
                except PermissionError:
                    return StorageAccessResult(
                        True, False, detail="read access denied to the configured remote path"
                    )
                if not self._probe_writable:
                    return StorageAccessResult(True, True)
                return self._probe_write(sftp, root)
        except ProviderHostKeyUnknownError as exc:
            return StorageAccessResult(False, False, detail=str(exc))
        except ProviderHostKeyMismatchError as exc:
            return StorageAccessResult(False, False, detail=str(exc))
        except ProviderAuthError as exc:
            return StorageAccessResult(False, False, detail=str(exc))
        except ProviderNotReachableError as exc:
            return StorageAccessResult(False, False, detail=str(exc))

    def _probe_write(self, sftp: paramiko.SFTPClient, root: str) -> StorageAccessResult:
        """Create/write/flush/stat/read-back/delete a ROMCloud-owned probe
        object inside *root* — never touches any pre-existing file."""
        probe = posixpath.join(root.rstrip("/"), f".romcloud-write-probe-{uuid.uuid4().hex}")
        created = False
        write_verified = False
        detail = ""
        try:
            # "x" alone only requests SFTP_FLAG_CREATE|EXCL without WRITE per
            # paramiko's mode parsing; "w" is required to also request write.
            with sftp.open(probe, "wxb") as fh:
                created = True
                fh.write(_PROBE_CONTENT)
                fh.flush()
            write_verified = True
        except PermissionError:
            detail = "write access denied to the configured remote path"
        except OSError as exc:
            detail = f"write access failed: {exc}"

        if write_verified:
            try:
                with sftp.open(probe, "rb") as fh:
                    content = fh.read()
                if content != _PROBE_CONTENT:
                    write_verified = False
                    detail = "write verification failed: probe content did not match"
            except OSError as exc:
                write_verified = False
                detail = f"write verification failed: {exc}"

        cleanup_verified: Optional[bool] = None
        if created:
            try:
                sftp.remove(probe)
                cleanup_verified = True
            except OSError as exc:
                cleanup_verified = False
                cleanup_detail = f"cleanup failed for ROMCloud probe {probe!r}: {exc}"
                detail = f"{detail}; {cleanup_detail}" if detail else cleanup_detail

        return StorageAccessResult(
            True,
            True,
            write_verified=write_verified,
            cleanup_verified=cleanup_verified,
            detail=detail,
        )

    def list_systems(self, rom_root: str) -> list[str]:
        with self._session() as sftp:
            try:
                entries = self._listdir_attr(sftp, rom_root)
            except FileNotFoundError as exc:
                raise ProviderNotReachableError(
                    f"ROM root not accessible: {rom_root}"
                ) from exc
            except PermissionError as exc:
                raise ProviderPermissionError(
                    f"Permission denied listing ROM root: {rom_root}"
                ) from exc
            return sorted(
                entry.filename
                for entry in entries
                if _is_dir(entry) and not _is_symlink(entry) and not entry.filename.startswith(".")
            )

    def list_entries(self, rom_root: str, system: str) -> list[RemoteEntry]:
        system_path = self._safe_join(rom_root, system)
        relative = PurePosixPath(str(system).replace("\\", "/"))
        with self._session() as sftp:
            try:
                raw_entries = self._listdir_attr(sftp, system_path)
            except FileNotFoundError as exc:
                raise ProviderError(f"System path not found: {system_path}") from exc
            except PermissionError as exc:
                raise ProviderPermissionError(
                    f"Permission denied listing: {system_path}"
                ) from exc

            entries: list[RemoteEntry] = []
            for entry in sorted(raw_entries, key=lambda item: item.filename.lower()):
                if entry.filename.startswith("."):
                    continue
                is_symlink = _is_symlink(entry)
                is_directory = _is_dir(entry)
                entries.append(
                    RemoteEntry(
                        name=entry.filename,
                        relative_path=PurePosixPath(relative, entry.filename).as_posix(),
                        is_directory=is_directory,
                        size_bytes=(
                            None if is_symlink or is_directory else entry.st_size
                        ),
                        is_symlink=is_symlink,
                    )
                )
            return entries

    def get_size(self, path: str) -> Optional[int]:
        try:
            with self._session() as sftp:
                return self._size_of(sftp, path)
        except (ProviderError, OSError):
            return None

    def _size_of(self, sftp: paramiko.SFTPClient, path: str) -> Optional[int]:
        try:
            attr = self._stat(sftp, path)
        except OSError:
            return None
        if attr.st_mode is not None and _is_dir(attr):
            total = 0
            for entry in self._listdir_attr(sftp, path):
                child_size = self._size_of_attr(
                    sftp, posixpath.join(path, entry.filename), entry
                )
                total += child_size or 0
            return total
        return attr.st_size

    def _size_of_attr(
        self,
        sftp: paramiko.SFTPClient,
        path: str,
        attr: paramiko.SFTPAttributes,
    ) -> Optional[int]:
        """Size an entry using metadata already returned by listdir_attr."""
        if attr.st_mode is not None and _is_dir(attr):
            total = 0
            for child in self._listdir_attr(sftp, path):
                child_size = self._size_of_attr(
                    sftp, posixpath.join(path, child.filename), child
                )
                total += child_size or 0
            return total
        return attr.st_size

    def read_text(self, path: str) -> str:
        try:
            with self._session() as sftp:
                self._record_file_open()
                with sftp.open(path, "r") as fh:
                    self._record_content_read()
                    raw = fh.read()
        except FileNotFoundError as exc:
            raise ProviderError(f"Cannot read {path}: not found") from exc
        except PermissionError as exc:
            raise ProviderPermissionError(f"Cannot read {path}: permission denied") from exc
        except OSError as exc:
            raise ProviderError(f"Cannot read {path}: {exc}") from exc
        return raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else raw

    def transfer_to(
        self,
        source_path: str,
        dest_path: str,
        on_progress: Optional[Callable[[int, int], None]] = None,
    ) -> None:
        dst = Path(dest_path)
        dst.parent.mkdir(parents=True, exist_ok=True)
        try:
            with self._session() as sftp:
                sftp.get(source_path, str(dst), callback=on_progress)
        except FileNotFoundError as exc:
            raise TransferError(f"Source does not exist: {source_path}") from exc
        except PermissionError as exc:
            raise TransferError(
                f"Permission denied reading {source_path}: {exc}"
            ) from exc
        except (paramiko.SSHException, OSError) as exc:
            raise TransferError(f"Copy failed {source_path} → {dest_path}: {exc}") from exc

    def resolve_path(self, root: str, relative_path: str) -> str:
        """Resolve a catalog path without applying local filesystem rules."""
        return self._safe_join(root, relative_path)

    def walk(self, root: str) -> list[RemoteEntry]:
        entries: list[RemoteEntry] = []
        with self._session() as sftp:
            try:
                self._stat(sftp, root)
            except PermissionError as exc:
                raise ProviderPermissionError(
                    f"Permission denied reading directory tree: {root}"
                ) from exc
            except OSError as exc:
                raise ProviderError(f"Directory tree not found: {root}") from exc
            stack = [""]
            while stack:
                relative_dir = stack.pop()
                current = (
                    posixpath.join(root.rstrip("/"), relative_dir) if relative_dir else root
                )
                try:
                    children = self._listdir_attr(sftp, current)
                except PermissionError as exc:
                    raise ProviderPermissionError(
                        f"Permission denied listing: {current}"
                    ) from exc
                except OSError as exc:
                    raise ProviderError(f"Cannot list {current}: {exc}") from exc
                for child in children:
                    if _is_symlink(child):
                        continue
                    child_relative = (
                        posixpath.join(relative_dir, child.filename)
                        if relative_dir
                        else child.filename
                    )
                    if _is_dir(child):
                        entries.append(
                            RemoteEntry(
                                name=child.filename,
                                relative_path=child_relative,
                                is_directory=True,
                                size_bytes=None,
                            )
                        )
                        stack.append(child_relative)
                        continue
                    entries.append(
                        RemoteEntry(
                            name=child.filename,
                            relative_path=child_relative,
                            is_directory=False,
                            size_bytes=child.st_size,
                        )
                    )
        return entries

    @contextlib.contextmanager
    def open_binary(self, path: str):
        with self._session() as sftp:
            try:
                self._record_file_open()
                with sftp.open(path, "rb") as fh:
                    yield fh
            except FileNotFoundError as exc:
                raise ProviderError(f"Cannot read {path}: not found") from exc
            except PermissionError as exc:
                raise ProviderPermissionError(
                    f"Cannot read {path}: permission denied"
                ) from exc
            except OSError as exc:
                raise ProviderError(f"Cannot read {path}: {exc}") from exc


def _is_dir(attr: paramiko.SFTPAttributes) -> bool:
    return attr.st_mode is not None and stat_module.S_ISDIR(attr.st_mode)


def _is_symlink(attr: paramiko.SFTPAttributes) -> bool:
    return attr.st_mode is not None and stat_module.S_ISLNK(attr.st_mode)
