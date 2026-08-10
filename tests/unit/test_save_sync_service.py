"""Unit tests for romcloud.services.saves.SaveSyncService."""

from __future__ import annotations

import errno
from pathlib import Path

import pytest

from romcloud.bootstrap.container import Container
from romcloud.core.exceptions import SaveSyncConnectivityError, SaveSyncVerificationError
from romcloud.core.models.savesync import SaveChangeKind, SaveDiff
from romcloud.core.storage import StorageProvider
from romcloud.infrastructure import mount, save_tree
from romcloud.infrastructure.config import (
    AppConfig,
    CacheConfig,
    RemoteDataConfig,
    SavesConfig,
    SMBConfig,
    SourceConfig,
)
from romcloud.services.saves import SaveSyncService


class _FakeProvider(StorageProvider):
    """Minimal StorageProvider — SaveSyncService only ever calls is_reachable."""

    def __init__(self, *, reachable: bool = True) -> None:
        self.reachable = reachable

    @property
    def provider_id(self) -> str:
        return "fake"

    def is_reachable(self, root: str) -> bool:
        return self.reachable

    def list_systems(self, rom_root: str) -> list[str]:
        raise NotImplementedError

    def list_entries(self, rom_root: str, system: str):
        raise NotImplementedError

    def get_size(self, path: str):
        raise NotImplementedError

    def read_text(self, path: str) -> str:
        raise NotImplementedError

    def transfer_to(self, source_path: str, dest_path: str, on_progress=None) -> None:
        raise NotImplementedError


@pytest.fixture
def provider() -> _FakeProvider:
    return _FakeProvider(reachable=True)


@pytest.fixture
def service(tmp_path: Path, provider: _FakeProvider) -> SaveSyncService:
    local = tmp_path / "local-saves"
    remote = tmp_path / "remote-saves"
    local.mkdir()
    return SaveSyncService(
        provider=provider,
        connectivity_root=str(tmp_path / "rom-root"),
        local_root=str(local),
        remote_root=str(remote),
        state_path=tmp_path / "data" / "savesync-state.json",
        xbox_enabled=False,
    )


def _write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)


class TestConnectivityFailure:
    def test_preview_upload_aborts_without_touching_anything(self, tmp_path, provider, service):
        provider.reachable = False
        local_file = tmp_path / "local-saves" / "psx" / "Game.srm"
        _write(local_file, b"save-data")

        with pytest.raises(SaveSyncConnectivityError):
            service.preview_upload()

        assert local_file.exists()
        assert not (tmp_path / "remote-saves").exists()

    def test_preview_download_aborts_when_unreachable(self, provider, service):
        provider.reachable = False
        with pytest.raises(SaveSyncConnectivityError):
            service.preview_download()

    def test_commit_upload_aborts_when_unreachable(self, tmp_path, provider, service):
        _write(tmp_path / "local-saves" / "psx" / "Game.srm", b"save-data")
        diff = service.preview_upload()
        provider.reachable = False

        with pytest.raises(SaveSyncConnectivityError):
            service.commit_upload(diff)

        assert not (tmp_path / "remote-saves").exists()
        assert service.get_state().last_upload is None


class TestWritableRemoteBoundary:
    def test_smb_upload_never_stages_under_read_only_catalog_mount(
        self, tmp_path: Path, monkeypatch
    ):
        rom_mount = tmp_path / "rom-source-ro"
        save_mount = tmp_path / "save-source-rw"
        local_saves = tmp_path / "local-saves"
        for path in (rom_mount, save_mount, local_saves):
            path.mkdir()

        config = AppConfig(
            source=SourceConfig(provider="local", rom_root=str(rom_mount)),
            cache=CacheConfig(path=str(tmp_path / "cache")),
            local_roms_path=str(tmp_path / "roms"),
            data_path=str(tmp_path / "data"),
            smb=SMBConfig(server="nas.local", share="ROMs"),
            remote_data=RemoteDataConfig(
                provider="smb",
                root=str(save_mount),
                smb=SMBConfig(server="data-nas.local", share="ROMCloud"),
            ),
            saves=SavesConfig(local_path=str(local_saves)),
        )
        _write(local_saves / "psx" / "Game.srm", b"hardware-regression")
        rom_mount.chmod(0o555)

        real_new_staging_dir = save_tree.new_staging_dir

        def reject_catalog_staging(sibling_of: Path) -> Path:
            if sibling_of == rom_mount / "saves":
                raise OSError(errno.EROFS, "Read-only file system", str(sibling_of))
            return real_new_staging_dir(sibling_of)

        monkeypatch.setattr(save_tree, "new_staging_dir", reject_catalog_staging)
        monkeypatch.setattr(
            mount,
            "is_target_mounted_cifs",
            lambda path, **kwargs: Path(path) == save_mount,
        )

        service = Container(config).saves
        record = service.commit_upload(service.preview_upload())

        assert record.artifact_count == 1
        assert not (rom_mount / "saves").exists()
        assert (save_mount / "saves" / "psx" / "Game.srm").read_bytes() == b"hardware-regression"

    def test_disconnected_smb_mount_point_directory_is_not_reachable(
        self, tmp_path: Path, monkeypatch
    ):
        rom_mount = tmp_path / "rom-source-ro"
        save_mount = tmp_path / "save-source-rw"
        local_saves = tmp_path / "local-saves"
        for path in (rom_mount, save_mount, local_saves):
            path.mkdir()
        config = AppConfig(
            source=SourceConfig(provider="local", rom_root=str(rom_mount)),
            cache=CacheConfig(path=str(tmp_path / "cache")),
            local_roms_path=str(tmp_path / "roms"),
            data_path=str(tmp_path / "data"),
            smb=SMBConfig(server="nas.local", share="ROMs"),
            remote_data=RemoteDataConfig(
                provider="smb",
                root=str(save_mount),
                smb=SMBConfig(server="data-nas.local", share="ROMCloud"),
            ),
            saves=SavesConfig(local_path=str(local_saves)),
        )
        _write(local_saves / "psx" / "Game.srm", b"must-not-land-locally")
        monkeypatch.setattr(mount, "is_target_mounted_cifs", lambda path, **kwargs: False)

        service = Container(config).saves
        with pytest.raises(SaveSyncConnectivityError):
            service.preview_upload()

        assert list(save_mount.iterdir()) == []

    def test_read_only_remote_data_mount_is_rejected_before_staging(
        self, tmp_path: Path, monkeypatch
    ):
        rom_mount = tmp_path / "rom-source-ro"
        remote_mount = tmp_path / "remote-mounted-ro"
        local_saves = tmp_path / "local-saves"
        for path in (rom_mount, remote_mount, local_saves):
            path.mkdir()
        config = AppConfig(
            source=SourceConfig("local", str(rom_mount)),
            cache=CacheConfig(str(tmp_path / "cache")),
            local_roms_path=str(tmp_path / "roms"),
            data_path=str(tmp_path / "data"),
            smb=SMBConfig("rom-nas", "ROMs"),
            remote_data=RemoteDataConfig(
                "smb",
                str(remote_mount),
                SMBConfig("data-nas", "ROMCloud"),
            ),
            saves=SavesConfig(local_path=str(local_saves)),
        )
        _write(local_saves / "psx" / "Game.srm", b"do-not-stage")
        monkeypatch.setattr(mount, "is_target_mounted_cifs", lambda path, **kwargs: False)

        with pytest.raises(SaveSyncConnectivityError):
            Container(config).saves.preview_upload()

        assert list(remote_mount.iterdir()) == []

    def test_wrong_rw_smb_share_is_rejected_before_staging(
        self, tmp_path: Path, monkeypatch
    ):
        remote_mount = tmp_path / "remote-mounted-wrong-share"
        local_saves = tmp_path / "local-saves"
        remote_mount.mkdir()
        local_saves.mkdir()
        config = AppConfig(
            source=SourceConfig("local", str(tmp_path / "roms")),
            cache=CacheConfig(str(tmp_path / "cache")),
            local_roms_path=str(tmp_path / "roms"),
            data_path=str(tmp_path / "data"),
            remote_data=RemoteDataConfig(
                "smb", str(remote_mount), SMBConfig("data-nas", "ROMCloud")
            ),
            saves=SavesConfig(local_path=str(local_saves)),
        )
        _write(local_saves / "psx" / "Game.srm", b"do-not-stage")
        observed = []

        def reject_wrong_share(path, **expected):
            observed.append(expected)
            return False

        monkeypatch.setattr(mount, "is_target_mounted_cifs", reject_wrong_share)

        with pytest.raises(SaveSyncConnectivityError):
            Container(config).saves.preview_upload()

        assert observed == [
            {"server": "data-nas", "share": "ROMCloud", "read_only": False}
        ]
        assert list(remote_mount.iterdir()) == []

    def test_missing_remote_data_configuration_disables_savesync(self, tmp_path: Path):
        config = AppConfig(
            source=SourceConfig(provider="local", rom_root=str(tmp_path / "roms")),
            cache=CacheConfig(path=str(tmp_path / "cache")),
            local_roms_path=str(tmp_path / "roms"),
            data_path=str(tmp_path / "data"),
            saves=SavesConfig(local_path=str(tmp_path / "local-saves")),
        )
        service = Container(config).saves

        assert service.is_remote_configured is False
        with pytest.raises(SaveSyncConnectivityError, match="not configured"):
            service.preview_upload()


class TestPreviewAccuracy:
    def test_added_entry_on_first_upload(self, tmp_path, service):
        _write(tmp_path / "local-saves" / "psx" / "Game.srm", b"save-data")

        diff = service.preview_upload()

        assert len(diff.added) == 1
        assert diff.added[0].relative_path == "psx/Game.srm"
        assert diff.transfer_bytes == len(b"save-data")
        assert diff.changed == () and diff.removed == () and diff.unchanged == ()

    def test_unchanged_after_matching_commit(self, tmp_path, service):
        _write(tmp_path / "local-saves" / "psx" / "Game.srm", b"save-data")
        service.commit_upload(service.preview_upload())

        diff = service.preview_upload()

        assert diff.unchanged and diff.unchanged[0].relative_path == "psx/Game.srm"
        assert diff.added == () and diff.changed == ()
        assert diff.transfer_bytes == 0

    def test_changed_entry_detected_by_content(self, tmp_path, service):
        local_file = tmp_path / "local-saves" / "psx" / "Game.srm"
        _write(local_file, b"version-1")
        service.commit_upload(service.preview_upload())
        local_file.write_bytes(b"version-2-longer")

        diff = service.preview_upload()

        assert len(diff.changed) == 1
        assert diff.changed[0].relative_path == "psx/Game.srm"
        assert diff.transfer_bytes == len(b"version-2-longer")

    def test_removed_entry_when_local_file_deleted(self, tmp_path, service):
        local_file = tmp_path / "local-saves" / "psx" / "Game.srm"
        _write(local_file, b"save-data")
        service.commit_upload(service.preview_upload())
        local_file.unlink()

        diff = service.preview_upload()

        assert len(diff.removed) == 1
        assert diff.removed[0].relative_path == "psx/Game.srm"
        assert diff.transfer_bytes == 0

    def test_download_diff_is_symmetric(self, tmp_path, service):
        _write(tmp_path / "local-saves" / "psx" / "Game.srm", b"save-data")
        service.commit_upload(service.preview_upload())
        # Simulate a second machine with nothing local yet.
        (tmp_path / "local-saves" / "psx" / "Game.srm").unlink()

        diff = service.preview_download()

        assert diff.direction == "download"
        assert len(diff.added) == 1
        assert diff.added[0].relative_path == "psx/Game.srm"


class TestExclusions:
    def test_unknown_system_never_appears_in_diff(self, tmp_path, service):
        _write(tmp_path / "local-saves" / "dolphin" / "GameSave.gci", b"x")

        diff = service.preview_upload()

        assert diff.entries == ()

    def test_excluded_pattern_never_appears_in_diff(self, tmp_path, service):
        _write(tmp_path / "local-saves" / "pcsx2" / "sstates" / "Game.p2s", b"x")
        _write(tmp_path / "local-saves" / "pcsx2" / "Mcd001.ps2", b"card")

        diff = service.preview_upload()

        assert [e.relative_path for e in diff.entries] == ["pcsx2/Mcd001.ps2"]

    def test_flatpak_dir_never_appears_in_diff(self, tmp_path, service):
        _write(tmp_path / "local-saves" / "flatpak" / "whatever.dat", b"x")

        assert service.preview_upload().entries == ()


class TestXboxOptIn:
    def test_disabled_by_default_hdd_excluded(self, tmp_path, service):
        _write(tmp_path / "local-saves" / "xbox" / "xbox_hdd.qcow2", b"vhd" * 100)

        diff = service.preview_upload()

        assert diff.entries == ()

    def test_enabled_includes_hdd(self, tmp_path, provider):
        local = tmp_path / "local-saves"
        _write(local / "xbox" / "xbox_hdd.qcow2", b"vhd" * 100)
        svc = SaveSyncService(
            provider=provider,
            connectivity_root=str(tmp_path / "rom-root"),
            local_root=str(local),
            remote_root=str(tmp_path / "remote-saves"),
            state_path=tmp_path / "data" / "savesync-state.json",
            xbox_enabled=True,
        )

        diff = svc.preview_upload()

        assert [e.relative_path for e in diff.entries] == ["xbox/xbox_hdd.qcow2"]

    def test_xbox_hdd_size_reported_and_none_when_absent(self, tmp_path, service):
        assert service.xbox_hdd_size() is None
        _write(tmp_path / "local-saves" / "xbox" / "xbox_hdd.qcow2", b"x" * 4096)
        assert service.xbox_hdd_size() == 4096


class TestCommitAuthoritativeSemantics:
    def test_upload_makes_remote_mirror_local_including_deletions(self, tmp_path, service):
        _write(tmp_path / "local-saves" / "psx" / "A.srm", b"a")
        _write(tmp_path / "local-saves" / "psx" / "B.srm", b"b")
        service.commit_upload(service.preview_upload())

        (tmp_path / "local-saves" / "psx" / "B.srm").unlink()
        service.commit_upload(service.preview_upload())

        remote_root = tmp_path / "remote-saves"
        assert (remote_root / "psx" / "A.srm").exists()
        assert not (remote_root / "psx" / "B.srm").exists()

    def test_download_makes_local_mirror_remote(self, tmp_path, service):
        _write(tmp_path / "local-saves" / "psx" / "A.srm", b"a")
        service.commit_upload(service.preview_upload())
        _write(tmp_path / "local-saves" / "psx" / "Local-only.srm", b"local-only")

        service.commit_download(service.preview_download())

        local_root = tmp_path / "local-saves"
        assert (local_root / "psx" / "A.srm").exists()
        assert not (local_root / "psx" / "Local-only.srm").exists()


class TestStagingCommitFailureSafety:
    def test_verification_failure_never_touches_existing_remote(self, tmp_path, service, monkeypatch):
        _write(tmp_path / "local-saves" / "psx" / "Good.srm", b"good")
        service.commit_upload(service.preview_upload())
        previous_bytes = (tmp_path / "remote-saves" / "psx" / "Good.srm").read_bytes()

        _write(tmp_path / "local-saves" / "psx" / "Corrupt.srm", b"about-to-change")
        diff = service.preview_upload()
        # Simulate the source changing after preview but before commit finishes.
        (tmp_path / "local-saves" / "psx" / "Corrupt.srm").write_bytes(b"different-content-now")

        with pytest.raises(SaveSyncVerificationError):
            service.commit_upload(diff)

        assert (tmp_path / "remote-saves" / "psx" / "Good.srm").read_bytes() == previous_bytes
        assert not (tmp_path / "remote-saves" / "psx" / "Corrupt.srm").exists()
        # No leftover staging directories.
        assert list(tmp_path.glob(".remote-saves.staging-*")) == []
        assert service.get_state().last_upload is not None
        assert service.get_state().last_upload.manifest[0].relative_path == "psx/Good.srm"

    def test_state_does_not_advance_on_verification_failure(self, tmp_path, service):
        _write(tmp_path / "local-saves" / "psx" / "A.srm", b"a")
        diff = service.preview_upload()
        (tmp_path / "local-saves" / "psx" / "A.srm").write_bytes(b"changed-after-preview")

        with pytest.raises(SaveSyncVerificationError):
            service.commit_upload(diff)

        assert service.get_state().last_upload is None

    def test_state_does_not_advance_when_atomic_swap_fails(
        self, tmp_path, service, monkeypatch
    ):
        _write(tmp_path / "local-saves" / "psx" / "A.srm", b"a")
        diff = service.preview_upload()
        monkeypatch.setattr(
            save_tree,
            "atomic_replace_dir",
            lambda *args: (_ for _ in ()).throw(OSError("simulated rename failure")),
        )

        with pytest.raises(OSError, match="simulated rename failure"):
            service.commit_upload(diff)

        assert service.get_state().last_upload is None
        assert not (tmp_path / "remote-saves").exists()
        assert list(tmp_path.glob(".remote-saves.staging-*")) == []

    def test_no_staging_directory_left_behind_after_failure(self, tmp_path, service):
        _write(tmp_path / "local-saves" / "psx" / "A.srm", b"a")
        diff = service.preview_upload()
        (tmp_path / "local-saves" / "psx" / "A.srm").write_bytes(b"tampered")

        with pytest.raises(SaveSyncVerificationError):
            service.commit_upload(diff)

        leftovers = [p for p in tmp_path.iterdir() if p.name.startswith(".remote-saves.staging-")]
        assert leftovers == []


class TestManifestAndStateAdvancement:
    def test_state_only_advances_after_successful_commit(self, tmp_path, service):
        assert service.get_state().last_upload is None
        _write(tmp_path / "local-saves" / "psx" / "A.srm", b"a")

        record = service.commit_upload(service.preview_upload())

        state = service.get_state()
        assert state.last_upload is not None
        assert state.last_upload.revision == record.revision
        assert state.last_upload.manifest[0].relative_path == "psx/A.srm"
        assert state.last_download is None

    def test_device_id_stable_across_calls(self, service):
        first = service.get_state().device_id
        second = service.get_state().device_id
        assert first == second and first

    def test_upload_and_download_records_are_independent(self, tmp_path, service):
        _write(tmp_path / "local-saves" / "psx" / "A.srm", b"a")
        upload_record = service.commit_upload(service.preview_upload())
        download_record = service.commit_download(service.preview_download())

        state = service.get_state()
        assert state.last_upload.revision == upload_record.revision
        assert state.last_download.revision == download_record.revision


class TestRepeatedCommitIsSafe:
    def test_committing_identical_state_twice_is_a_no_op(self, tmp_path, service):
        _write(tmp_path / "local-saves" / "psx" / "A.srm", b"a")
        service.commit_upload(service.preview_upload())
        second_diff = service.preview_upload()

        assert second_diff.entries and all(
            e.change == SaveChangeKind.UNCHANGED for e in second_diff.entries
        )
        record = service.commit_upload(second_diff)
        assert (tmp_path / "remote-saves" / "psx" / "A.srm").read_bytes() == b"a"
        assert record.artifact_count == 1


class TestDiffSerialization:
    def test_to_dict_and_from_dict_round_trip(self, tmp_path, service):
        _write(tmp_path / "local-saves" / "psx" / "A.srm", b"a")
        diff = service.preview_upload()

        restored = SaveDiff.from_dict(diff.to_dict())

        assert restored.direction == diff.direction
        assert restored.entries == diff.entries
