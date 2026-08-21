"""Role-specific contract for ROMCloud-owned remote data.

Unlike :mod:`romcloud.core.storage`'s ROM catalog surface, this contract says
nothing about systems, ROMs, or provider path syntax.  A root is intentionally
opaque and logical keys are interpreted by the implementation.  SaveSync uses
a higher-level remote save-store strategy built on this role so a future
provider may represent a logical save set as an object/package rather than as
loose files.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Callable, Optional, Protocol, runtime_checkable

from romcloud.core.cancellation import TransferCancellationToken
from romcloud.core.exceptions import ProviderNotReachableError
from romcloud.core.storage import ProviderCapabilities, RemoteEntry, StorageAccessResult


def validate_logical_key(key: str, *, allow_empty: bool = False) -> str:
    """Return a canonical provider-neutral key or reject namespace escape."""
    raw = str(key).replace("\\", "/")
    parsed = PurePosixPath(raw)
    if (
        (not raw and not allow_empty)
        or parsed.is_absolute()
        or any(part in {"", ".", ".."} for part in parsed.parts)
        or (parsed.as_posix() != raw and raw != "")
    ):
        raise ValueError(f"Unsafe remote-data logical key: {key!r}")
    return parsed.as_posix() if raw else ""


@dataclass(frozen=True)
class RemoteDataCapabilities:
    """Storage guarantees relevant to ROMCloud-owned synchronized data."""

    filesystem_transactions: bool = False
    filesystem_journal: bool = False
    resumable_download: bool = False
    resumable_upload: bool = False
    conditional_revisions: bool = False
    object_generations: bool = False
    logical_writes: bool = False

    @classmethod
    def from_storage(cls, capabilities: ProviderCapabilities) -> "RemoteDataCapabilities":
        durable = capabilities.supports_durable_transactions
        filesystem = capabilities.has_filesystem_semantics
        return cls(
            filesystem_transactions=durable,
            filesystem_journal=filesystem and durable,
            resumable_download=capabilities.can_resume_download,
            resumable_upload=capabilities.can_resume_upload,
            conditional_revisions=capabilities.supports_conditional_revisions,
            object_generations=capabilities.supports_object_generations,
            logical_writes=capabilities.supports_remote_data_writes,
        )


@dataclass(frozen=True)
class RemoteOperationContext:
    """Cooperative cancellation/deadline passed across the remote boundary."""

    deadline: Optional[float] = None
    cancellation: Optional[TransferCancellationToken] = None
    clock: Callable[[], float] = time.monotonic

    def check(self) -> None:
        if self.cancellation is not None:
            self.cancellation.raise_if_cancelled()
        if self.deadline is not None and self.clock() >= self.deadline:
            raise ProviderNotReachableError("Remote-data operation deadline expired")


@runtime_checkable
class RemoteDataProvider(Protocol):
    """Small common role used by every remote-data strategy.

    Current ``StorageProvider`` implementations satisfy this structurally.
    A future package-backed provider can implement only this identity,
    readiness, and namespace surface; it need not expose individual save
    files or pretend to be a ROM source.
    """

    @property
    def provider_id(self) -> str: ...

    @property
    def capabilities(self) -> ProviderCapabilities: ...

    def is_reachable(self, root: object) -> bool: ...

    def validate_access(self, root: object) -> StorageAccessResult: ...

    def remote_data_root(self, root: object, namespace: str) -> object: ...


@runtime_checkable
class LooseObjectRemoteDataProvider(RemoteDataProvider, Protocol):
    """Optional role for providers that expose path-like logical objects.

    This supports today's filesystem/SFTP-style layout adapter. Package or
    snapshot providers should instead supply their own ``RemoteSaveStore``
    strategy and are deliberately not required to implement this role.
    """

    def list_children(
        self,
        root: object,
        relative_directory: str = "",
        *,
        operation: Optional[RemoteOperationContext] = None,
    ) -> list[RemoteEntry]: ...

    def download_to_local(
        self,
        root: object,
        relative_path: str,
        destination: str,
        on_progress: Optional[Callable[[int, int], None]] = None,
        *,
        operation: Optional[RemoteOperationContext] = None,
    ) -> None: ...

    def open_binary(self, path: object): ...

    def resolve_path(self, root: object, relative_path: str) -> object: ...

    def metadata(
        self,
        root: object,
        relative_path: str,
        *,
        operation: Optional[RemoteOperationContext] = None,
    ) -> Optional[RemoteEntry]: ...

    def ensure_directory(
        self,
        root: object,
        relative_path: str,
        *,
        operation: Optional[RemoteOperationContext] = None,
    ) -> RemoteEntry: ...

    def upload_from_local(
        self,
        root: object,
        relative_path: str,
        source_path: str,
        *,
        expected_revision: Optional[str] = None,
        create_only: bool = False,
        on_progress: Optional[Callable[[int, int], None]] = None,
        operation: Optional[RemoteOperationContext] = None,
    ) -> RemoteEntry: ...

    def delete_object(
        self,
        root: object,
        relative_path: str,
        *,
        expected_revision: Optional[str] = None,
        operation: Optional[RemoteOperationContext] = None,
    ) -> None: ...
