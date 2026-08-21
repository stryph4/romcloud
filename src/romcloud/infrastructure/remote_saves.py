"""Remote SaveSync storage strategies.

The filesystem strategy deliberately retains ROMCloud's existing Path-based
transaction and journal machinery.  Protocol/object strategies expose only
logical manifests and local materialization; they never turn opaque roots into
local paths.  A future package-backed provider can implement ``RemoteSaveStore``
without exposing one remote object per save file.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import Optional

from romcloud.core.remote_data import (
    LooseObjectRemoteDataProvider,
    RemoteDataCapabilities,
    RemoteDataProvider,
    RemoteOperationContext,
    validate_logical_key,
)
from romcloud.core.save_selection import SaveSelectionPolicy
from romcloud.core.storage import StorageAccessResult
from romcloud.infrastructure import save_tree, savesync_journal


class RemoteSaveStore(ABC):
    """Logical remote SaveSync dataset independent of provider root syntax."""

    def __init__(
        self,
        provider: RemoteDataProvider,
        connectivity_root: object,
        dataset_root: object,
    ) -> None:
        self._provider = provider
        self._connectivity_root = connectivity_root
        self._dataset_root = dataset_root
        self._capabilities = RemoteDataCapabilities.from_storage(provider.capabilities)

    @property
    def provider_id(self) -> str:
        return self._provider.provider_id

    @property
    def capabilities(self) -> RemoteDataCapabilities:
        return self._capabilities

    @property
    def display_root(self) -> str:
        return str(self._connectivity_root)

    def validate_access(self) -> StorageAccessResult:
        return self._provider.validate_access(self._connectivity_root)

    def is_readable(self) -> bool:
        return self.validate_access().readable

    def is_writable(self, access: Optional[StorageAccessResult] = None) -> bool:
        checked = access or self.validate_access()
        if checked.write_verified is None:
            # Compatibility for providers whose legacy probe only reported
            # reachability. Production writable filesystem providers perform
            # an explicit write/read-back/cleanup probe.
            return checked.readable and self.capabilities.filesystem_transactions
        return checked.writable

    @abstractmethod
    def scan(
        self,
        policy: SaveSelectionPolicy,
        *,
        enabled_optional_systems: frozenset[str],
        enabled_optional_groups: frozenset[str],
        operation: Optional[RemoteOperationContext] = None,
    ) -> save_tree.ScanReport:
        """Return the provider's current logical save-artifact manifest."""

    @abstractmethod
    def materialize(
        self,
        relative_path: str,
        destination: Path,
        *,
        operation: Optional[RemoteOperationContext] = None,
    ) -> Path:
        """Place a verified remote artifact in local scratch and return it."""

    @property
    def filesystem_transaction_root(self) -> Optional[Path]:
        return None

    @property
    def filesystem_journal_path(self) -> Optional[Path]:
        return None

    def recover_filesystem_dataset(self) -> None:
        """Recover provider-owned transaction artifacts when applicable."""


class FilesystemRemoteSaveStore(RemoteSaveStore):
    """Adapter preserving existing mounted/local filesystem semantics."""

    def __init__(
        self,
        provider: RemoteDataProvider,
        connectivity_root: object,
        dataset_root: str | Path,
    ) -> None:
        super().__init__(provider, connectivity_root, dataset_root)
        self._root = Path(dataset_root)

    def scan(
        self,
        policy: SaveSelectionPolicy,
        *,
        enabled_optional_systems: frozenset[str],
        enabled_optional_groups: frozenset[str],
        operation: Optional[RemoteOperationContext] = None,
    ) -> save_tree.ScanReport:
        if operation is not None:
            operation.check()
        return save_tree.scan_tree_report(
            self._root,
            policy,
            enabled_optional_systems=enabled_optional_systems,
            enabled_optional_groups=enabled_optional_groups,
        )

    def materialize(
        self,
        relative_path: str,
        destination: Path,
        *,
        operation: Optional[RemoteOperationContext] = None,
    ) -> Path:
        relative_path = validate_logical_key(relative_path)
        if operation is not None:
            operation.check()
        # SaveSelectionPolicy has already validated the canonical key. The
        # transaction engine repeats containment/symlink checks before use.
        return self._root.joinpath(*relative_path.split("/"))

    @property
    def filesystem_transaction_root(self) -> Optional[Path]:
        return self._root

    @property
    def filesystem_journal_path(self) -> Optional[Path]:
        return savesync_journal.default_journal_path(self._root)

    def recover_filesystem_dataset(self) -> None:
        save_tree.recover_interrupted_commit(self._root)


class ProviderRemoteSaveStore(RemoteSaveStore):
    """Read/materialization adapter for a non-filesystem provider.

    It intentionally advertises no mutation strategy.  A future object-backed
    implementation should subclass ``RemoteSaveStore`` directly and may
    expose packages/generations rather than loose provider files.
    """

    def __init__(
        self,
        provider: LooseObjectRemoteDataProvider,
        connectivity_root: object,
        dataset_root: object,
    ) -> None:
        super().__init__(provider, connectivity_root, dataset_root)
        self._provider = provider

    def scan(
        self,
        policy: SaveSelectionPolicy,
        *,
        enabled_optional_systems: frozenset[str],
        enabled_optional_groups: frozenset[str],
        operation: Optional[RemoteOperationContext] = None,
    ) -> save_tree.ScanReport:
        return save_tree.scan_provider_tree_report(
            self._provider,
            self._dataset_root,
            policy,
            enabled_optional_systems=enabled_optional_systems,
            enabled_optional_groups=enabled_optional_groups,
            operation=operation,
        )

    def materialize(
        self,
        relative_path: str,
        destination: Path,
        *,
        operation: Optional[RemoteOperationContext] = None,
    ) -> Path:
        relative_path = validate_logical_key(relative_path)
        if operation is not None:
            operation.check()
        destination.parent.mkdir(parents=True, exist_ok=True)
        self._provider.download_to_local(
            self._dataset_root,
            relative_path,
            str(destination),
            operation=operation,
        )
        if operation is not None:
            operation.check()
        return destination


def build_remote_save_store(
    provider: Optional[RemoteDataProvider],
    *,
    connectivity_root: object | None,
    dataset_root: object | None,
) -> Optional[RemoteSaveStore]:
    if provider is None or connectivity_root is None or dataset_root is None:
        return None
    if provider.capabilities.has_filesystem_semantics:
        if not isinstance(dataset_root, (str, Path)):
            raise TypeError("Filesystem remote-data roots must be path-like")
        return FilesystemRemoteSaveStore(provider, connectivity_root, dataset_root)
    if not isinstance(provider, LooseObjectRemoteDataProvider):
        raise TypeError(
            "Non-filesystem remote data requires a provider-specific save-store "
            "strategy or the loose-object provider role"
        )
    return ProviderRemoteSaveStore(provider, connectivity_root, dataset_root)
