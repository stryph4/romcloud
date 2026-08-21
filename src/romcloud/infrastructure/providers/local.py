"""Local filesystem storage provider."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import uuid
from pathlib import Path
from pathlib import PurePosixPath
from typing import Callable, Optional

from romcloud.core.exceptions import (
    MountError,
    ProviderError,
    ProviderNotReachableError,
    TransferError,
)
from romcloud.core.storage import (
    ProviderCapabilities,
    RemoteEntry,
    StorageAccessResult,
    StorageProvider,
)

_CHUNK = 1024 * 1024
_PROBE_CONTENT = "ROMCloud writable storage probe\n"


def _safe_remote_data_path(
    root: str, relative_path: str, *, create_parents: bool = False
) -> Path:
    base = Path(root).resolve()
    relative = PurePosixPath(str(relative_path).replace("\\", "/"))
    if (
        relative.is_absolute()
        or not relative.parts
        or any(part in {"", ".", ".."} for part in relative.parts)
    ):
        raise ProviderError(f"Unsafe remote-data logical key: {relative_path!r}")
    target = base.joinpath(*relative.parts)
    try:
        target.resolve(strict=False).relative_to(base)
    except (OSError, ValueError) as exc:
        raise ProviderError(f"Remote-data key escapes root: {relative_path!r}") from exc
    cursor = base
    for part in relative.parts[:-1]:
        cursor /= part
        if cursor.is_symlink():
            raise ProviderError(f"Refusing remote-data symlink parent: {relative_path}")
        if cursor.exists() and not cursor.is_dir():
            raise ProviderError(f"Remote-data parent is not a directory: {relative_path}")
        if create_parents:
            cursor.mkdir(exist_ok=True)
    return target

# Re-exported for existing callers/tests importing from this module.
__all__ = [
    "StorageAccessResult",
    "LocalFilesystemProvider",
    "WritableMountedFilesystemProvider",
    "WritableLocalFilesystemProvider",
    "probe_directory_access",
    "probe_directory_access_bounded",
]

_LOCAL_CAPABILITIES = ProviderCapabilities(
    has_filesystem_semantics=True,
    can_resume_download=False,
    supports_durable_transactions=True,
    supports_remote_data_writes=True,
)


class LocalFilesystemProvider(StorageProvider):
    """Storage provider backed by the local or mounted filesystem."""

    PROVIDER_ID = "local"

    def __init__(self, *, probe_timeout: float | None = None) -> None:
        # Plain local/USB paths keep the direct fast path. Mounted network
        # filesystems use a short-lived subprocess so a kernel filesystem
        # call can be abandoned without pinning the ROMCloud caller.
        self._probe_timeout = probe_timeout

    @property
    def provider_id(self) -> str:
        return self.PROVIDER_ID

    @property
    def capabilities(self) -> ProviderCapabilities:
        # Real local paths (plain local/USB, or a mounted CIFS share) —
        # Direct/Connected Mode can symlink straight into them.
        return _LOCAL_CAPABILITIES

    def is_reachable(self, root: str) -> bool:
        if self._probe_timeout is None:
            return Path(root).is_dir()
        return self.validate_access(root).ok

    def validate_access(self, root: str) -> StorageAccessResult:
        if self._probe_timeout is not None:
            return probe_directory_access_bounded(
                Path(root), writable=False, timeout=self._probe_timeout
            )
        return probe_directory_access(Path(root), writable=False)

    def list_systems(self, rom_root: str) -> list[str]:
        root = Path(rom_root)
        if not root.is_dir():
            raise ProviderNotReachableError(f"ROM root not accessible: {rom_root}")
        return sorted(
            entry.name
            for entry in root.iterdir()
            if entry.is_dir()
            and not entry.is_symlink()
            and not entry.name.startswith(".")
        )

    def list_entries(self, rom_root: str, system: str) -> list[RemoteEntry]:
        root = Path(rom_root).resolve()
        relative = PurePosixPath(str(system).replace("\\", "/"))
        if (
            relative.is_absolute()
            or not relative.parts
            or any(part in ("", ".", "..") for part in relative.parts)
        ):
            raise ProviderError(f"Unsafe relative source directory: {system!r}")
        system_path = root.joinpath(*relative.parts)
        try:
            system_path.resolve().relative_to(root)
        except (OSError, ValueError) as exc:
            raise ProviderError(
                f"Source directory escapes ROM root: {system!r}"
            ) from exc
        if not system_path.is_dir():
            raise ProviderError(f"System path not found: {system_path}")

        entries: list[RemoteEntry] = []
        for entry in sorted(system_path.iterdir(), key=lambda item: item.name.lower()):
            if entry.name.startswith("."):
                continue
            is_symlink = entry.is_symlink()
            is_directory = entry.is_dir()
            entries.append(
                RemoteEntry(
                    name=entry.name,
                    relative_path=PurePosixPath(relative, entry.name).as_posix(),
                    is_directory=is_directory,
                    # Recursive directory sizing during discovery is O(tree)
                    # work duplicated by the traversal itself. Package sizes
                    # remain unknown until transfer/accounting needs them.
                    size_bytes=(
                        None
                        if is_symlink or is_directory
                        else _entry_size(entry)
                    ),
                    is_symlink=is_symlink,
                )
            )
        return entries

    def get_size(self, path: str) -> Optional[int]:
        return _entry_size(Path(path))

    def read_text(self, path: str) -> str:
        try:
            return Path(path).read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            raise ProviderError(f"Cannot read {path}: {exc}") from exc

    def transfer_to(
        self,
        source_path: str,
        dest_path: str,
        on_progress: Optional[Callable[[int, int], None]] = None,
    ) -> None:
        src = Path(source_path)
        dst = Path(dest_path)
        if not src.exists():
            raise ProviderError(f"Source does not exist: {source_path}")
        dst.parent.mkdir(parents=True, exist_ok=True)
        if src.is_dir():
            _copy_dir(src, dst, on_progress)
        else:
            _copy_file(src, dst, on_progress)

    def walk(self, root: str) -> list[RemoteEntry]:
        base = Path(root)
        if not base.is_dir():
            raise ProviderError(f"Directory tree not found: {root}")
        entries: list[RemoteEntry] = []
        for current, directories, filenames in os.walk(base, followlinks=False):
            current_path = Path(current)
            directories[:] = sorted(
                name for name in directories if not (current_path / name).is_symlink()
            )
            for dirname in directories:
                candidate = current_path / dirname
                entries.append(
                    RemoteEntry(
                        name=dirname,
                        relative_path=candidate.relative_to(base).as_posix(),
                        is_directory=True,
                        size_bytes=None,
                    )
                )
            for filename in sorted(filenames):
                candidate = current_path / filename
                if candidate.is_symlink() or not candidate.is_file():
                    continue
                relative = candidate.relative_to(base).as_posix()
                entries.append(
                    RemoteEntry(
                        name=filename,
                        relative_path=relative,
                        is_directory=False,
                        size_bytes=candidate.stat().st_size,
                    )
                )
        return entries

    def open_binary(self, path: str):
        try:
            return Path(path).open("rb")
        except OSError as exc:
            raise ProviderError(f"Cannot read {path}: {exc}") from exc

    def metadata(
        self, root: str, relative_path: str, *, operation=None
    ) -> Optional[RemoteEntry]:
        if operation is not None:
            operation.check()
        path = _safe_remote_data_path(root, relative_path)
        if not path.exists() and not path.is_symlink():
            return None
        stat = path.lstat()
        is_symlink = path.is_symlink()
        is_directory = path.is_dir() and not is_symlink
        return RemoteEntry(
            name=path.name,
            relative_path=PurePosixPath(relative_path).as_posix(),
            is_directory=is_directory,
            size_bytes=None if is_directory or is_symlink else stat.st_size,
            is_symlink=is_symlink,
            object_id=str(path),
            revision=f"{stat.st_ino}:{stat.st_size}:{stat.st_mtime_ns}",
        )

    def ensure_directory(
        self, root: str, relative_path: str, *, operation=None
    ) -> RemoteEntry:
        if operation is not None:
            operation.check()
        path = _safe_remote_data_path(root, relative_path, create_parents=True)
        if path.is_symlink():
            raise ProviderError(f"Refusing remote-data symlink: {relative_path}")
        path.mkdir(parents=True, exist_ok=True)
        metadata = self.metadata(root, relative_path, operation=operation)
        assert metadata is not None
        return metadata

    def upload_from_local(
        self,
        root: str,
        relative_path: str,
        source_path: str,
        *,
        expected_revision: Optional[str] = None,
        create_only: bool = False,
        on_progress: Optional[Callable[[int, int], None]] = None,
        operation=None,
    ) -> RemoteEntry:
        if operation is not None:
            operation.check()
        destination = _safe_remote_data_path(root, relative_path, create_parents=True)
        current = self.metadata(root, relative_path, operation=operation)
        if create_only and current is not None:
            raise ProviderError(f"Remote-data object already exists: {relative_path}")
        if expected_revision is not None and (
            current is None or current.revision != expected_revision
        ):
            raise ProviderError(f"Remote-data object changed: {relative_path}")
        source = Path(source_path)
        if source.is_symlink() or not source.is_file():
            raise ProviderError(f"Upload source is not a regular file: {source_path}")
        temporary = destination.with_name(f".{destination.name}.upload-{uuid.uuid4().hex}")
        try:
            _copy_file(source, temporary, on_progress)
            os.replace(temporary, destination)
        finally:
            temporary.unlink(missing_ok=True)
        if operation is not None:
            operation.check()
        metadata = self.metadata(root, relative_path, operation=operation)
        assert metadata is not None
        return metadata

    def delete_object(
        self,
        root: str,
        relative_path: str,
        *,
        expected_revision: Optional[str] = None,
        operation=None,
    ) -> None:
        if operation is not None:
            operation.check()
        path = _safe_remote_data_path(root, relative_path)
        current = self.metadata(root, relative_path, operation=operation)
        if current is None:
            return
        if expected_revision is not None and current.revision != expected_revision:
            raise ProviderError(f"Remote-data object changed: {relative_path}")
        if current.is_symlink or current.is_directory:
            raise ProviderError(f"Refusing to delete non-file object: {relative_path}")
        path.unlink()


class WritableMountedFilesystemProvider(LocalFilesystemProvider):
    """Local-filesystem view that is reachable only as a real writable mount.

    Used by SaveSync for SMB deployments. A bare mount-point directory left
    behind after a disconnect must not be mistaken for the remote dataset.
    """

    def __init__(
        self,
        *,
        expected_server: str,
        expected_share: str,
        probe_timeout: float | None = None,
    ) -> None:
        super().__init__(probe_timeout=probe_timeout)
        self._expected_server = expected_server
        self._expected_share = expected_share

    def is_reachable(self, root: str) -> bool:
        return self.validate_access(root).ok

    def validate_access(self, root: str) -> StorageAccessResult:
        from romcloud.infrastructure import mount

        try:
            mounted = mount.is_target_mounted_cifs(
                root,
                server=self._expected_server,
                share=self._expected_share,
                read_only=False,
            )
        except MountError as exc:
            return StorageAccessResult(False, False, detail=f"mount check failed: {exc}")
        if not mounted:
            return StorageAccessResult(
                False,
                False,
                detail="the configured read-write SMB share is not mounted",
            )
        if self._probe_timeout is not None:
            return probe_directory_access_bounded(
                Path(root), writable=True, timeout=self._probe_timeout
            )
        return probe_directory_access(Path(root), writable=True)


class WritableLocalFilesystemProvider(LocalFilesystemProvider):
    """Explicit local/USB remote-data root with a real write probe."""

    def is_reachable(self, root: str) -> bool:
        return self.validate_access(root).ok

    def validate_access(self, root: str) -> StorageAccessResult:
        return probe_directory_access(Path(root), writable=True)


def probe_directory_access(path: Path, *, writable: bool) -> StorageAccessResult:
    """List *path* and optionally verify create/write/read-back/delete.

    The probe uses exclusive creation with a random ROMCloud-owned filename,
    so existing user files can never be overwritten. Cleanup is attempted
    after every successful create and a cleanup failure makes the probe fail
    with an explicit diagnostic.
    """
    if not path.is_dir():
        return StorageAccessResult(False, False, detail="storage location is not accessible")
    try:
        next(path.iterdir(), None)
    except OSError as exc:
        return StorageAccessResult(True, False, detail=f"read access failed: {exc}")
    if not writable:
        return StorageAccessResult(True, True)

    probe = path / f".romcloud-write-probe-{uuid.uuid4().hex}"
    created = False
    write_verified = False
    detail = ""
    try:
        with probe.open("x", encoding="utf-8") as fh:
            created = True
            fh.write(_PROBE_CONTENT)
        write_verified = True
    except OSError as exc:
        detail = f"write access failed: {exc}"

    if write_verified:
        try:
            content = probe.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            detail = f"write verification failed: {exc}"
            write_verified = False
        else:
            if content != _PROBE_CONTENT:
                detail = "write verification failed: probe content did not match"
                write_verified = False

    cleanup_verified: Optional[bool] = None
    if created:
        try:
            probe.unlink()
            cleanup_verified = True
        except OSError as exc:
            cleanup_verified = False
            cleanup_detail = (
                f"cleanup failed for ROMCloud probe {probe.name!r}: {exc}"
            )
            detail = f"{detail}; {cleanup_detail}" if detail else cleanup_detail

    return StorageAccessResult(
        True,
        True,
        write_verified=write_verified,
        cleanup_verified=cleanup_verified,
        detail=detail,
    )


def probe_directory_access_bounded(
    path: Path,
    *,
    writable: bool,
    timeout: float = 5.0,
    popen: Callable[..., "subprocess.Popen[str]"] = subprocess.Popen,
) -> StorageAccessResult:
    """Run a mounted-filesystem probe behind an abandonable process boundary."""
    script = (
        "import json,sys\n"
        "from pathlib import Path\n"
        "from romcloud.infrastructure.providers.local import probe_directory_access\n"
        "r=probe_directory_access(Path(sys.argv[1]), writable=sys.argv[2]=='1')\n"
        "print(json.dumps({'connected':r.connected,'read_verified':r.read_verified,"
        "'write_verified':r.write_verified,'cleanup_verified':r.cleanup_verified,"
        "'detail':r.detail}))\n"
    )
    process: "subprocess.Popen[str] | None" = None
    try:
        process = popen(
            [sys.executable, "-c", script, str(path), "1" if writable else "0"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        stdout, stderr = process.communicate(timeout=max(0.01, timeout))
    except subprocess.TimeoutExpired:
        if process is not None:
            try:
                process.kill()
            except OSError:
                pass
            try:
                process.wait(timeout=0.2)
            except (OSError, subprocess.TimeoutExpired):
                pass
        return StorageAccessResult(
            False,
            False,
            detail=f"storage availability check timed out after {timeout:.1f}s",
        )
    except OSError as exc:
        return StorageAccessResult(False, False, detail=f"storage check failed: {exc}")

    if process.returncode != 0:
        detail = (stderr or "storage probe failed").strip()
        return StorageAccessResult(False, False, detail=detail)
    try:
        payload = json.loads(stdout)
        return StorageAccessResult(
            connected=bool(payload["connected"]),
            read_verified=bool(payload["read_verified"]),
            write_verified=payload.get("write_verified"),
            cleanup_verified=payload.get("cleanup_verified"),
            detail=str(payload.get("detail", "")),
        )
    except (KeyError, TypeError, ValueError):
        return StorageAccessResult(
            False, False, detail="storage availability check returned an invalid response"
        )


def _directory_is_writable(path: Path) -> bool:
    """Backward-compatible boolean wrapper around the detailed probe."""
    return probe_directory_access(path, writable=True).ok


def _entry_size(path: Path) -> Optional[int]:
    try:
        if path.is_file():
            return path.stat().st_size
        if path.is_dir():
            return sum(file.stat().st_size for file in path.rglob("*") if file.is_file())
    except OSError:
        pass
    return None


def _copy_file(
    src: Path,
    dst: Path,
    on_progress: Optional[Callable[[int, int], None]],
) -> None:
    total = src.stat().st_size
    if dst.exists() and dst.stat().st_size == total:
        if on_progress:
            on_progress(total, total)
        return

    try:
        copied = 0
        with src.open("rb") as source, dst.open("wb") as destination:
            while True:
                buffer = source.read(_CHUNK)
                if not buffer:
                    break
                destination.write(buffer)
                copied += len(buffer)
                if on_progress:
                    on_progress(copied, total)
    except OSError as exc:
        raise TransferError(f"Copy failed {src} → {dst}: {exc}") from exc


def _copy_dir(
    src: Path,
    dst: Path,
    on_progress: Optional[Callable[[int, int], None]],
) -> None:
    all_files = [file for file in src.rglob("*") if file.is_file()]
    total = sum(file.stat().st_size for file in all_files)
    copied_so_far = 0
    dst.mkdir(parents=True, exist_ok=True)

    for file in sorted(all_files):
        destination_file = dst / file.relative_to(src)
        destination_file.parent.mkdir(parents=True, exist_ok=True)
        file_total = file.stat().st_size
        if destination_file.exists() and destination_file.stat().st_size == file_total:
            copied_so_far += file_total
            if on_progress:
                on_progress(copied_so_far, total)
            continue

        try:
            with file.open("rb") as source, destination_file.open("wb") as destination:
                while True:
                    buffer = source.read(_CHUNK)
                    if not buffer:
                        break
                    destination.write(buffer)
                    copied_so_far += len(buffer)
                    if on_progress:
                        on_progress(copied_so_far, total)
        except OSError as exc:
            raise TransferError(f"Copy failed {file} → {destination_file}: {exc}") from exc
