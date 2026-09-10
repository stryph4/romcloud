"""Multi-device race and crash-recovery tests for the SaveSync commit protocol.

These exercise the Step-3 guarantees directly: two devices sharing one remote
dataset, compare-and-swap on targeted groups, the shared intent, and recovery
by a device that did not start the interrupted commit.

Scope note: every guarantee proven here is *new-protocol to new-protocol*. An
old client does not take the widened commit lock around its payload promotion
(it only locks the journal read/append), so it is narrowed and detected but
never mutually excluded. Nothing in this file claims otherwise.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from romcloud.core.exceptions import (
    SaveSyncCasConflictError,
    SaveSyncRecoveryEvidenceError,
    SaveSyncVerificationError,
)
from romcloud.core.storage import ProviderCapabilities, StorageProvider
from romcloud.infrastructure import savesync_commit, savesync_index
from romcloud.services.saves import SaveSyncService

from tests.unit._savesync_protocol_helpers import (
    index_root_for,
    mutate_remote_out_of_band,
    seed_peer_commit,
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


def _device(tmp_path: Path, name: str) -> SaveSyncService:
    """One device with its own local root and state, sharing one remote."""
    local = tmp_path / name / "local"
    local.mkdir(parents=True, exist_ok=True)
    return SaveSyncService(
        provider=_Provider(),
        connectivity_root=str(tmp_path / "remote"),
        local_root=str(local),
        remote_root=str(tmp_path / "remote"),
        state_path=tmp_path / name / "data" / "savesync-state.json",
    )


class TestSameGroupRace:
    def test_stale_decision_is_rejected_and_never_overwrites_the_peer(
        self, tmp_path: Path, monkeypatch, caplog
    ):
        """A and B both plan from generation N; A commits N+1 first.

        B's compare-and-swap must fail against its stale expectation. B then
        re-plans against A's committed state, which is content that diverged
        from B's baseline on both sides — so the correct outcome is a
        recorded conflict, never a silent overwrite of A's save.
        """
        device_b = _device(tmp_path, "device-b")
        b_local = tmp_path / "device-b" / "local" / "psx" / "Game.srm"
        _write(b_local, b"shared-base")
        device_b.full_sync()
        index_root = index_root_for(tmp_path / "remote")

        planned = {"count": 0}
        original = savesync_index.read_group_expectations

        def capture(index_root_arg, head, group_ids, layout_for_group):
            result = original(index_root_arg, head, group_ids, layout_for_group)
            planned["count"] += 1
            # Device A commits *after* B captured its expectation but before
            # B validates it — the exact interleaving CAS exists to catch.
            if planned["count"] == 1:
                seed_peer_commit(
                    device_b,
                    remote_root=tmp_path / "remote",
                    relative_path="psx/Game.srm",
                    content=b"device-a-committed",
                    device_id="device-a",
                )
            return result

        monkeypatch.setattr(savesync_index, "read_group_expectations", capture)

        _write(b_local, b"device-b-progress")
        device_b.mark_local_dirty("psx/Game.srm")

        with caplog.at_level("WARNING"):
            device_b.reconcile()

        assert "SaveSync commit CAS rejected" in caplog.text
        assert "payload_mutated=false" in caplog.text

        # A's committed save is intact; B never overwrote it.
        remote = tmp_path / "remote" / "psx" / "Game.srm"
        assert remote.read_bytes() == b"device-a-committed"
        head = savesync_index.load_head_strict(index_root)
        shard = savesync_index.load_shard(
            index_root, "retroarch-root-psx", head.layouts["retroarch-root-psx"]
        )
        group = next(iter(shard.groups))
        assert group.artifacts[0].sha256 == hashlib.sha256(b"device-a-committed").hexdigest()
        assert group.origin_device == "device-a"
        # Re-planning against A's commit reaches a conflict, and B's own
        # bytes are preserved locally for the user to resolve.
        assert device_b.get_state().active_conflicts
        assert b_local.read_bytes() == b"device-b-progress"

    def test_cas_conflict_is_raised_before_any_payload_mutation(
        self, tmp_path: Path
    ):
        device = _device(tmp_path, "device-b")
        local = tmp_path / "device-b" / "local" / "psx" / "Game.srm"
        _write(local, b"shared-base")
        device.full_sync()
        index_root = index_root_for(tmp_path / "remote")
        head = savesync_index.load_head_strict(index_root)
        layout_for_group = {"retroarch-root-psx/game": "retroarch-root-psx"}
        stale = savesync_index.read_group_expectations(
            index_root, head, frozenset(layout_for_group), layout_for_group
        )

        seed_peer_commit(
            device,
            remote_root=tmp_path / "remote",
            relative_path="psx/Game.srm",
            content=b"peer-newer",
            device_id="device-a",
        )

        with pytest.raises(SaveSyncCasConflictError):
            device._assert_cas(stale, operation_id="test-op")

        assert (tmp_path / "remote" / "psx" / "Game.srm").read_bytes() == b"peer-newer"


class TestDifferentGroupRace:
    def test_disjoint_group_commits_both_survive(self, tmp_path: Path):
        """A changes group X while B changes group Y.

        A global index generation bump alone must not discard a safe
        unrelated-group operation: B rebases onto A's commit.
        """
        device = _device(tmp_path, "device-b")
        game_x = tmp_path / "device-b" / "local" / "psx" / "GameX.srm"
        game_y = tmp_path / "device-b" / "local" / "psx" / "GameY.srm"
        _write(game_x, b"x-base")
        _write(game_y, b"y-base")
        device.full_sync()

        # Device A commits group X through the protocol.
        seed_peer_commit(
            device,
            remote_root=tmp_path / "remote",
            relative_path="psx/GameX.srm",
            content=b"x-from-device-a",
            device_id="device-a",
        )

        # Device B commits group Y, planned before/independently of X.
        _write(game_y, b"y-from-device-b")
        device.mark_local_dirty("psx/GameY.srm")
        device.reconcile()

        assert (tmp_path / "remote" / "psx" / "GameX.srm").read_bytes() == b"x-from-device-a"
        assert (tmp_path / "remote" / "psx" / "GameY.srm").read_bytes() == b"y-from-device-b"

        index_root = index_root_for(tmp_path / "remote")
        head = savesync_index.load_head_strict(index_root)
        shard = savesync_index.load_shard(
            index_root, "retroarch-root-psx", head.layouts["retroarch-root-psx"]
        )
        groups = {group.group_id: group for group in shard.groups}
        x_group = groups["retroarch-root-psx/gamex"]
        y_group = groups["retroarch-root-psx/gamey"]
        assert x_group.origin_device == "device-a"
        assert x_group.artifacts[0].sha256 == hashlib.sha256(b"x-from-device-a").hexdigest()
        assert y_group.artifacts[0].sha256 == hashlib.sha256(b"y-from-device-b").hexdigest()


class TestSharedIntentRecovery:
    def _interrupted(
        self, tmp_path: Path, *, phase: savesync_commit.IntentPhase, desired: bytes
    ) -> tuple[SaveSyncService, Path]:
        device = _device(tmp_path, "device-a")
        local = tmp_path / "device-a" / "local" / "psx" / "Game.srm"
        _write(local, b"base")
        device.full_sync()
        index_root = index_root_for(tmp_path / "remote")
        head = savesync_index.load_head_strict(index_root)
        before = (
            savesync_index.IndexArtifact(
                "psx/Game.srm", 4, hashlib.sha256(b"base").hexdigest()
            ),
        )
        after = (
            savesync_index.IndexArtifact(
                "psx/Game.srm", len(desired), hashlib.sha256(desired).hexdigest()
            ),
        )
        intent = savesync_commit.CommitIntent(
            schema_version=savesync_commit.SCHEMA_VERSION,
            operation_id="abandoned-operation",
            origin_device="device-a",
            dataset_id=head.dataset_id,
            base_index_generation=head.index_generation,
            base_journal_generation=head.journal_generation,
            phase=phase.value,
            started_at="2026-09-09T00:00:00+00:00",
            groups=(
                savesync_commit.IntentGroup(
                    group_id="retroarch-root-psx/game",
                    layout_id="retroarch-root-psx",
                    system="psx",
                    expected_group_generation=1,
                    before=before,
                    desired=after,
                ),
            ),
        )
        savesync_commit.write_intent(index_root, intent)
        return device, index_root

    def test_payload_at_before_state_discards_the_intent(self, tmp_path: Path):
        device, index_root = self._interrupted(
            tmp_path, phase=savesync_commit.IntentPhase.PROMOTING, desired=b"never-landed"
        )

        device._resolve_shared_intent(owned=True)

        assert savesync_commit.load_intent(index_root) is None
        assert (tmp_path / "remote" / "psx" / "Game.srm").read_bytes() == b"base"

    def test_payload_at_desired_state_is_completed_forward_not_rolled_back(
        self, tmp_path: Path
    ):
        device, index_root = self._interrupted(
            tmp_path,
            phase=savesync_commit.IntentPhase.PAYLOAD_VERIFIED,
            desired=b"promoted-bytes",
        )
        mutate_remote_out_of_band(
            tmp_path / "remote", "psx/Game.srm", b"promoted-bytes"
        )

        device._resolve_shared_intent(owned=True)

        # The verified payload was published, never reverted.
        assert (tmp_path / "remote" / "psx" / "Game.srm").read_bytes() == b"promoted-bytes"
        head = savesync_index.load_head_strict(index_root)
        assert "abandoned-operation" in savesync_index.committed_transaction_ids(
            index_root, head
        )
        assert savesync_commit.load_intent(index_root) is None

    def test_unknown_third_version_preserves_evidence_and_fails_closed(
        self, tmp_path: Path
    ):
        device, index_root = self._interrupted(
            tmp_path,
            phase=savesync_commit.IntentPhase.PROMOTING,
            desired=b"intended-bytes",
        )
        mutate_remote_out_of_band(
            tmp_path / "remote", "psx/Game.srm", b"somebody-elses-bytes"
        )

        with pytest.raises(SaveSyncRecoveryEvidenceError, match="Full Sync"):
            device._resolve_shared_intent(owned=True)

        # Nothing was rolled back or completed, and the evidence survives.
        assert (
            tmp_path / "remote" / "psx" / "Game.srm"
        ).read_bytes() == b"somebody-elses-bytes"
        preserved = list(index_root.glob("INTENT.json.*.unresolved"))
        assert len(preserved) == 1

    def test_recovery_by_a_different_device_completes_the_commit(self, tmp_path: Path):
        _, index_root = self._interrupted(
            tmp_path,
            phase=savesync_commit.IntentPhase.PAYLOAD_VERIFIED,
            desired=b"promoted-bytes",
        )
        mutate_remote_out_of_band(
            tmp_path / "remote", "psx/Game.srm", b"promoted-bytes"
        )
        # A completely separate device, which never saw the original run.
        other = _device(tmp_path, "device-b")

        other._resolve_shared_intent(owned=True)

        head = savesync_index.load_head_strict(index_root)
        assert "abandoned-operation" in savesync_index.committed_transaction_ids(
            index_root, head
        )
        assert savesync_commit.load_intent(index_root) is None

    def test_deferred_full_sync_intent_is_never_partially_published(
        self, tmp_path: Path
    ):
        """A crashed Full Sync must not be completed group-wise.

        Its intent covers only the groups that run happened to mutate, so
        publishing from it would fabricate a partial authoritative index.
        Recovery must refuse and require a complete Full Sync instead.
        """
        device, index_root = self._interrupted(
            tmp_path,
            phase=savesync_commit.IntentPhase.PAYLOAD_VERIFIED,
            desired=b"promoted-bytes",
        )
        intent = savesync_commit.load_intent(index_root)
        assert intent is not None
        savesync_commit.write_intent(
            index_root,
            savesync_commit.CommitIntent(
                schema_version=intent.schema_version,
                operation_id=intent.operation_id,
                origin_device=intent.origin_device,
                dataset_id=intent.dataset_id,
                base_index_generation=intent.base_index_generation,
                base_journal_generation=intent.base_journal_generation,
                phase=intent.phase,
                started_at=intent.started_at,
                groups=intent.groups,
                publication_scope=savesync_commit.PublicationScope.FULL_SYNC.value,
            ),
        )
        mutate_remote_out_of_band(
            tmp_path / "remote", "psx/Game.srm", b"promoted-bytes"
        )
        before_head = savesync_index.load_head_strict(index_root)

        with pytest.raises(SaveSyncVerificationError, match="Full Sync"):
            device._resolve_shared_intent(owned=True)

        after_head = savesync_index.load_head_strict(index_root)
        assert after_head.index_generation == before_head.index_generation
        assert "abandoned-operation" not in savesync_index.committed_transaction_ids(
            index_root, after_head
        )

    def test_a_full_sync_clears_a_deferred_intent_and_republishes_completely(
        self, tmp_path: Path
    ):
        device, index_root = self._interrupted(
            tmp_path,
            phase=savesync_commit.IntentPhase.PAYLOAD_VERIFIED,
            desired=b"promoted-bytes",
        )
        intent = savesync_commit.load_intent(index_root)
        assert intent is not None
        savesync_commit.write_intent(
            index_root,
            savesync_commit.CommitIntent(
                schema_version=intent.schema_version,
                operation_id=intent.operation_id,
                origin_device=intent.origin_device,
                dataset_id=intent.dataset_id,
                base_index_generation=intent.base_index_generation,
                base_journal_generation=intent.base_journal_generation,
                phase=intent.phase,
                started_at=intent.started_at,
                groups=intent.groups,
                publication_scope=savesync_commit.PublicationScope.FULL_SYNC.value,
            ),
        )
        mutate_remote_out_of_band(
            tmp_path / "remote", "psx/Game.srm", b"promoted-bytes"
        )

        device.full_sync()

        assert savesync_commit.load_intent(index_root) is None
        head = savesync_index.load_head_strict(index_root)
        shard = savesync_index.load_shard(
            index_root, "retroarch-root-psx", head.layouts["retroarch-root-psx"]
        )
        group = next(iter(shard.groups))
        # The republished index describes the real bytes, from a complete scan.
        assert group.artifacts[0].sha256 == hashlib.sha256(b"promoted-bytes").hexdigest()


class TestOwnershipStates:
    def test_full_sync_is_the_only_path_to_owned(self, tmp_path: Path):
        device = _device(tmp_path, "device-a")
        local = tmp_path / "device-a" / "local" / "psx" / "Game.srm"
        _write(local, b"base")
        index_root = index_root_for(tmp_path / "remote")

        device.reconcile()
        assert (
            savesync_index.resolve_dataset_state(index_root).ownership
            is savesync_index.DatasetOwnership.UNOWNED
        )

        device.full_sync()
        assert (
            savesync_index.resolve_dataset_state(index_root).ownership
            is savesync_index.DatasetOwnership.OWNED
        )

    def test_marker_without_head_is_damaged_and_fails_closed(self, tmp_path: Path):
        device = _device(tmp_path, "device-a")
        local = tmp_path / "device-a" / "local" / "psx" / "Game.srm"
        _write(local, b"base")
        device.full_sync()
        index_root = index_root_for(tmp_path / "remote")
        savesync_index.head_path(index_root).unlink()

        assert (
            savesync_index.resolve_dataset_state(index_root).ownership
            is savesync_index.DatasetOwnership.DAMAGED
        )
        _write(local, b"changed")
        device.mark_local_dirty("psx/Game.srm")
        with pytest.raises(SaveSyncVerificationError, match="damaged"):
            device.reconcile()
        assert (tmp_path / "remote" / "psx" / "Game.srm").read_bytes() == b"base"

    def test_corrupt_head_never_silently_disables_cas(self, tmp_path: Path):
        device = _device(tmp_path, "device-a")
        local = tmp_path / "device-a" / "local" / "psx" / "Game.srm"
        _write(local, b"base")
        device.full_sync()
        index_root = index_root_for(tmp_path / "remote")
        savesync_index.head_path(index_root).write_text("{ not json")

        assert (
            savesync_index.resolve_dataset_state(index_root).ownership
            is savesync_index.DatasetOwnership.DAMAGED
        )
        _write(local, b"changed")
        device.mark_local_dirty("psx/Game.srm")
        with pytest.raises(SaveSyncVerificationError, match="damaged"):
            device.reconcile()
        # The corrupt document is preserved for diagnosis, never rewritten.
        assert savesync_index.head_path(index_root).read_text() == "{ not json"

    def test_vanished_marker_on_a_known_dataset_is_damaged_not_legacy(
        self, tmp_path: Path
    ):
        """A silent downgrade to legacy would disable CAS exactly when
        something has gone wrong with the shared state."""
        device = _device(tmp_path, "device-a")
        local = tmp_path / "device-a" / "local" / "psx" / "Game.srm"
        _write(local, b"base")
        device.full_sync()
        index_root = index_root_for(tmp_path / "remote")
        savesync_index.ownership_path(index_root).unlink()

        _write(local, b"changed")
        device.mark_local_dirty("psx/Game.srm")
        with pytest.raises(SaveSyncVerificationError, match="damaged"):
            device.reconcile()
        assert (tmp_path / "remote" / "psx" / "Game.srm").read_bytes() == b"base"


class TestCommitLockExclusion:
    def test_the_commit_lock_is_the_existing_remote_journal_lock(self, tmp_path: Path):
        """Widened, not replaced: an old client's journal lock is the same
        file, which is what keeps mixed-version writers from interleaving
        their *journal* updates even though their payload writes are not
        excluded."""
        from romcloud.infrastructure import savesync_journal

        remote_data_root = tmp_path / "remote-data"
        journal = savesync_journal.default_journal_path(remote_data_root / "saves")
        assert (
            savesync_commit.commit_lock_path(remote_data_root)
            == journal.with_name(".savesync-journal.lock")
        )

    def test_nested_acquisition_within_one_process_does_not_deadlock(
        self, tmp_path: Path
    ):
        root = tmp_path / "remote-data"
        with savesync_commit.commit_lock(root):
            with savesync_commit.commit_lock(root):
                assert savesync_commit.commit_lock_path(root).exists()

    def test_lock_is_released_when_the_scope_raises(self, tmp_path: Path):
        root = tmp_path / "remote-data"
        with pytest.raises(RuntimeError):
            with savesync_commit.commit_lock(root):
                raise RuntimeError("boom")
        # Re-acquirable, so nothing leaked.
        with savesync_commit.commit_lock(root):
            pass
