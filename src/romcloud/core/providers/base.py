"""Storage provider abstraction.

All concrete providers implement :class:`StorageProvider`.
The rest of ROMCloud does not know whether data comes from a local
filesystem, SMB share, SFTP server, or any future backend.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Callable, Optional


@dataclass(frozen=True)
class RemoteEntry:
    """A single item (file or directory) found on a storage provider."""

    name: str
    """Bare name — no parent path component."""

    relative_path: str
    """Path relative to the ROM root, e.g. ``ps2/Final Fantasy X.iso``."""

    is_directory: bool
    size_bytes: Optional[int]


class StorageProvider(ABC):
    """Abstract base class for all ROMCloud storage backends.

    Design notes
    ------------
    - Providers are *stateless* between calls — they do not cache
      directory listings internally.
    - All paths passed to/from the provider are provider-relative
      (``source_path``) or local-filesystem absolute (``dest_path``).
    - Progress callbacks receive ``(bytes_transferred: int, total_bytes: int)``.
      ``total_bytes`` may be 0 if the size is unknown.
    """

    @property
    @abstractmethod
    def provider_id(self) -> str:
        """Stable short identifier for this provider type, e.g. ``"local"``."""

    @abstractmethod
    def is_reachable(self, root: str) -> bool:
        """Return True if *root* is currently accessible on this provider."""

    @abstractmethod
    def list_systems(self, rom_root: str) -> list[str]:
        """Return the names of system directories found directly under *rom_root*."""

    @abstractmethod
    def list_entries(self, rom_root: str, system: str) -> list[RemoteEntry]:
        """Return all top-level entries inside ``rom_root/system/``."""

    @abstractmethod
    def get_size(self, path: str) -> Optional[int]:
        """Return the byte size of a file or directory tree at *path*.

        Returns None when the size cannot be determined without a full walk.
        """

    @abstractmethod
    def transfer_to(
        self,
        source_path: str,
        dest_path: str,
        on_progress: Optional[Callable[[int, int], None]] = None,
    ) -> None:
        """Copy *source_path* (on this provider) to *dest_path* (local FS).

        Callers are responsible for creating a staging ``dest_path`` before
        calling this method.  If *dest_path* already exists and has the
        correct size the implementation may skip the transfer (resume).

        Raises :class:`~romcloud.core.exceptions.ProviderNotReachableError`
        if the backend cannot be contacted, or
        :class:`~romcloud.core.exceptions.TransferError` on failure.
        """
