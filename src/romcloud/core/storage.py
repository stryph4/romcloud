"""Storage provider contract shared by ROMCloud services and adapters."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Callable, Optional


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


class StorageProvider(ABC):
    """Abstract base class for all ROMCloud storage backends."""

    @property
    @abstractmethod
    def provider_id(self) -> str:
        """Stable short identifier for this provider type, e.g. ``"local"``."""

    @abstractmethod
    def is_reachable(self, root: str) -> bool:
        """Return True if *root* is currently accessible on this provider."""

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
