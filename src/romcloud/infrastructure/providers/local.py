"""Local filesystem storage provider."""

from __future__ import annotations

from pathlib import Path
from typing import Callable, Optional

from romcloud.core.exceptions import (
    MountError,
    ProviderError,
    ProviderNotReachableError,
    TransferError,
)
from romcloud.core.storage import RemoteEntry, StorageProvider

_CHUNK = 1024 * 1024


class LocalFilesystemProvider(StorageProvider):
    """Storage provider backed by the local or mounted filesystem."""

    PROVIDER_ID = "local"

    @property
    def provider_id(self) -> str:
        return self.PROVIDER_ID

    def is_reachable(self, root: str) -> bool:
        return Path(root).is_dir()

    def list_systems(self, rom_root: str) -> list[str]:
        root = Path(rom_root)
        if not root.is_dir():
            raise ProviderNotReachableError(f"ROM root not accessible: {rom_root}")
        return sorted(
            entry.name
            for entry in root.iterdir()
            if entry.is_dir() and not entry.name.startswith(".")
        )

    def list_entries(self, rom_root: str, system: str) -> list[RemoteEntry]:
        system_path = Path(rom_root) / system
        if not system_path.is_dir():
            raise ProviderError(f"System path not found: {system_path}")

        entries: list[RemoteEntry] = []
        for entry in sorted(system_path.iterdir(), key=lambda item: item.name.lower()):
            if entry.name.startswith("."):
                continue
            entries.append(
                RemoteEntry(
                    name=entry.name,
                    relative_path=str(Path(system) / entry.name),
                    is_directory=entry.is_dir(),
                    size_bytes=_entry_size(entry),
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


class WritableMountedFilesystemProvider(LocalFilesystemProvider):
    """Local-filesystem view that is reachable only as a real writable mount.

    Used by SaveSync for SMB deployments. A bare mount-point directory left
    behind after a disconnect must not be mistaken for the remote dataset.
    """

    def is_reachable(self, root: str) -> bool:
        if not super().is_reachable(root):
            return False
        from romcloud.infrastructure import mount

        try:
            return mount.is_target_mounted_writable(root)
        except MountError:
            return False


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
