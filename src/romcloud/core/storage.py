"""Storage provider contract shared by ROMCloud services and adapters."""

from __future__ import annotations

from abc import ABC, abstractmethod
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Callable, Iterator, Optional


@dataclass(frozen=True)
class RemoteEntry:
    """A single item (file or directory) found on a storage provider."""

    name: str
    """Bare name with no parent path component."""

    relative_path: str
    """Path relative to the ROM root, e.g. ``ps2/Final Fantasy X.iso``."""

    is_directory: bool
    size_bytes: Optional[int]
    is_symlink: bool = False


@dataclass(frozen=True)
class ProviderCapabilities:
    """What a :class:`StorageProvider` implementation can legitimately do.

    This is a property of the *implementation* (what the code actually
    does), not of a specific configured target's current read/write
    permissions — those are runtime facts established separately (e.g. via
    ``validate_access``/``is_reachable`` and explicit write-probes; see
    :mod:`romcloud.infrastructure.providers.local`). Feature code should
    consult this object (or a runtime probe) instead of branching on
    ``provider_id``.
    """

    has_filesystem_semantics: bool = False
    """True when files appear at real local paths that Direct/Connected
    Mode can symlink into and Batocera's emulators can open directly —
    never true for a provider that only exposes read/download/list
    operations over a network protocol."""

    can_resume_download: bool = False
    """True when an interrupted download can continue from its last
    written byte instead of restarting from zero."""

    supports_durable_transactions: bool = False
    """True when writes through this provider go through a real local (or
    locally-mounted) filesystem, so ROMCloud's atomic-rename + fsync
    transaction/journal engine (see
    :mod:`romcloud.infrastructure.save_transaction`) can provide its
    documented crash/power-loss durability guarantee for a transaction
    *destined* at this provider. This is distinct from
    ``has_filesystem_semantics``: it is specifically about the durability
    contract SaveSync/Library Sync's write path requires, not about
    Direct-mode path-addressability. A provider that only implements a
    remote protocol (e.g. SFTP) must report False here even though it may
    fully support reads/writes/deletes — see the SFTP provider audit notes.
    Write-dependent SaveSync/Library Sync operations must gate on this
    capability of the *destination* provider instance, never on provider
    identity; read-only consumption of such a target remains available."""


_DEFAULT_CAPABILITIES = ProviderCapabilities()


@dataclass(frozen=True)
class StorageAccessResult:
    """Detailed, credential-safe result of a non-destructive storage probe.

    This is the *validated target state* — what a specific configured
    instance/credentials/path combination actually permits right now — as
    opposed to :class:`ProviderCapabilities`, which only describes what the
    provider implementation could theoretically do. Feature gating must use
    this (or an equivalent runtime check), never intrinsic capabilities
    alone: a provider that implements writes does not mean a given
    read-only account/share/path is writable.
    """

    connected: bool
    read_verified: bool
    write_verified: Optional[bool] = None
    cleanup_verified: Optional[bool] = None
    detail: str = ""

    @property
    def ok(self) -> bool:
        required = [self.connected, self.read_verified]
        if self.write_verified is not None:
            required.append(self.write_verified)
        if self.cleanup_verified is not None:
            required.append(self.cleanup_verified)
        return all(required)

    def as_dict(self) -> dict[str, object]:
        return {
            "connected": self.connected,
            "read_verified": self.read_verified,
            "write_verified": self.write_verified,
            "cleanup_verified": self.cleanup_verified,
        }


class StorageProvider(ABC):
    """Abstract base class for all ROMCloud storage backends."""

    @property
    @abstractmethod
    def provider_id(self) -> str:
        """Stable short identifier for this provider type, e.g. ``"local"``."""

    @property
    def capabilities(self) -> ProviderCapabilities:
        """Capabilities of this provider implementation. Default: no
        filesystem semantics, no resumable download — the safe baseline
        for a provider that only implements read/list/download."""
        return _DEFAULT_CAPABILITIES

    @abstractmethod
    def is_reachable(self, root: str) -> bool:
        """Return True if *root* is currently accessible on this provider."""

    def validate_access(self, root: str) -> StorageAccessResult:
        """Return the validated read/write state of *root* right now.

        The default falls back to the binary :meth:`is_reachable` probe.
        Providers that can distinguish connectivity from read/write/cleanup
        (see :mod:`romcloud.infrastructure.providers.local` and
        :mod:`romcloud.infrastructure.providers.sftp`) should override this.
        """
        reachable = self.is_reachable(root)
        return StorageAccessResult(
            reachable,
            reachable,
            detail="" if reachable else "storage location is not accessible",
        )

    @contextmanager
    def catalog_system_scan(self, system: str) -> Iterator[None]:
        """Bound one system's catalog enumeration work.

        Most providers need no lifecycle around a scan. Protocol providers
        may override this narrow scope to reuse a connection, retain
        directory snapshots, and emit operation diagnostics without leaking
        provider-specific behavior into the catalog service.
        """
        del system
        yield

    @abstractmethod
    def list_systems(self, rom_root: str) -> list[str]:
        """Return system directory names found directly under *rom_root*."""

    @abstractmethod
    def list_entries(self, rom_root: str, system: str) -> list[RemoteEntry]:
        """Return immediate entries inside ``rom_root/system/``.

        ``system`` may include a safe nested relative directory. Providers
        must reject root escapes; callers use repeated single-directory
        listings for bounded recursive discovery.
        """

    @abstractmethod
    def get_size(self, path: str) -> Optional[int]:
        """Return the byte size of a file or directory tree, if known."""

    @abstractmethod
    def read_text(self, path: str) -> str:
        """Return the decoded text contents of a small metadata file."""

    @abstractmethod
    def transfer_to(
        self,
        source_path: str,
        dest_path: str,
        on_progress: Optional[Callable[[int, int], None]] = None,
    ) -> None:
        """Copy *source_path* from this provider to local *dest_path*."""

    def resolve_path(self, root: str, relative_path: str) -> str:
        """Resolve a catalog-relative path in this provider's namespace.

        Catalog paths always use POSIX separators, while the provider root is
        opaque to callers. Filesystem-backed providers can use the default;
        protocol providers should override it when their namespace differs.
        """
        relative = PurePosixPath(str(relative_path).replace("\\", "/"))
        if (
            relative.is_absolute()
            or not relative.parts
            or any(part in ("", ".", "..") for part in relative.parts)
        ):
            raise ValueError(f"Unsafe provider-relative path: {relative_path!r}")
        return str(Path(root).joinpath(*relative.parts))

    @contextmanager
    def transfer_session(self) -> Iterator[None]:
        """Bound one logical game's transfer operations.

        Most providers need no lifecycle. Protocol providers may override
        this scope to reuse a connection across tree enumeration and all
        file downloads while still closing it on success, failure, or cancel.
        """
        yield

    def walk(self, root: str) -> list[RemoteEntry]:
        """Recursively enumerate every entry under *root*, relative to it.

        Generic arbitrary-tree read surface for callers (SaveSync/Library
        Sync) that need to scan a dataset not shaped like the ROM
        catalog's one-level system/game layout ``list_entries`` assumes.
        ``relative_path`` on each returned entry is relative to *root*
        itself, not to any provider-wide root. Directories, including empty
        ones, are yielded so callers can reproduce an exact tree. The default
        raises; providers that can be used as remote-data must override it (see
        :mod:`romcloud.infrastructure.providers.local` and
        :mod:`romcloud.infrastructure.providers.sftp`).
        """
        raise NotImplementedError(f"{self.provider_id} does not support tree walking")

    def open_binary(self, path: str):
        """Context manager yielding a readable binary stream for *path*.

        Narrow streaming-read primitive used to hash/compare remote content
        without materializing a local temporary copy first. The default
        raises; see provider overrides.
        """
        raise NotImplementedError(f"{self.provider_id} does not support streamed reads")
