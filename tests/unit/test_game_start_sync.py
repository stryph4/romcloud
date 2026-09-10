"""Step 5: targeted, best-effort ``gameStart`` synchronization.

SaveSync must never hold the game hostage: every scenario here asserts that
``AutoSaveSyncCoordinator.game_start`` returns normally (never raises) no
matter what the underlying targeted sync attempt discovers, while also
verifying the narrow scope contract — only the launched game's own
deterministically-resolved save group is ever touched, never a broader scan.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from romcloud.core.capabilities import CapabilityPolicy, OperatingMode
from romcloud.core.exceptions import SaveSyncError
from romcloud.core.save_selection import DEFAULT_SAVE_SELECTION_POLICY, SaveSelectionPolicy
from romcloud.core.storage import ProviderCapabilities, StorageProvider
from romcloud.infrastructure import diagnostics, savesync_index, savesync_prompts
from romcloud.infrastructure.ps1_memory_card import (
    BLOCK_SIZE,
    CARD_SIZE,
    FRAME_SIZE,
    Ps1RawMemoryCardAdapter,
)
from romcloud.services.auto_savesync import AutoSaveSyncCoordinator, layout_ids_for_session
from romcloud.services.saves import SaveSyncService

from tests.unit._savesync_protocol_helpers import (
    index_root_for,
    mutate_remote_out_of_band,
    publish_current_remote_as_peer,
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


def _coordinator(
    tmp_path: Path,
    service: SaveSyncService,
    *,
    policy: SaveSelectionPolicy = DEFAULT_SAVE_SELECTION_POLICY,
) -> AutoSaveSyncCoordinator:
    return AutoSaveSyncCoordinator(
        service,
        data_root=tmp_path / "data",
        enabled=True,
        policy=policy,
        quiet_seconds=0,
    )


def _whole_layout_policy(layout_id: str) -> SaveSelectionPolicy:
    """A structural single-container invariant for *layout_id*, for testing.

    Mirrors ``test_save_containers._whole_layout_policy``: only ``group_by``
    changes, so the layout's own container adapter/kind (and therefore its
    existing format-aware merge semantics) are left completely untouched.
    """
    return SaveSelectionPolicy(
        layouts=tuple(
            replace(layout, group_by="layout")
            if layout.layout_id == layout_id
            else layout
            for layout in DEFAULT_SAVE_SELECTION_POLICY.layouts
        )
    )


def _checksum(frame: bytearray) -> None:
    value = 0
    for byte in frame[:127]:
        value ^= byte
    frame[127] = value


def _ps1_card(entries: list[tuple[bytes, tuple[int, ...], bytes]]) -> bytes:
    """Copied from test_save_containers._ps1_card: a minimal valid raw PS1
    memory card image with one commercial-namespace entry per domain."""
    raw = bytearray(b"\xff" * CARD_SIZE)
    header = bytearray(FRAME_SIZE)
    header[:2] = b"MC"
    _checksum(header)
    raw[:FRAME_SIZE] = header
    for block in range(1, 16):
        frame = bytearray(FRAME_SIZE)
        frame[0] = 0xA0
        frame[8:10] = b"\xff\xff"
        _checksum(frame)
        raw[block * FRAME_SIZE : (block + 1) * FRAME_SIZE] = frame
    for filename, blocks, fill in entries:
        for index, block in enumerate(blocks):
            frame = bytearray(FRAME_SIZE)
            frame[0] = 0x51 if index == 0 else (0x53 if index == len(blocks) - 1 else 0x52)
            if index == 0:
                frame[4:8] = (len(blocks) * BLOCK_SIZE).to_bytes(4, "little")
                frame[10 : 10 + len(filename)] = filename
            frame[8:10] = (
                b"\xff\xff"
                if index == len(blocks) - 1
                else (blocks[index + 1] - 1).to_bytes(2, "little")
            )
            _checksum(frame)
            raw[block * FRAME_SIZE : (block + 1) * FRAME_SIZE] = frame
            raw[block * BLOCK_SIZE : (block + 1) * BLOCK_SIZE] = fill * BLOCK_SIZE
    return bytes(raw)


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


class _Progress:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def stage(self, text: str) -> None:
        self.calls.append(("stage", text))

    def close(self, ok: bool, message=None) -> None:
        self.calls.append(("close", ok, message))

    @property
    def stages(self) -> list[str]:
        return [call[1] for call in self.calls if call[0] == "stage"]


class TestGameStartOwnedDataset:
    def test_targeted_download_uses_shared_lifecycle_progress_phases(
        self, tmp_path: Path
    ):
        provider = _Provider()
        service = _service(tmp_path, provider)
        local = tmp_path / "local" / "snes" / "Super Metroid.srm"
        _write(local, b"base")
        service.full_sync()
        seed_peer_commit(
            service,
            remote_root=tmp_path / "remote",
            relative_path="snes/Super Metroid.srm",
            content=b"remote-edit",
        )
        progress = _Progress()

        conflict_ids = _coordinator(tmp_path, service).game_start(
            system="snes",
            emulator="libretro",
            core="snes9x",
            rom="Super Metroid.sfc",
            progress=progress,
        )

        assert conflict_ids == ()
        assert progress.stages == [
            "Checking save…",
            "Checking remote state…",
            "Comparing save versions…",
            "Downloading save…",
            "Verifying save…",
        ]
        assert progress.calls[-1] == ("close", True, "Save is current.")

    def test_absent_baseline_divergence_is_unresolved_conflict(
        self, tmp_path: Path, caplog
    ):
        """A successful reconcile operation can still leave a conflict."""
        provider = _Provider()
        service = _service(tmp_path, provider)
        # Establish protocol ownership while this target group is absent, so
        # its three-way baseline is genuinely absent on both devices.
        service.full_sync()
        local = tmp_path / "local" / "snes" / "Super Metroid.srm"
        remote = tmp_path / "remote" / "snes" / "Super Metroid.srm"
        _write(local, b"local-without-baseline")
        service.mark_local_dirty("snes/Super Metroid.srm")
        seed_peer_commit(
            service,
            remote_root=tmp_path / "remote",
            relative_path="snes/Super Metroid.srm",
            content=b"remote-without-baseline",
        )

        coordinator = _coordinator(tmp_path, service)
        with caplog.at_level("INFO"):
            conflict_ids = coordinator.game_start(
                system="snes",
                emulator="libretro",
                core="snes9x",
                rom="Super Metroid.sfc",
            )

        report = service.get_state().last_reconcile
        assert report is not None
        assert (report.uploaded, report.downloaded, report.conflicts) == (0, 0, 1)
        assert local.read_bytes() == b"local-without-baseline"
        assert remote.read_bytes() == b"remote-without-baseline"
        payload = _session_payload(
            tmp_path, system="snes", rom="Super Metroid.sfc"
        )
        assert payload["sync_outcome"] == "unresolved"
        assert "status=unresolved reason=conflict" in caplog.text
        assert conflict_ids == tuple(
            item.conflict_id for item in service.get_state().active_conflicts
        )

    def test_old_target_conflict_is_requeued_and_presentable_on_each_launch(
        self, tmp_path: Path
    ):
        provider = _Provider()
        service = _service(tmp_path, provider)
        local = tmp_path / "local" / "snes" / "Super Metroid.srm"
        remote = tmp_path / "remote" / "snes" / "Super Metroid.srm"
        _write(local, b"base")
        service.full_sync()
        _write(local, b"local-edit")
        service.mark_local_dirty("snes/Super Metroid.srm")
        seed_peer_commit(
            service,
            remote_root=tmp_path / "remote",
            relative_path="snes/Super Metroid.srm",
            content=b"remote-edit",
        )
        service.quick_sync()
        conflict_id = service.get_state().active_conflicts[0].conflict_id
        assert savesync_prompts.pending_ids(tmp_path / "data") == ()

        coordinator = _coordinator(tmp_path, service)
        assert coordinator.game_start(
            system="snes",
            emulator="libretro",
            core="snes9x",
            rom="Super Metroid.sfc",
        ) == (conflict_id,)
        assert savesync_prompts.pending_ids(tmp_path / "data") == (conflict_id,)

        # Resolve Later removes presentation bookkeeping only. The next
        # launch restores the same unresolved conflict to the exact-ID queue.
        savesync_prompts.complete(tmp_path / "data", conflict_id)
        assert coordinator.game_start(
            system="snes",
            emulator="libretro",
            core="snes9x",
            rom="Super Metroid.sfc",
        ) == (conflict_id,)
        assert savesync_prompts.pending_ids(tmp_path / "data") == (conflict_id,)

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

    def test_targeted_remote_newer_exposes_phase_timings_and_io_counts(
        self, tmp_path: Path
    ):
        provider = _Provider()
        service = _service(tmp_path, provider)
        local = tmp_path / "local/snes/Super Metroid.srm"
        _write(local, b"base")
        service.full_sync()
        seed_peer_commit(
            service,
            remote_root=tmp_path / "remote",
            relative_path="snes/Super Metroid.srm",
            content=b"peer-advanced",
        )

        with diagnostics.operation("targeted gameStart", subsystem="savesync"):
            result = service.targeted_game_start_sync(
                {"retroarch-root-snes/super metroid": "retroarch-root-snes"}
            )
            timing = diagnostics.current_timing_snapshot()

        assert result.report is not None and result.report.downloaded == 1
        assert {
            "remote-readiness",
            "protocol-ownership-resolution",
            "head-read",
            "layout-shard-read",
            "journal-generation-check",
            "scan-local",
            "scan-remote",
            "reconciliation-planning",
            "staging",
            "payload-transfer",
            "staging-verify",
            "staging-verify-remote",
            "transaction-apply",
            "promotion-materialization",
            "final-verify",
            "final-verify-remote",
            "local-state-persistence",
        }.issubset(timing["stages"])
        assert timing["counters"]["head_reads"] >= 1
        assert timing["counters"]["shard_reads"] >= 1
        assert timing["counters"]["payload_opens"] >= 1
        assert timing["counters"]["payload_read_calls"] >= 1
        assert timing["counters"]["payload_stats"] >= 1
        assert timing["counters"]["remote_manifest_observations"] >= 1
        assert timing["counters"]["verification_passes"] >= 1

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

    def test_ambiguous_multi_container_layouts_are_skipped(self, tmp_path: Path):
        """No layout reachable for a plain ps2/pcsx2 launch has a provable
        single-container invariant via ``group_id_for_rom`` (all four
        card/folder layouts permit more than one physical container) and
        none of *those* use group_by="layout" in the shipped registry, so
        gameStart must do nothing for them regardless of how many/few card
        files happen to exist locally right now."""
        provider = _Provider()
        service = _service(tmp_path, provider)
        card = tmp_path / "local" / "ps2" / "pcsx2" / "Mcd001.ps2"
        _write(card, b"one-card-present")

        coordinator = _coordinator(tmp_path, service)
        coordinator.game_start(
            system="ps2", emulator="pcsx2", core="pcsx2", rom="Game.iso"
        )
        assert not (tmp_path / "remote" / "ps2" / "pcsx2" / "Mcd001.ps2").exists()
        payload = _session_payload(tmp_path, system="ps2", rom="Game.iso")
        assert "pcsx2-legacy-memory-cards/mcd001" not in payload["sync_group_ids"]
        assert "pcsx2-memory-cards/mcd001" not in payload["sync_group_ids"]

    def test_layout_wide_shared_domain_reconciles_before_launch(self, tmp_path: Path):
        """``pcsx2-states``/``pcsx2-legacy-states`` are shared *and*
        group_by="layout" in the shipped registry: the whole layout is
        *defined* as one save group by the SaveLayout contract itself, a
        structural invariant independent of what exists on disk."""
        provider = _Provider()
        service = _service(tmp_path, provider)
        state_file = tmp_path / "local" / "ps2" / "pcsx2" / "sstates" / "slot1.p2s"
        _write(state_file, b"base")
        service.full_sync()
        _write(state_file, b"changed")
        service.mark_local_dirty("ps2/pcsx2/sstates/slot1.p2s")

        coordinator = _coordinator(tmp_path, service)
        coordinator.game_start(
            system="ps2", emulator="pcsx2", core="pcsx2", rom="Game.iso"
        )

        assert (
            tmp_path / "remote" / "ps2" / "pcsx2" / "sstates" / "slot1.p2s"
        ).read_bytes() == b"changed"
        payload = _session_payload(tmp_path, system="ps2", rom="Game.iso")
        assert "pcsx2-states/dataset" in payload["sync_group_ids"]

    def test_unrelated_multi_container_layout_left_untouched(self, tmp_path: Path):
        """Reconciling the layout-wide shared domain must never widen to an
        unrelated, genuinely-ambiguous multi-container layout in the same
        launch."""
        provider = _Provider()
        service = _service(tmp_path, provider)
        state_file = tmp_path / "local" / "ps2" / "pcsx2" / "sstates" / "slot1.p2s"
        card_file = tmp_path / "local" / "ps2" / "pcsx2" / "Mcd001.ps2"
        _write(state_file, b"base")
        _write(card_file, b"card-base")
        service.full_sync()
        _write(state_file, b"changed")
        service.mark_local_dirty("ps2/pcsx2/sstates/slot1.p2s")
        _write(card_file, b"card-changed")
        service.mark_local_dirty("ps2/pcsx2/Mcd001.ps2")

        coordinator = _coordinator(tmp_path, service)
        coordinator.game_start(
            system="ps2", emulator="pcsx2", core="pcsx2", rom="Game.iso"
        )

        assert (
            tmp_path / "remote" / "ps2" / "pcsx2" / "sstates" / "slot1.p2s"
        ).read_bytes() == b"changed"
        assert (
            tmp_path / "remote" / "ps2" / "pcsx2" / "Mcd001.ps2"
        ).read_bytes() == b"card-base"
        assert any(
            group.dirty_path_hints
            for group in service.get_state().groups
            if group.layout_id == "pcsx2-memory-cards"
        )

    def test_layout_scoped_shared_container_reuses_existing_ps1_merge(
        self, tmp_path: Path
    ):
        """If a shared PS1-card-style layout *did* declare a structural
        single-container invariant (group_by="layout"), gameStart reconciles
        that whole container — and the byte-level per-domain PS1 merge
        (keyed by physical ``container_id``, independent of ``group_by``)
        is the exact unmodified adapter used everywhere else, not a
        gameStart-specific shortcut."""
        policy = _whole_layout_policy("duckstation-memory-cards")
        provider = _Provider()
        service = SaveSyncService(
            provider=provider,
            connectivity_root=str(tmp_path / "remote-data"),
            local_root=str(tmp_path / "local"),
            remote_root=str(tmp_path / "remote"),
            state_path=tmp_path / "data" / "savesync-state.json",
            policy=policy,
        )
        local = tmp_path / "local" / "duckstation" / "memcards" / "card.mcd"
        local.parent.mkdir(parents=True)
        name_a = b"BASLUS-00001SAVE"
        name_b = b"BESCES-00002SAVE"
        local.write_bytes(_ps1_card([(name_a, (1,), b"A"), (name_b, (2,), b"B")]))
        service.commit_upload(service.preview_upload())
        service.full_sync()

        remote = tmp_path / "remote" / "duckstation" / "memcards" / "card.mcd"
        remote.write_bytes(_ps1_card([(name_a, (1,), b"A"), (name_b, (2,), b"R")]))
        publish_current_remote_as_peer(service, remote_root=tmp_path / "remote")

        coordinator = _coordinator(tmp_path, service, policy=policy)
        coordinator.game_start(
            system="psx", emulator="duckstation", core="duckstation", rom="Any Game.chd"
        )

        adapter = Ps1RawMemoryCardAdapter()
        local_snapshot = adapter.snapshot(local, container_id="card")
        remote_snapshot = adapter.snapshot(remote, container_id="card")
        # Domain B (an unrelated game's own save inside the *same* physical
        # card) merged in via the existing adapter; domain A never moved.
        assert local_snapshot.entries == remote_snapshot.entries
        payload = _session_payload(tmp_path, system="psx", rom="Any Game.chd")
        assert payload["sync_outcome"] == "synchronized"
        assert "duckstation-memory-cards/dataset" in payload["sync_group_ids"]

    def test_opaque_malformed_container_remains_conservative(self, tmp_path: Path):
        """A layout-wide shared domain whose physical card the adapter
        cannot parse must fail exactly as conservatively as it already does
        for gameStop/Quick Sync — gameStart must not weaken that, and must
        still never block the launch."""
        policy = _whole_layout_policy("duckstation-memory-cards")
        provider = _Provider()
        service = SaveSyncService(
            provider=provider,
            connectivity_root=str(tmp_path / "remote-data"),
            local_root=str(tmp_path / "local"),
            remote_root=str(tmp_path / "remote"),
            state_path=tmp_path / "data" / "savesync-state.json",
            policy=policy,
        )
        local = tmp_path / "local" / "duckstation" / "memcards" / "card.mcd"
        local.parent.mkdir(parents=True)
        local.write_bytes(b"not a valid 128 KiB PS1 card image")

        coordinator = _coordinator(tmp_path, service, policy=policy)
        coordinator.game_start(
            system="psx", emulator="duckstation", core="duckstation", rom="Any Game.chd"
        )  # must not raise

        payload = _session_payload(tmp_path, system="psx", rom="Any Game.chd")
        assert payload["sync_outcome"] in {"unresolved", "skipped"}
        assert local.read_bytes() == b"not a valid 128 KiB PS1 card image"
        assert not (tmp_path / "remote" / "duckstation" / "memcards" / "card.mcd").exists()

    def test_resolution_failure_records_unresolved_not_skipped(
        self, tmp_path: Path, monkeypatch
    ):
        provider = _Provider()
        service = _service(tmp_path, provider)

        def _boom(self, layout_id):
            raise RuntimeError("simulated resolver failure")

        monkeypatch.setattr(
            "romcloud.core.save_selection.SaveSelectionPolicy.shared_container_group_id",
            _boom,
        )
        coordinator = _coordinator(tmp_path, service)
        coordinator.game_start(
            system="ps2", emulator="pcsx2", core="pcsx2", rom="Game.iso"
        )  # must not raise
        payload = _session_payload(tmp_path, system="ps2", rom="Game.iso")
        assert payload["sync_outcome"] == "unresolved"

    def test_xbox_hdd_layout_is_lifecycle_disabled_and_never_targeted(
        self, tmp_path: Path
    ):
        """The opt-in xemu/Xbox HDD layout (group_by="layout", a trivially
        provable single-container invariant on its own) is nonetheless
        never reachable from gameStart at all: it is
        ``lifecycle_enabled=False`` in the registry, so
        ``layout_ids_for_session`` never returns it regardless of resolver
        logic. It receives no automatic pre-launch sync."""
        xbox_layout = DEFAULT_SAVE_SELECTION_POLICY.layout("xemu-hdd")
        assert xbox_layout.group_by == "layout"
        assert xbox_layout.lifecycle_enabled is False
        assert "xemu-hdd" not in layout_ids_for_session(
            DEFAULT_SAVE_SELECTION_POLICY, xbox_layout.system, "xemu", "xemu"
        )

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
