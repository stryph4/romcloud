"""Index-driven Quick Sync (Step 4): candidate discovery on OWNED datasets.

The Metroid acceptance case this whole step exists for: device A advances a
save and commits it through the protocol; device B, with no local dirty
hint at all, must discover and reconcile exactly that group through the
remote index alone — never the bounded legacy journal, and never a Full
Sync.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from romcloud.core.capabilities import CapabilityPolicy, PresentationIntent
from romcloud.core.exceptions import CapabilityUnavailableError, SaveSyncVerificationError
from romcloud.core.storage import ProviderCapabilities, StorageProvider
from romcloud.infrastructure import save_tree, savesync_index, savesync_journal
from romcloud.services.saves import SaveSyncService

from tests.unit._savesync_protocol_helpers import (
    index_root_for,
    mutate_remote_out_of_band,
    seed_peer_commit,
    strip_protocol_ownership,
)


class _Provider(StorageProvider):
    @property
    def provider_id(self) -> str:
        return "test"

    @property
    def capabilities(self):
        return ProviderCapabilities(
            has_filesystem_semantics=True, supports_durable_transactions=True
        )

    def is_reachable(self, root: str) -> bool:
        return True

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


def _device(
    tmp_path: Path, name: str, *, capability_policy: CapabilityPolicy | None = None
) -> SaveSyncService:
    local = tmp_path / name / "local"
    local.mkdir(parents=True, exist_ok=True)
    return SaveSyncService(
        provider=_Provider(),
        connectivity_root=str(tmp_path / "remote"),
        local_root=str(local),
        remote_root=str(tmp_path / "remote"),
        state_path=tmp_path / name / "data" / "savesync-state.json",
        capability_policy=capability_policy,
    )


class TestEmptyFastPath:
    def test_unchanged_head_and_no_local_work_scans_nothing(
        self, tmp_path: Path, monkeypatch
    ):
        device = _device(tmp_path, "a")
        _write(tmp_path / "a" / "local" / "snes" / "Metroid.srm", b"base")
        device.full_sync()
        # The very first Quick Sync after a Full Sync still primes this
        # device's own index watermark (one cheap shard-fetch pass, no
        # filesystem scan); the fast path applies from the second call on.
        primed = device.quick_sync()
        assert primed.status == "unchanged"

        def fail(*args, **kwargs):
            raise AssertionError("empty fast path must not scan or fetch anything")

        monkeypatch.setattr(device, "_scan_local_layouts", fail)
        monkeypatch.setattr(device, "_scan_remote_layouts", fail)
        monkeypatch.setattr(device, "_scan_automatic_local", fail)
        monkeypatch.setattr(device, "_scan_automatic_remote", fail)
        monkeypatch.setattr(savesync_index, "load_shard", lambda *a, **k: fail())

        result = device.quick_sync()

        assert result.status == "unchanged"
        assert result.reason == "index-current-local-materialized"

    def test_repeated_calls_stay_on_the_fast_path(self, tmp_path: Path):
        device = _device(tmp_path, "a")
        _write(tmp_path / "a" / "local" / "snes" / "Metroid.srm", b"base")
        device.full_sync()

        first = device.quick_sync()
        second = device.quick_sync()

        assert first.status == "unchanged"
        assert second.status == "unchanged"
        assert first.remote_generation == second.remote_generation


class TestPeerAdvanceWithNoLocalDirtyHint:
    def test_metroid_acceptance_case(self, tmp_path: Path, monkeypatch):
        """Device A advances Metroid and commits; device B has no dirty
        hint at all. Quick Sync must discover it through the index and
        reconcile only that group — no Full Sync."""
        device_a = _device(tmp_path, "device-a")
        device_b = _device(tmp_path, "device-b")
        _write(tmp_path / "device-a" / "local" / "snes" / "Metroid.srm", b"save-v1")
        device_a.full_sync()
        device_b.full_sync()  # device B joins the already-OWNED dataset

        seed_peer_commit(
            device_a,
            remote_root=tmp_path / "remote",
            relative_path="snes/Metroid.srm",
            content=b"save-v2-from-device-a",
            device_id="device-a",
        )

        reads = []
        original = save_tree.hash_file
        monkeypatch.setattr(
            save_tree, "hash_file", lambda p: (reads.append(Path(p)), original(p))[1]
        )

        result = device_b.quick_sync()

        assert result.status == "reconciled"
        assert result.report is not None
        assert result.report.downloaded == 1
        assert (
            tmp_path / "device-b" / "local" / "snes" / "Metroid.srm"
        ).read_bytes() == b"save-v2-from-device-a"
        assert result.processed_groups == ("retroarch-root-snes/metroid",)

    def test_peer_advance_is_detected_with_zero_local_dirty_state(self, tmp_path: Path):
        device_a = _device(tmp_path, "device-a")
        device_b = _device(tmp_path, "device-b")
        _write(tmp_path / "device-a" / "local" / "psx" / "Game.srm", b"base")
        device_a.full_sync()
        device_b.full_sync()

        state_before = device_b.get_state()
        assert all(
            group.condition.value == "clean" and not group.dirty_path_hints
            for group in state_before.groups
        )

        seed_peer_commit(
            device_a,
            remote_root=tmp_path / "remote",
            relative_path="psx/Game.srm",
            content=b"peer-progress",
            device_id="device-a",
        )
        result = device_b.quick_sync()

        assert result.status == "reconciled"
        assert (
            tmp_path / "device-b" / "local" / "psx" / "Game.srm"
        ).read_bytes() == b"peer-progress"


class TestRemoteOnlyNewArtifact:
    def test_brand_new_remote_only_group_is_discovered_through_the_shard(
        self, tmp_path: Path
    ):
        """The Step-1 gap: a group neither locally present nor previously
        baselined must still be found, using the index's own artifact list
        rather than local/baseline knowledge."""
        device_a = _device(tmp_path, "device-a")
        device_b = _device(tmp_path, "device-b")
        _write(tmp_path / "device-a" / "local" / "snes" / "Existing.srm", b"existing")
        device_a.full_sync()
        device_b.full_sync()

        # A brand-new save device B has never seen in any form.
        seed_peer_commit(
            device_a,
            remote_root=tmp_path / "remote",
            relative_path="snes/NeverSeenBefore.srm",
            content=b"first-appearance",
            device_id="device-a",
        )

        result = device_b.quick_sync()

        assert result.status == "reconciled"
        assert result.report is not None
        assert result.report.downloaded == 1
        assert (
            tmp_path / "device-b" / "local" / "snes" / "NeverSeenBefore.srm"
        ).read_bytes() == b"first-appearance"


class TestUnrelatedGroupIsolation:
    def test_only_the_advanced_group_in_the_layout_is_touched(
        self, tmp_path: Path, monkeypatch
    ):
        device_a = _device(tmp_path, "device-a")
        device_b = _device(tmp_path, "device-b")
        _write(tmp_path / "device-a" / "local" / "snes" / "GameA.srm", b"a-base")
        _write(tmp_path / "device-a" / "local" / "snes" / "GameB.srm", b"b-base")
        device_a.full_sync()
        device_b.full_sync()

        seed_peer_commit(
            device_a,
            remote_root=tmp_path / "remote",
            relative_path="snes/GameA.srm",
            content=b"a-changed",
            device_id="device-a",
        )

        reads: list[Path] = []
        original = save_tree.hash_file
        monkeypatch.setattr(
            save_tree, "hash_file", lambda p: (reads.append(Path(p)), original(p))[1]
        )

        result = device_b.quick_sync()

        assert result.status == "reconciled"
        game_b_remote = tmp_path / "remote" / "snes" / "GameB.srm"
        # Only remote scanning is narrowed (Step 1); the local layout scan
        # remains broad by design, so GameB's local file is expected to be
        # read — what must never happen is a remote read for it.
        assert game_b_remote not in reads
        assert (
            tmp_path / "device-b" / "local" / "snes" / "GameB.srm"
        ).read_bytes() == b"b-base"


class TestMultipleAdvancedGroups:
    def test_multiple_remotely_advanced_groups_are_all_selected(self, tmp_path: Path):
        device_a = _device(tmp_path, "device-a")
        device_b = _device(tmp_path, "device-b")
        _write(tmp_path / "device-a" / "local" / "snes" / "GameA.srm", b"a-base")
        _write(tmp_path / "device-a" / "local" / "psx" / "GameB.srm", b"b-base")
        device_a.full_sync()
        device_b.full_sync()

        seed_peer_commit(
            device_a, remote_root=tmp_path / "remote",
            relative_path="snes/GameA.srm", content=b"a-changed", device_id="device-a",
        )
        seed_peer_commit(
            device_a, remote_root=tmp_path / "remote",
            relative_path="psx/GameB.srm", content=b"b-changed", device_id="device-a",
        )

        result = device_b.quick_sync()

        assert result.status == "reconciled"
        assert set(result.processed_groups) == {
            "retroarch-root-snes/gamea",
            "retroarch-root-psx/gameb",
        }
        assert (
            tmp_path / "device-b" / "local" / "snes" / "GameA.srm"
        ).read_bytes() == b"a-changed"
        assert (
            tmp_path / "device-b" / "local" / "psx" / "GameB.srm"
        ).read_bytes() == b"b-changed"


class TestLocalAndRemoteUnion:
    def test_local_dirty_and_remote_advanced_are_both_processed_together(
        self, tmp_path: Path
    ):
        device_a = _device(tmp_path, "device-a")
        device_b = _device(tmp_path, "device-b")
        _write(tmp_path / "device-a" / "local" / "snes" / "GameA.srm", b"a-base")
        _write(tmp_path / "device-a" / "local" / "psx" / "GameB.srm", b"b-base")
        device_a.full_sync()
        device_b.full_sync()

        # Device B has its own unsynced local change...
        b_local = tmp_path / "device-b" / "local" / "snes" / "GameA.srm"
        _write(b_local, b"b-own-change")
        device_b.mark_local_dirty("snes/GameA.srm")
        # ...while device A independently advanced a different group.
        seed_peer_commit(
            device_a, remote_root=tmp_path / "remote",
            relative_path="psx/GameB.srm", content=b"peer-change", device_id="device-a",
        )

        result = device_b.quick_sync()

        assert result.status == "reconciled"
        assert set(result.processed_groups) == {
            "retroarch-root-snes/gamea",
            "retroarch-root-psx/gameb",
        }
        assert (
            tmp_path / "remote" / "snes" / "GameA.srm"
        ).read_bytes() == b"b-own-change"
        assert (
            tmp_path / "device-b" / "local" / "psx" / "GameB.srm"
        ).read_bytes() == b"peer-change"


class TestSameGroupDivergenceReachesConflictSemantics:
    def test_local_and_remote_both_changed_becomes_a_conflict_not_an_overwrite(
        self, tmp_path: Path
    ):
        device_a = _device(tmp_path, "device-a")
        device_b = _device(tmp_path, "device-b")
        _write(tmp_path / "device-a" / "local" / "snes" / "Game.srm", b"base")
        device_a.full_sync()
        device_b.full_sync()

        seed_peer_commit(
            device_a, remote_root=tmp_path / "remote",
            relative_path="snes/Game.srm", content=b"peer-change", device_id="device-a",
        )
        b_local = tmp_path / "device-b" / "local" / "snes" / "Game.srm"
        _write(b_local, b"device-b-own-change")
        device_b.mark_local_dirty("snes/Game.srm")

        result = device_b.quick_sync()

        assert result.status == "reconciled"
        assert result.report is not None
        assert result.report.conflicts == 1
        assert device_b.get_state().active_conflicts
        # Neither side was silently overwritten.
        assert b_local.read_bytes() == b"device-b-own-change"
        assert (tmp_path / "remote" / "snes" / "Game.srm").read_bytes() == b"peer-change"


class TestUnownedQuickSyncUnaffected:
    def test_unowned_dataset_never_takes_the_index_driven_path(
        self, tmp_path: Path, monkeypatch
    ):
        device = _device(tmp_path, "a")
        _write(tmp_path / "a" / "local" / "psx" / "Game.srm", b"base")
        device.full_sync()
        strip_protocol_ownership(tmp_path / "remote", tmp_path / "a" / "data")

        monkeypatch.setattr(
            device,
            "_quick_sync_owned",
            lambda *a, **k: (_ for _ in ()).throw(
                AssertionError("UNOWNED must never use index-driven discovery")
            ),
        )

        journal_path = savesync_journal.default_journal_path(tmp_path / "remote")
        savesync_journal.append_mutations(
            journal_path,
            device_id="peer",
            revision="r2",
            timestamp="2026-01-01T00:00:01+00:00",
            mutations=[
                {
                    "system": "psx",
                    "layout_id": "retroarch-root-psx",
                    "group_id": "retroarch-root-psx/game",
                    "object_id": "psx/Game.srm",
                    "operation": "update",
                }
            ],
        )

        result = device.quick_sync()

        assert result.status == "reconciled"


class TestDamagedDatasetFailsClosed:
    def test_quick_sync_refuses_and_mutates_nothing(self, tmp_path: Path):
        device = _device(tmp_path, "a")
        local = tmp_path / "a" / "local" / "psx" / "Game.srm"
        _write(local, b"base")
        device.full_sync()
        savesync_index.head_path(index_root_for(tmp_path / "remote")).unlink()

        _write(local, b"changed")
        device.mark_local_dirty("psx/Game.srm")

        with pytest.raises(SaveSyncVerificationError, match="damaged"):
            device.quick_sync()

        assert (tmp_path / "remote" / "psx" / "Game.srm").read_bytes() == b"base"


class TestJournalIndexDivergenceOwned:
    def test_old_writer_journal_advance_requires_full_sync(self, tmp_path: Path):
        """An OWNED dataset whose journal advanced without a matching index
        rebuild is old-writer/out-of-band evidence, never guessed past."""
        device = _device(tmp_path, "a")
        local = tmp_path / "a" / "local" / "psx" / "Game.srm"
        _write(local, b"base")
        device.full_sync()

        savesync_journal.append_mutations(
            savesync_journal.default_journal_path(tmp_path / "remote"),
            device_id="old-client",
            revision="legacy-write",
            timestamp="2026-08-22T00:00:00+00:00",
            mutations=[
                {
                    "system": "psx",
                    "layout_id": "retroarch-root-psx",
                    "group_id": "retroarch-root-psx/game",
                    "object_id": "psx/Game.srm",
                    "operation": "update",
                }
            ],
        )
        _write(local, b"changed")
        device.mark_local_dirty("psx/Game.srm")

        result = device.quick_sync()

        assert result.status == "requires-full-sync"
        assert result.reason == "journal-index-divergence"
        # Nothing was mutated or committed.
        assert (tmp_path / "remote" / "psx" / "Game.srm").read_bytes() == b"base"

    def test_diverged_journal_alone_requires_full_sync_with_zero_scans(
        self, tmp_path: Path, monkeypatch
    ):
        """Unchanged HEAD, no local pending work at all: the empty fast path
        must still perform the minimum metadata check needed to catch an
        old writer that appended to the journal without republishing the
        index — while still never scanning a SaveLayout, fetching a shard,
        or hashing/staging anything."""
        device = _device(tmp_path, "a")
        local = tmp_path / "a" / "local" / "psx" / "Game.srm"
        _write(local, b"base")
        device.full_sync()
        primed = device.quick_sync()
        assert primed.status == "unchanged"

        # An old, non-participating writer mutates remote payload and
        # appends the legacy journal without touching the authoritative
        # index at all.
        mutate_remote_out_of_band(tmp_path / "remote", "psx/Game.srm", b"old-writer-edit")
        savesync_journal.append_mutations(
            savesync_journal.default_journal_path(tmp_path / "remote"),
            device_id="old-client",
            revision="legacy-write",
            timestamp="2026-08-22T00:00:00+00:00",
            mutations=[
                {
                    "system": "psx",
                    "layout_id": "retroarch-root-psx",
                    "group_id": "retroarch-root-psx/game",
                    "object_id": "psx/Game.srm",
                    "operation": "update",
                }
            ],
        )

        def fail(*args, **kwargs):
            raise AssertionError("journal-only divergence check must not scan anything")

        monkeypatch.setattr(device, "_scan_local_layouts", fail)
        monkeypatch.setattr(device, "_scan_remote_layouts", fail)
        monkeypatch.setattr(device, "_scan_automatic_local", fail)
        monkeypatch.setattr(device, "_scan_automatic_remote", fail)
        monkeypatch.setattr(savesync_index, "load_shard", lambda *a, **k: fail())

        result = device.quick_sync()

        assert result.status == "requires-full-sync"
        assert result.reason == "journal-index-divergence"
        assert (
            tmp_path / "remote" / "psx" / "Game.srm"
        ).read_bytes() == b"old-writer-edit"

    def test_failed_reconcile_does_not_advance_the_index_watermark(
        self, tmp_path: Path, monkeypatch
    ):
        device_a = _device(tmp_path, "device-a")
        device_b = _device(tmp_path, "device-b")
        _write(tmp_path / "device-a" / "local" / "psx" / "Game.srm", b"base")
        device_a.full_sync()
        device_b.full_sync()
        seed_peer_commit(
            device_a, remote_root=tmp_path / "remote",
            relative_path="psx/Game.srm", content=b"peer-change", device_id="device-a",
        )
        watermark_before = device_b._load_index_watermark(
            dataset_id=savesync_index.load_head_strict(
                index_root_for(tmp_path / "remote")
            ).dataset_id
        )
        monkeypatch.setattr(
            device_b,
            "_reconcile",
            lambda **kwargs: (_ for _ in ()).throw(SaveSyncVerificationError("boom")),
        )

        with pytest.raises(SaveSyncVerificationError):
            device_b.quick_sync()

        watermark_after = device_b._load_index_watermark(dataset_id=watermark_before.dataset_id)
        assert watermark_after.index_generation == watermark_before.index_generation
        assert watermark_after.layout_generations == watermark_before.layout_generations


class TestOfflineMakesNoRemoteCalls:
    def test_offline_capability_blocks_before_any_dataset_resolution(
        self, tmp_path: Path, monkeypatch
    ):
        offline_policy = CapabilityPolicy("smart_cache", PresentationIntent.OFFLINE)
        device = _device(tmp_path, "a", capability_policy=offline_policy)
        monkeypatch.setattr(
            device,
            "_resolve_dataset_state",
            lambda *a, **k: (_ for _ in ()).throw(
                AssertionError("offline must never resolve dataset state")
            ),
        )

        with pytest.raises(CapabilityUnavailableError):
            device.quick_sync()


class TestQuickNeverBroadScansAsFallback:
    def test_owned_group_candidate_always_uses_a_narrow_remote_scan(
        self, tmp_path: Path, monkeypatch
    ):
        device_a = _device(tmp_path, "device-a")
        device_b = _device(tmp_path, "device-b")
        _write(tmp_path / "device-a" / "local" / "snes" / "GameA.srm", b"a-base")
        _write(tmp_path / "device-a" / "local" / "snes" / "GameB.srm", b"b-base")
        device_a.full_sync()
        device_b.full_sync()
        seed_peer_commit(
            device_a, remote_root=tmp_path / "remote",
            relative_path="snes/GameA.srm", content=b"a-changed", device_id="device-a",
        )

        scopes: list[object] = []
        original = device_b._scan_remote_layouts

        def spy(layout_ids, *, only_relative_paths=None):
            scopes.append(only_relative_paths)
            return original(layout_ids, only_relative_paths=only_relative_paths)

        monkeypatch.setattr(device_b, "_scan_remote_layouts", spy)

        result = device_b.quick_sync()

        assert result.status == "reconciled"
        assert scopes
        assert all(scope is not None for scope in scopes)


class TestFullSyncStillFindsOutOfBandChanges:
    def test_full_sync_discovers_a_manual_edit_after_cutover(self, tmp_path: Path):
        device = _device(tmp_path, "a")
        local = tmp_path / "a" / "local" / "psx" / "Game.srm"
        _write(local, b"base")
        device.full_sync()

        mutate_remote_out_of_band(tmp_path / "remote", "psx/Game.srm", b"manual-edit")

        report = device.full_sync()

        assert report is not None
        assert local.read_bytes() == b"manual-edit"
        head = savesync_index.load_head_strict(index_root_for(tmp_path / "remote"))
        shard = savesync_index.load_shard(
            index_root_for(tmp_path / "remote"),
            "retroarch-root-psx",
            head.layouts["retroarch-root-psx"],
        )
        group = next(iter(shard.groups))
        assert group.artifacts[0].sha256 == hashlib.sha256(b"manual-edit").hexdigest()
