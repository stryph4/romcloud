"""Phase-1 Google Drive SaveSync strategy foundation.

Package manifests and generation publishing intentionally arrive in Phase 2.
This strategy proves construction, readiness, owned-root resolution, and safe
object materialization without exposing Drive as a filesystem or loose tree.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from romcloud.core.exceptions import SaveSyncWriteUnavailableError
from romcloud.core.remote_data import RemoteOperationContext
from romcloud.core.save_selection import SaveSelectionPolicy
from romcloud.infrastructure import save_tree
from romcloud.infrastructure.providers.google_drive import (
    GoogleDriveDatasetRoot,
    GoogleDriveObject,
    GoogleDriveProvider,
)
from romcloud.infrastructure.remote_saves import RemoteSaveStore


class GoogleDriveRemoteSaveStore(RemoteSaveStore):
    """Object/package strategy shell; never exposes a filesystem root."""

    def __init__(
        self,
        provider: GoogleDriveProvider,
        connectivity_root: object,
        dataset_root: object,
    ) -> None:
        if not isinstance(dataset_root, GoogleDriveDatasetRoot):
            raise TypeError("Google Drive SaveSync requires an opaque dataset root")
        super().__init__(provider, connectivity_root, dataset_root)
        self._provider = provider
        self._dataset = dataset_root

    def ensure_root(
        self, operation: Optional[RemoteOperationContext] = None
    ) -> GoogleDriveObject:
        return self._provider.ensure_app_root(operation)

    def materialize_object(
        self,
        object_id: str,
        destination: Path,
        *,
        role: Optional[str] = None,
        operation: Optional[RemoteOperationContext] = None,
    ) -> GoogleDriveObject:
        root = self.ensure_root(operation)
        return self._provider.download_owned_object(
            object_id,
            destination,
            role=role,
            parent_id=root.object_id,
            operation=operation,
        )

    def scan(
        self,
        policy: SaveSelectionPolicy,
        *,
        enabled_optional_systems: frozenset[str],
        enabled_optional_groups: frozenset[str],
        operation: Optional[RemoteOperationContext] = None,
    ) -> save_tree.ScanReport:
        del policy, enabled_optional_systems, enabled_optional_groups, operation
        raise SaveSyncWriteUnavailableError(
            "Google Drive package manifests are not implemented in this phase"
        )

    def materialize(
        self,
        relative_path: str,
        destination: Path,
        *,
        operation: Optional[RemoteOperationContext] = None,
    ) -> Path:
        del relative_path, destination, operation
        raise SaveSyncWriteUnavailableError(
            "Google Drive save-package materialization is not implemented in this phase"
        )
