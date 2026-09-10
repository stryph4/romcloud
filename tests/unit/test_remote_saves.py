from __future__ import annotations

import hashlib
import io
import os
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
from romcloud.core.save_selection import DEFAULT_SAVE_SELECTION_POLICY, SaveSelectionPolicy
from romcloud.core.storage import ProviderCapabilities, RemoteEntry, StorageAccessResult
from romcloud.infrastructure import save_tree
from romcloud.infrastructure.remote_saves import (
    FilesystemRemoteSaveStore,
    ProviderRemoteSaveStore,
)
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


class TestOnlyRelativePathsScopedScan:
    """``only_relative_paths`` must narrow a remote scan without opening any
    file outside that set, and without ever widening what the positive
    SaveLayout registry already admits."""

    def test_filesystem_store_skips_hashing_unrelated_files(self, tmp_path: Path, monkeypatch):
        root = tmp_path / "remote"
        (root / "snes").mkdir(parents=True)
        (root / "snes" / "GameA.srm").write_bytes(b"a-content")
        (root / "snes" / "GameB.srm").write_bytes(b"b-content")
        store = FilesystemRemoteSaveStore(WritableLocalFilesystemProvider(), str(root), str(root))

        hashed: list[Path] = []
        real_hash_file = save_tree.hash_file
        monkeypatch.setattr(
            save_tree,
            "hash_file",
            lambda path: (hashed.append(Path(path)), real_hash_file(path))[1],
        )

        report = store.scan(
            DEFAULT_SAVE_SELECTION_POLICY,
            enabled_optional_systems=frozenset(),
            enabled_optional_groups=frozenset(),
            only_relative_paths=frozenset({"snes/GameA.srm"}),
        )

        assert set(report.artifacts) == {"snes/GameA.srm"}
        assert hashed == [root / "snes" / "GameA.srm"]

    def test_filesystem_store_multiple_selected_paths_all_observed(self, tmp_path: Path):
        root = tmp_path / "remote"
        (root / "snes").mkdir(parents=True)
        (root / "snes" / "Game.srm").write_bytes(b"save")
        (root / "snes" / "Game.state0").write_bytes(b"state")
        (root / "snes" / "Other.srm").write_bytes(b"other")
        store = FilesystemRemoteSaveStore(WritableLocalFilesystemProvider(), str(root), str(root))

        report = store.scan(
            DEFAULT_SAVE_SELECTION_POLICY,
            enabled_optional_systems=frozenset(),
            enabled_optional_groups=frozenset(),
            only_relative_paths=frozenset({"snes/Game.srm", "snes/Game.state0"}),
        )

        assert set(report.artifacts) == {"snes/Game.srm", "snes/Game.state0"}

    def test_filesystem_store_missing_selected_path_is_simply_absent(self, tmp_path: Path):
        root = tmp_path / "remote"
        (root / "snes").mkdir(parents=True)
        (root / "snes" / "GameA.srm").write_bytes(b"a-content")
        store = FilesystemRemoteSaveStore(WritableLocalFilesystemProvider(), str(root), str(root))

        report = store.scan(
            DEFAULT_SAVE_SELECTION_POLICY,
            enabled_optional_systems=frozenset(),
            enabled_optional_groups=frozenset(),
            only_relative_paths=frozenset({"snes/GameA.srm", "snes/Deleted.srm"}),
        )

        assert set(report.artifacts) == {"snes/GameA.srm"}

    def test_filesystem_store_cannot_use_only_relative_paths_to_escape_registry(
        self, tmp_path: Path
    ):
        root = tmp_path / "remote"
        (root / "unknown-system").mkdir(parents=True)
        (root / "unknown-system" / "private.bin").write_bytes(b"private")
        store = FilesystemRemoteSaveStore(WritableLocalFilesystemProvider(), str(root), str(root))

        report = store.scan(
            DEFAULT_SAVE_SELECTION_POLICY,
            enabled_optional_systems=frozenset(),
            enabled_optional_groups=frozenset(),
            only_relative_paths=frozenset({"unknown-system/private.bin"}),
        )

        assert report.artifacts == {}

    def test_provider_store_skips_downloading_unrelated_files(self):
        provider = _ObjectProvider()
        provider.files["nes/other.srm"] = b"other"
        opened: list[str] = []
        real_open_binary = provider.open_binary
        provider.open_binary = lambda path: (opened.append(path[1]), real_open_binary(path))[1]
        store = _store(provider)

        report = store.scan(
            DEFAULT_SAVE_SELECTION_POLICY,
            enabled_optional_systems=frozenset(),
            enabled_optional_groups=frozenset(),
            only_relative_paths=frozenset({"nes/game.srm"}),
        )

        assert set(report.artifacts) == {"nes/game.srm"}
        assert opened == ["nes/game.srm"]


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


class TestFilesystemRemoteSaveStoreObservationTrust:
    """A ``FilesystemRemoteSaveStore`` may be a mounted CIFS/SMB share whose
    metadata cannot prove another client did not rewrite a file, so it must
    never reuse any caller-supplied observation cache."""

    def _store(self, root: Path) -> FilesystemRemoteSaveStore:
        return FilesystemRemoteSaveStore(
            WritableLocalFilesystemProvider(), str(root), str(root)
        )

    def test_ignores_a_stale_cache_entry_keyed_by_current_metadata(
        self, tmp_path: Path
    ):
        """Simulates the worst case: a cache entry that exactly matches the
        file's *current* (device, inode, size, mtime, ctime) tuple but holds
        the wrong digest — the scenario a coarse/cached CIFS stat could
        produce. The store must still report the real content, proving it
        never even consults the cache rather than merely tending to miss."""
        root = tmp_path / "remote"
        save = root / "snes" / "Super Metroid.srm"
        save.parent.mkdir(parents=True)
        save.write_bytes(b"real-current-content")
        status = save.stat()

        poisoned = save_tree.ContentObservationCache()
        poisoned._entries[
            (
                str(save),
                status.st_dev,
                status.st_ino,
                status.st_size,
                status.st_mtime_ns,
                status.st_ctime_ns,
            )
        ] = "0" * 64  # a plausible-looking but wrong sha256 hex digest

        report = self._store(root).scan(
            DEFAULT_SAVE_SELECTION_POLICY,
            enabled_optional_systems=frozenset(),
            enabled_optional_groups=frozenset(),
            cache=poisoned,
        )

        assert (
            report.artifacts["snes/Super Metroid.srm"].content_hash
            == save_tree.hash_file(save)
        )
        assert report.artifacts["snes/Super Metroid.srm"].content_hash != "0" * 64

    def test_same_size_rewrite_with_coarse_unchanged_mtime_is_still_detected(
        self, tmp_path: Path
    ):
        """A network share can report the same mtime for two scans spanning a
        same-size rewrite (coarse resolution, client-side attribute caching).
        Even when the filesystem-level signal genuinely looks unchanged, the
        store must not have cached the first observation to serve here."""
        root = tmp_path / "remote"
        save = root / "snes" / "Super Metroid.srm"
        save.parent.mkdir(parents=True)
        save.write_bytes(b"original-save-bytes!")
        store = self._store(root)
        cache = save_tree.ContentObservationCache()

        first = store.scan(
            DEFAULT_SAVE_SELECTION_POLICY,
            enabled_optional_systems=frozenset(),
            enabled_optional_groups=frozenset(),
            cache=cache,
        )
        before = save.stat()
        save.write_bytes(b"different-save-bytes")  # same length
        os.utime(save, ns=(before.st_atime_ns, before.st_mtime_ns))

        second = store.scan(
            DEFAULT_SAVE_SELECTION_POLICY,
            enabled_optional_systems=frozenset(),
            enabled_optional_groups=frozenset(),
            cache=cache,
        )

        assert (
            first.artifacts["snes/Super Metroid.srm"].content_hash
            != second.artifacts["snes/Super Metroid.srm"].content_hash
        )
        assert (
            second.artifacts["snes/Super Metroid.srm"].content_hash
            == save_tree.hash_file(save)
        )

    def test_repeated_scans_always_re_read_bytes(self, tmp_path: Path, monkeypatch):
        root = tmp_path / "remote"
        save = root / "snes" / "Super Metroid.srm"
        save.parent.mkdir(parents=True)
        save.write_bytes(b"content")
        reads: list[Path] = []
        original = save_tree.hash_file
        monkeypatch.setattr(
            save_tree,
            "hash_file",
            lambda path: (reads.append(Path(path)), original(path))[1],
        )
        store = self._store(root)
        cache = save_tree.ContentObservationCache()

        store.scan(
            DEFAULT_SAVE_SELECTION_POLICY,
            enabled_optional_systems=frozenset(),
            enabled_optional_groups=frozenset(),
            cache=cache,
        )
        store.scan(
            DEFAULT_SAVE_SELECTION_POLICY,
            enabled_optional_systems=frozenset(),
            enabled_optional_groups=frozenset(),
            cache=cache,
        )

        assert len(reads) == 2

    def test_unrelated_remote_saves_are_never_hashed(self, tmp_path: Path, monkeypatch):
        """Freshness must not regress into scanning unrelated data: a narrow
        layout scope still only touches its own files."""
        root = tmp_path / "remote"
        snes = root / "snes" / "Super Metroid.srm"
        psx = root / "psx" / "duckstation" / "memcards" / "shared_card_1.mcd"
        snes.parent.mkdir(parents=True)
        psx.parent.mkdir(parents=True)
        snes.write_bytes(b"snes-save")
        psx.write_bytes(b"unrelated-psx-memory-card")
        reads: list[Path] = []
        original = save_tree.hash_file
        monkeypatch.setattr(
            save_tree,
            "hash_file",
            lambda path: (reads.append(Path(path)), original(path))[1],
        )
        snes_only = SaveSelectionPolicy(
            layouts=tuple(
                layout
                for layout in DEFAULT_SAVE_SELECTION_POLICY.layouts
                if layout.system == "snes"
            )
        )

        self._store(root).scan(
            snes_only,
            enabled_optional_systems=frozenset(),
            enabled_optional_groups=frozenset(),
        )

        assert reads == [snes]
