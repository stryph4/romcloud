"""Local filesystem storage provider.

Handles any source that is already mounted and accessible as a normal
POSIX path — internal drives, USB sticks, NFS mounts, etc.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Callable, Optional

from romcloud.core.exceptions import ProviderError, ProviderNotReachableError, TransferError
from romcloud.core.providers.base import RemoteEntry, StorageProvider

_CHUNK = 1024 * 1024  # 1 MiB read/write chunk


class LocalFilesystemProvider(StorageProvider):
    """Storage provider backed by the local (or mounted) filesystem."""

    PROVIDER_ID = "local"

    @property
    def provider_id(self) -> str:
        return self.PROVIDER_ID

    # ── reachability ──────────────────────────────────────────────────────────

    def is_reachable(self, root: str) -> bool:
        return Path(root).is_dir()

    # ── directory listing ─────────────────────────────────────────────────────

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
        for entry in sorted(system_path.iterdir(), key=lambda e: e.name.lower()):
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

    # ── size ──────────────────────────────────────────────────────────────────

    def get_size(self, path: str) -> Optional[int]:
        return _entry_size(Path(path))

    # ── transfer ──────────────────────────────────────────────────────────────

    def transfer_to(
        self,
        source_path: str,
        dest_path: str,
        on_progress: Optional[Callable[[int, int], None]] = None,
    ) -> None:
        """Copy source_path to dest_path on the local filesystem.

        Resume behaviour:
        - For files: if dest already exists with the same size, skip.
        - For directories: process each file inside and skip already-complete files.
        """
        src = Path(source_path)
        dst = Path(dest_path)

        if not src.exists():
            raise ProviderError(f"Source does not exist: {source_path}")

        dst.parent.mkdir(parents=True, exist_ok=True)

        if src.is_dir():
            _copy_dir(src, dst, on_progress)
        else:
            _copy_file(src, dst, on_progress)


# ── helpers ───────────────────────────────────────────────────────────────────


def _entry_size(path: Path) -> Optional[int]:
    try:
        if path.is_file():
            return path.stat().st_size
        if path.is_dir():
            return sum(f.stat().st_size for f in path.rglob("*") if f.is_file())
    except OSError:
        pass
    return None


def _copy_file(
    src: Path,
    dst: Path,
    on_progress: Optional[Callable[[int, int], None]],
) -> None:
    total = src.stat().st_size

    # Resume: skip if destination already has the correct size.
    if dst.exists() and dst.stat().st_size == total:
        if on_progress:
            on_progress(total, total)
        return

    try:
        copied = 0
        with src.open("rb") as fsrc, dst.open("wb") as fdst:
            while True:
                buf = fsrc.read(_CHUNK)
                if not buf:
                    break
                fdst.write(buf)
                copied += len(buf)
                if on_progress:
                    on_progress(copied, total)
    except OSError as exc:
        raise TransferError(f"Copy failed {src} → {dst}: {exc}") from exc


def _copy_dir(
    src: Path,
    dst: Path,
    on_progress: Optional[Callable[[int, int], None]],
) -> None:
    all_files = [f for f in src.rglob("*") if f.is_file()]
    total = sum(f.stat().st_size for f in all_files)
    copied_so_far = 0

    dst.mkdir(parents=True, exist_ok=True)

    for file in sorted(all_files):
        rel = file.relative_to(src)
        dest_file = dst / rel
        dest_file.parent.mkdir(parents=True, exist_ok=True)

        file_total = file.stat().st_size

        # Resume individual file if it is already complete.
        if dest_file.exists() and dest_file.stat().st_size == file_total:
            copied_so_far += file_total
            if on_progress:
                on_progress(copied_so_far, total)
            continue

        try:
            file_copied = 0
            with file.open("rb") as fsrc, dest_file.open("wb") as fdst:
                while True:
                    buf = fsrc.read(_CHUNK)
                    if not buf:
                        break
                    fdst.write(buf)
                    file_copied += len(buf)
                    copied_so_far += len(buf)
                    if on_progress:
                        on_progress(copied_so_far, total)
        except OSError as exc:
            raise TransferError(f"Copy failed {file} → {dest_file}: {exc}") from exc
