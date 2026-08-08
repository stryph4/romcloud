"""SMB storage provider (stub — not yet implemented).

This module reserves the SMB provider slot in the architecture.
Implement when the local vertical slice is stable and tested.
"""

from __future__ import annotations

from typing import Callable, Optional

from romcloud.core.exceptions import ProviderError
from romcloud.core.providers.base import RemoteEntry, StorageProvider


class SMBProvider(StorageProvider):
    """Storage provider backed by an SMB/CIFS share.

    .. note::
        This is a placeholder.  Requires ``smbprotocol`` (extra: ``smb``).
    """

    PROVIDER_ID = "smb"

    def __init__(
        self,
        server: str,
        share: str,
        username: str,
        password: str,
        port: int = 445,
    ) -> None:
        self._server = server
        self._share = share
        self._username = username
        self._password = password
        self._port = port

    @property
    def provider_id(self) -> str:
        return self.PROVIDER_ID

    def is_reachable(self, root: str) -> bool:
        raise NotImplementedError("SMB provider not yet implemented")

    def list_systems(self, rom_root: str) -> list[str]:
        raise NotImplementedError("SMB provider not yet implemented")

    def list_entries(self, rom_root: str, system: str) -> list[RemoteEntry]:
        raise NotImplementedError("SMB provider not yet implemented")

    def get_size(self, path: str) -> Optional[int]:
        raise NotImplementedError("SMB provider not yet implemented")

    def transfer_to(
        self,
        source_path: str,
        dest_path: str,
        on_progress: Optional[Callable[[int, int], None]] = None,
    ) -> None:
        raise NotImplementedError("SMB provider not yet implemented")
