from __future__ import annotations

import hashlib
import io
from pathlib import Path

import pytest

from romcloud.core.cancellation import TransferCancellationToken
from romcloud.core.exceptions import TransferCancelledError
from romcloud.core.remote_data import (
    LooseObjectRemoteDataProvider,
    RemoteDataProvider,
    RemoteOperationContext,
    validate_logical_key,
)
from romcloud.core.save_selection import DEFAULT_SAVE_SELECTION_POLICY
from romcloud.core.storage import ProviderCapabilities, RemoteEntry, StorageAccessResult
from romcloud.infrastructure.remote_saves import ProviderRemoteSaveStore
from romcloud.infrastructure.providers.local import WritableLocalFilesystemProvider
from romcloud.services.saves import SaveSyncService


class _OpaqueRoot:
    pass


class _ObjectProvider:
    provider_id = "object-test"

    def __init__(self, *, writable: bool = False) -> None:
        self.root = _OpaqueRoot()
        self.files = {
            "nes/game.srm": b"save",
            "unknown/private.bin": b"private",
        }
        self.listed: list[tuple[object, str]] = []
        self.downloaded: list[tuple[object, str]] = []
        self._writable = writable

    @property
    def capabilities(self):
        return ProviderCapabilities()

    def is_reachable(self, root: object) -> bool:
        return root is self.root

    def validate_access(self, root: object) -> StorageAccessResult:
        assert root is self.root
        return StorageAccessResult(
            True,
            True,
            write_verified=self._writable,
            cleanup_verified=self._writable,
            detail="read-only" if not self._writable else "",
        )

    def list_children(
        self, root: object, relative_directory: str = "", *, operation=None
    ):
        assert root is self.root
        if operation is not None:
            operation.check()
        self.listed.append((root, relative_directory))
        prefix = f"{relative_directory}/" if relative_directory else ""
        names: dict[str, bool] = {}
        for key in self.files:
            if not key.startswith(prefix):
                continue
            remainder = key[len(prefix) :]
            name, separator, _ = remainder.partition("/")
            names[name] = names.get(name, False) or bool(separator)
        entries = [
            RemoteEntry(
                name=name,
                relative_path=f"{prefix}{name}",
                is_directory=is_directory,
                size_bytes=(None if is_directory else len(self.files[f"{prefix}{name}"])),
            )
            for name, is_directory in sorted(names.items())
        ]
        if relative_directory == "nes":
            entries.append(
                RemoteEntry(
                    name="shortcut.srm",
                    relative_path="nes/shortcut.srm",
                    is_directory=False,
                    size_bytes=4,
                    is_symlink=True,
                )
            )
        return entries

    def resolve_path(self, root: object, relative_path: str):
        assert root is self.root
        return root, relative_path

    def open_binary(self, path):
        root, relative = path
        assert root is self.root
        return io.BytesIO(self.files[relative])

    def download_to_local(
        self,
        root,
        relative_path,
        destination,
        on_progress=None,
        *,
        operation=None,
    ):
        assert root is self.root
        if operation is not None:
            operation.check()
        self.downloaded.append((root, relative_path))
        Path(destination).write_bytes(self.files[relative_path])


def _store(provider: _ObjectProvider) -> ProviderRemoteSaveStore:
    return ProviderRemoteSaveStore(provider, provider.root, provider.root)


def test_opaque_root_and_allowlist_directed_scan() -> None:
    provider = _ObjectProvider()
    report = _store(provider).scan(
        DEFAULT_SAVE_SELECTION_POLICY,
        enabled_optional_systems=frozenset(),
        enabled_optional_groups=frozenset(),
    )

    artifact = report.artifacts["nes/game.srm"]
    assert artifact.content_hash == hashlib.sha256(b"save").hexdigest()
    assert all(root is provider.root for root, _ in provider.listed)
    assert (provider.root, "unknown") not in provider.listed
    assert "nes/shortcut.srm" not in report.artifacts


@pytest.mark.parametrize("key", ["../escape", "/absolute", "nes/../../escape", "nes\\..\\escape"])
def test_logical_key_escape_is_rejected(key: str) -> None:
    with pytest.raises(ValueError, match="Unsafe"):
        validate_logical_key(key)


def test_readable_and_writable_readiness_are_independent() -> None:
    read_only = _store(_ObjectProvider(writable=False))
    access = read_only.validate_access()
    assert access.readable is True
    assert access.writable is False
    assert read_only.is_readable() is True
    assert read_only.is_writable(access) is False


def test_expired_deadline_stops_before_provider_listing() -> None:
    provider = _ObjectProvider()
    with pytest.raises(Exception, match="deadline"):
        _store(provider).scan(
            DEFAULT_SAVE_SELECTION_POLICY,
            enabled_optional_systems=frozenset(),
            enabled_optional_groups=frozenset(),
            operation=RemoteOperationContext(deadline=1.0, clock=lambda: 2.0),
        )
    assert provider.listed == []


def test_cancellation_propagates_before_provider_listing() -> None:
    provider = _ObjectProvider()
    cancellation = TransferCancellationToken()
    cancellation.cancel()
    with pytest.raises(TransferCancelledError):
        _store(provider).scan(
            DEFAULT_SAVE_SELECTION_POLICY,
            enabled_optional_systems=frozenset(),
            enabled_optional_groups=frozenset(),
            operation=RemoteOperationContext(cancellation=cancellation),
        )
    assert provider.listed == []


def test_package_provider_is_not_required_to_expose_loose_objects() -> None:
    class PackageProvider:
        provider_id = "package-test"
        capabilities = ProviderCapabilities(supports_object_generations=True)

        def is_reachable(self, root):
            return True

        def validate_access(self, root):
            return StorageAccessResult(True, True)

        def remote_data_root(self, root, namespace):
            return (root, namespace)

    provider = PackageProvider()
    assert isinstance(provider, RemoteDataProvider)
    assert not isinstance(provider, LooseObjectRemoteDataProvider)


def test_protocol_root_never_reaches_local_recovery(tmp_path: Path) -> None:
    provider = _ObjectProvider()
    store = _store(provider)
    local = tmp_path / "local"
    local.mkdir()
    dangerous_local_path = tmp_path / "data" / "saves"
    dangerous_local_path.parent.mkdir()
    abandoned = dangerous_local_path.parent / ".saves.staging-sentinel"
    abandoned.mkdir()
    marker = abandoned / "keep"
    marker.write_text("not provider storage")

    service = SaveSyncService(
        provider=None,
        connectivity_root=None,
        local_root=str(local),
        remote_root=None,
        remote_store=store,
        state_path=tmp_path / "state" / "savesync-state.json",
    )
    service.preview_download()

    assert marker.read_text() == "not provider storage"
    assert service._remote_root is None


def test_filesystem_remote_data_object_contract(tmp_path: Path) -> None:
    provider = WritableLocalFilesystemProvider()
    root = tmp_path / "remote"
    root.mkdir()
    source = tmp_path / "source.bin"
    source.write_bytes(b"one")

    assert provider.metadata(str(root), "nes/game.srm") is None
    provider.ensure_directory(str(root), "nes")
    created = provider.upload_from_local(
        str(root), "nes/game.srm", str(source), create_only=True
    )
    assert created.size_bytes == 3
    assert created.revision
    assert (root / "nes" / "game.srm").read_bytes() == b"one"

    source.write_bytes(b"two")
    replaced = provider.upload_from_local(
        str(root),
        "nes/game.srm",
        str(source),
        expected_revision=created.revision,
    )
    assert replaced.revision != created.revision
    provider.delete_object(
        str(root), "nes/game.srm", expected_revision=replaced.revision
    )
    assert provider.metadata(str(root), "nes/game.srm") is None

    with pytest.raises(Exception, match="Unsafe"):
        provider.metadata(str(root), "../escape")
