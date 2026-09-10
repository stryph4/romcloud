"""Step 5: targeted, best-effort ``gameStart`` synchronization.

SaveSync must never hold the game hostage: every scenario here asserts that
``AutoSaveSyncCoordinator.game_start`` returns normally (never raises) no
matter what the underlying targeted sync attempt discovers, while also
verifying the narrow scope contract — only the launched game's own
deterministically-resolved save group is ever touched, never a broader scan.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from romcloud.core.capabilities import CapabilityPolicy, OperatingMode
from romcloud.core.exceptions import SaveSyncError
from romcloud.core.save_selection import DEFAULT_SAVE_SELECTION_POLICY
from romcloud.core.storage import ProviderCapabilities, StorageProvider
from romcloud.infrastructure import savesync_index
from romcloud.services.auto_savesync import AutoSaveSyncCoordinator
from romcloud.services.saves import SaveSyncService

from tests.unit._savesync_protocol_helpers import (
    index_root_for,
    mutate_remote_out_of_band,
    seed_peer_commit,
    strip_protocol_ownership,
)


class _Provider(StorageProvider):
    def __init__(self) -> None:
        self.reachable = True

    @property
    def provider_id(self) -> str:
        return "test"

    @property
    def capabilities(self):
        return ProviderCapabilities(
            has_filesystem_semantics=True, supports_durable_transactions=True
        )

    def is_reachable(self, root: str) -> bool:
        return self.reachable

    def list_systems(self, rom_root: str):
        raise NotImplementedError

    def list_entries(self, rom_root: str, system: str):
        raise NotImplementedError

    def get_size(self, path: str):
        raise NotImplementedError

    def read_text(self, path: str) -> str:
        raise NotImplementedError

    def transfer_to(self, source_path: str, dest_path: str, on_progress=None) -> None:
        raise NotImplementedError


def _write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)


def _service(
    tmp_path: Path,
    provider: _Provider,
    *,
    capability_policy: CapabilityPolicy | None = None,
) -> SaveSyncService:
    local = tmp_path / "local"
    local.mkdir(exist_ok=True)
    return SaveSyncService(
        provider=provider,
        connectivity_root=str(tmp_path / "remote-data"),
        local_root=str(local),
        remote_root=str(tmp_path / "remote"),
        state_path=tmp_path / "data" / "savesync-state.json",
        capability_policy=capability_policy,
    )


def _coordinator(tmp_path: Path, service: SaveSyncService) -> AutoSaveSyncCoordinator:
    return AutoSaveSyncCoordinator(
        service,
        data_root=tmp_path / "data",
        enabled=True,
        policy=DEFAULT_SAVE_SELECTION_POLICY,
        quiet_seconds=0,
    )


def _session_payload(tmp_path: Path, *, system: str, rom: str) -> dict:
    coordinator = _coordinator(tmp_path, SaveSyncService(
        provider=_Provider(),
        connectivity_root=str(tmp_path / "remote-data"),
        local_root=str(tmp_path / "local"),
        remote_root=str(tmp_path / "remote"),
        state_path=tmp_path / "data" / "savesync-state.json",
    ))
    path = coordinator._sessions._path(system, rom)
    return json.loads(path.read_text(encoding="utf-8"))


class TestGameStartOwnedDataset:
    def test_current_state_is_minimal_metadata_only(self, tmp_path: Path, monkeypatch):
        provider = _Provider()
        service = _service(tmp_path, provider)
        _write(tmp_path / "local" / "snes" / "Super Metroid.srm", b"base")
        service.full_sync()
        coordinator = _coordinator(tmp_path, service)

        # First gameStart after full_sync primes this device's own layout
        # watermark for retroarch-root-snes (one shard read).
        coordinator.game_start(
            system="snes", emulator="libretro", core="snes9x", rom="Super Metroid.sfc"
        )

        calls: list[str] = []
        original = savesync_index.load_shard

        def _tracking(index_root, layout_id, layout_head):
            calls.append(layout_id)
            return original(index_root, layout_id, layout_head)

        monkeypatch.setattr(savesync_index, "load_shard", _tracking)
        coordinator.game_start(
            system="snes", emulator="libretro", core="snes9x", rom="Super Metroid.sfc"
        )
        assert calls == []
        payload = _session_payload(tmp_path, system="snes", rom="Super Metroid.sfc")
        assert payload["sync_outcome"] == "synchronized"
        assert payload["sync_group_ids"] == ["retroarch-root-snes/super metroid"]

    def test_peer_advanced_group_materialized_before_success(self, tmp_path: Path):
        provider = _Provider()
        service = _service(tmp_path, provider)
        _write(tmp_path / "local" / "snes" / "Super Metroid.srm", b"base")
        service.full_sync()

        seed_peer_commit(
            service,
            remote_root=tmp_path / "remote",
            relative_path="snes/Super Metroid.srm",
            content=b"peer-advanced",
        )

        coordinator = _coordinator(tmp_path, service)
        coordinator.game_start(
            system="snes", emulator="libretro", core="snes9x", rom="Super Metroid.sfc"
        )

        assert (tmp_path / "local" / "snes" / "Super Metroid.srm").read_bytes() == b"peer-advanced"
        payload = _session_payload(tmp_path, system="snes", rom="Super Metroid.sfc")
        assert payload["sync_outcome"] == "synchronized"

    def test_peer_advanced_with_no_local_dirty_hint(self, tmp_path: Path):
        provider = _Provider()
        service = _service(tmp_path, provider)
        _write(tmp_path / "local" / "snes" / "Super Metroid.srm", b"base")
        service.full_sync()
        assert not any(
            group.dirty_path_hints for group in service.get_state().groups
        )

        seed_peer_commit(
            service,
            remote_root=tmp_path / "remote",
            relative_path="snes/Super Metroid.srm",
            content=b"from-peer",
        )
        coordinator = _coordinator(tmp_path, service)
        coordinator.game_start(
            system="snes", emulator="libretro", core="snes9x", rom="Super Metroid.sfc"
        )
        assert (tmp_path / "local" / "snes" / "Super Metroid.srm").read_bytes() == b"from-peer"

    def test_remote_only_brand_new_save_discovered_from_index(self, tmp_path: Path):
        provider = _Provider()
        service = _service(tmp_path, provider)
        _write(tmp_path / "local" / "snes" / "Other.srm", b"seed")
        service.full_sync()
        (tmp_path / "local" / "snes" / "Other.srm").unlink()

        seed_peer_commit(
            service,
            remote_root=tmp_path / "remote",
            relative_path="snes/Super Metroid.srm",
            content=b"never-seen-before",
        )
        coordinator = _coordinator(tmp_path, service)
        assert not (tmp_path / "local" / "snes" / "Super Metroid.srm").exists()
        coordinator.game_start(
            system="snes", emulator="libretro", core="snes9x", rom="Super Metroid.sfc"
        )
        assert (
            tmp_path / "local" / "snes" / "Super Metroid.srm"
        ).read_bytes() == b"never-seen-before"

    def test_unrelated_changed_group_in_same_layout_untouched(self, tmp_path: Path):
        provider = _Provider()
        service = _service(tmp_path, provider)
        _write(tmp_path / "local" / "psx" / "Game.srm", b"base")
        service.full_sync()

        seed_peer_commit(
            service,
            remote_root=tmp_path / "remote",
            relative_path="psx/OtherGame.srm",
            content=b"peer-only-other-game",
        )
        coordinator = _coordinator(tmp_path, service)
        coordinator.game_start(system="psx", emulator="libretro", core="pcsx", rom="Game.chd")

        assert not (tmp_path / "local" / "psx" / "OtherGame.srm").exists()
        payload = _session_payload(tmp_path, system="psx", rom="Game.chd")
        assert payload["sync_group_ids"] == ["retroarch-root-psx/game"]

    def test_local_dirty_remote_unchanged_uploads(self, tmp_path: Path):
        provider = _Provider()
        service = _service(tmp_path, provider)
        _write(tmp_path / "local" / "snes" / "Super Metroid.srm", b"base")
        service.full_sync()
        _write(tmp_path / "local" / "snes" / "Super Metroid.srm", b"local-edit")
        service.mark_local_dirty("snes/Super Metroid.srm")

        coordinator = _coordinator(tmp_path, service)
        coordinator.game_start(
            system="snes", emulator="libretro", core="snes9x", rom="Super Metroid.sfc"
        )
        assert (
            tmp_path / "remote" / "snes" / "Super Metroid.srm"
        ).read_bytes() == b"local-edit"
        payload = _session_payload(tmp_path, system="snes", rom="Super Metroid.sfc")
        assert payload["sync_outcome"] == "synchronized"

    def test_both_sides_changed_reaches_conflict_and_does_not_block_launch(
        self, tmp_path: Path
    ):
        provider = _Provider()
        service = _service(tmp_path, provider)
        _write(tmp_path / "local" / "snes" / "Super Metroid.srm", b"base")
        service.full_sync()
        _write(tmp_path / "local" / "snes" / "Super Metroid.srm", b"local-edit")
        service.mark_local_dirty("snes/Super Metroid.srm")
        seed_peer_commit(
            service,
            remote_root=tmp_path / "remote",
            relative_path="snes/Super Metroid.srm",
            content=b"remote-edit",
        )

        coordinator = _coordinator(tmp_path, service)
        coordinator.game_start(
            system="snes", emulator="libretro", core="snes9x", rom="Super Metroid.sfc"
        )  # must not raise

        assert (
            tmp_path / "local" / "snes" / "Super Metroid.srm"
        ).read_bytes() == b"local-edit"
        assert (
            tmp_path / "remote" / "snes" / "Super Metroid.srm"
        ).read_bytes() == b"remote-edit"

    def test_journal_index_divergence_does_not_block_launch(self, tmp_path: Path):
        from romcloud.infrastructure import savesync_journal

        provider = _Provider()
        service = _service(tmp_path, provider)
        _write(tmp_path / "local" / "snes" / "Super Metroid.srm", b"base")
        service.full_sync()
        savesync_journal.append_mutations(
            savesync_journal.default_journal_path(tmp_path / "remote"),
            device_id="old-client",
            revision="legacy-write",
            timestamp="2026-08-22T00:00:00+00:00",
            mutations=[
                {
                    "system": "snes",
                    "layout_id": "retroarch-root-snes",
                    "group_id": "retroarch-root-snes/super metroid",
                    "object_id": "snes/Super Metroid.srm",
                    "operation": "update",
                }
            ],
        )

        coordinator = _coordinator(tmp_path, service)
        coordinator.game_start(
            system="snes", emulator="libretro", core="snes9x", rom="Super Metroid.sfc"
        )  # must not raise
        payload = _session_payload(tmp_path, system="snes", rom="Super Metroid.sfc")
        assert payload["sync_outcome"] == "unresolved"


    def test_damaged_dataset_fails_closed_without_blocking_launch(self, tmp_path: Path):
        provider = _Provider()
        service = _service(tmp_path, provider)
        _write(tmp_path / "local" / "snes" / "Super Metroid.srm", b"base")
        service.full_sync()
        index_root = index_root_for(tmp_path / "remote")
        head_path = savesync_index.head_path(index_root)
        head_path.write_text("not json", encoding="utf-8")

        coordinator = _coordinator(tmp_path, service)
        coordinator.game_start(
            system="snes", emulator="libretro", core="snes9x", rom="Super Metroid.sfc"
        )  # must not raise
        payload = _session_payload(tmp_path, system="snes", rom="Super Metroid.sfc")
        assert payload["sync_outcome"] == "unresolved"


class TestGameStartLegacyDataset:
    def test_unowned_known_group_stays_legacy_compatible(self, tmp_path: Path):
        provider = _Provider()
        service = _service(tmp_path, provider)
        _write(tmp_path / "local" / "snes" / "Super Metroid.srm", b"base")
        service.full_sync()
        strip_protocol_ownership(tmp_path / "remote", tmp_path / "data")
        _write(tmp_path / "local" / "snes" / "Super Metroid.srm", b"changed")
        service.mark_local_dirty("snes/Super Metroid.srm")

        coordinator = _coordinator(tmp_path, service)
        coordinator.game_start(
            system="snes", emulator="libretro", core="snes9x", rom="Super Metroid.sfc"
        )
        assert (
            tmp_path / "remote" / "snes" / "Super Metroid.srm"
        ).read_bytes() == b"changed"


    def test_unowned_unknown_group_is_skipped_not_broad_scanned(self, tmp_path: Path):
        provider = _Provider()
        service = _service(tmp_path, provider)
        _write(tmp_path / "remote" / "snes" / "Super Metroid.srm", b"never-tracked")

        coordinator = _coordinator(tmp_path, service)
        coordinator.game_start(
            system="snes", emulator="libretro", core="snes9x", rom="Super Metroid.sfc"
        )
        assert not (tmp_path / "local" / "snes" / "Super Metroid.srm").exists()
        payload = _session_payload(tmp_path, system="snes", rom="Super Metroid.sfc")
        assert payload["sync_outcome"] == "skipped"


class TestGameStartScopeAndSafety:
    def test_offline_mode_does_zero_remote_work(self, tmp_path: Path):
        provider = _Provider()
        capability_policy = CapabilityPolicy("smart_cache", OperatingMode.OFFLINE)
        service = _service(tmp_path, provider, capability_policy=capability_policy)
        _write(tmp_path / "local" / "snes" / "Super Metroid.srm", b"base")

        coordinator = _coordinator(tmp_path, service)
        coordinator.game_start(
            system="snes", emulator="libretro", core="snes9x", rom="Super Metroid.sfc"
        )  # must not raise
        payload = _session_payload(tmp_path, system="snes", rom="Super Metroid.sfc")
        assert payload["sync_outcome"] == "unresolved"

    def test_unsupported_system_does_no_savesync_work(self, tmp_path: Path):
        class _Unexpected:
            def __getattr__(self, name):
                raise AssertionError(f"unsupported system accessed service.{name}")

        coordinator = AutoSaveSyncCoordinator(
            _Unexpected(),  # type: ignore[arg-type]
            data_root=tmp_path / "data",
            enabled=True,
            policy=DEFAULT_SAVE_SELECTION_POLICY,
            quiet_seconds=0,
        )
        coordinator.game_start(
            system="totally-unknown-system", emulator="", core="", rom="Game.rom"
        )
        payload = _session_payload(tmp_path, system="totally-unknown-system", rom="Game.rom")
        assert payload["sync_outcome"] == "unsupported"

    def test_shared_container_layout_skips_rather_than_broad_scans(self, tmp_path: Path):
        provider = _Provider()
        service = _service(tmp_path, provider)

        coordinator = _coordinator(tmp_path, service)
        coordinator.game_start(
            system="ps2", emulator="pcsx2", core="pcsx2", rom="Game.iso"
        )
        payload = _session_payload(tmp_path, system="ps2", rom="Game.iso")
        assert payload["sync_outcome"] == "skipped"
        assert payload["sync_group_ids"] == []

    def test_no_broad_scan_only_target_layout_shard_is_fetched(
        self, tmp_path: Path, monkeypatch
    ):
        provider = _Provider()
        service = _service(tmp_path, provider)
        _write(tmp_path / "local" / "snes" / "Super Metroid.srm", b"base")
        _write(tmp_path / "local" / "megadrive" / "Sonic.srm", b"base")
        service.full_sync()

        seed_peer_commit(
            service,
            remote_root=tmp_path / "remote",
            relative_path="megadrive/Sonic.srm",
            content=b"unrelated-layout-changed",
        )

        calls: list[str] = []
        original = savesync_index.load_shard

        def _tracking(index_root, layout_id, layout_head):
            calls.append(layout_id)
            return original(index_root, layout_id, layout_head)

        monkeypatch.setattr(savesync_index, "load_shard", _tracking)
        coordinator = _coordinator(tmp_path, service)
        coordinator.game_start(
            system="snes", emulator="libretro", core="snes9x", rom="Super Metroid.sfc"
        )
        assert all(layout_id == "retroarch-root-snes" for layout_id in calls)
        assert (tmp_path / "local" / "megadrive" / "Sonic.srm").read_bytes() == b"base"

    def test_failed_sync_does_not_create_falsely_synchronized_state(
        self, tmp_path: Path, monkeypatch
    ):
        provider = _Provider()
        service = _service(tmp_path, provider)
        _write(tmp_path / "local" / "snes" / "Super Metroid.srm", b"base")
        service.full_sync()

        def _boom(self, group_layout_map):
            raise SaveSyncError("simulated failure")

        monkeypatch.setattr(SaveSyncService, "targeted_game_start_sync", _boom)
        coordinator = _coordinator(tmp_path, service)
        coordinator.game_start(
            system="snes", emulator="libretro", core="snes9x", rom="Super Metroid.sfc"
        )  # must not raise
        payload = _session_payload(tmp_path, system="snes", rom="Super Metroid.sfc")
        assert payload["sync_outcome"] == "unresolved"
        assert payload["sync_group_ids"] == ["retroarch-root-snes/super metroid"]

    def test_successful_prelaunch_sync_still_allows_correct_gamestop(self, tmp_path: Path):
        provider = _Provider()
        service = _service(tmp_path, provider)
        _write(tmp_path / "local" / "snes" / "Super Metroid.srm", b"base")
        service.full_sync()
        seed_peer_commit(
            service,
            remote_root=tmp_path / "remote",
            relative_path="snes/Super Metroid.srm",
            content=b"materialized-by-game-start",
        )
        coordinator = _coordinator(tmp_path, service)
        coordinator.game_start(
            system="snes", emulator="libretro", core="snes9x", rom="Super Metroid.sfc"
        )
        assert (
            tmp_path / "local" / "snes" / "Super Metroid.srm"
        ).read_bytes() == b"materialized-by-game-start"

        # Playing the game now edits the freshly-materialized save; gameStop
        # must still discover and upload exactly that new edit.
        _write(
            tmp_path / "local" / "snes" / "Super Metroid.srm", b"played-after-sync"
        )
        coordinator.game_stop(
            system="snes", emulator="libretro", core="snes9x", rom="Super Metroid.sfc"
        )
        assert (
            tmp_path / "remote" / "snes" / "Super Metroid.srm"
        ).read_bytes() == b"played-after-sync"
