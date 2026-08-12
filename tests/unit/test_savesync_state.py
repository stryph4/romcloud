"""Focused tests for durable SaveSync schema-v3 state."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from romcloud.core.exceptions import SaveSyncError
from romcloud.core.models.savesync import (
    SaveArtifact,
    SaveConflictResolution,
    SaveGroupCondition,
    SaveGroupSnapshot,
    SaveGroupState,
    SaveReconcileReport,
    SaveRemoteAvailability,
    SaveRemoteObservation,
    SaveSyncActiveOperation,
    SaveSyncLastError,
    SaveSyncRecord,
    SaveSyncState,
    SaveSyncStatus,
)
from romcloud.infrastructure.savesync_state import (
    CURRENT_STATE_VERSION,
    SaveSyncStateStore,
    acknowledge_conflict,
    conflict_id_for,
    load_state,
    mark_local_dirty,
    mark_remote_dirty,
    record_conflict,
    resolve_conflict,
    save_state,
    state_from_dict,
    state_to_dict,
)
from romcloud.infrastructure import savesync_state as state_module

_NOW = "2026-08-12T12:00:00+00:00"


def _artifact(path: str, byte: str = "a") -> SaveArtifact:
    return SaveArtifact(path, 1, byte * 64)


def _snapshot(
    group_id: str = "psx:game", byte: str = "a", *, observed_at: str = _NOW
) -> SaveGroupSnapshot:
    return SaveGroupSnapshot(
        group_id=group_id,
        layout_id="retroarch-root",
        artifacts=(_artifact("psx/Game.srm", byte),),
        observed_at=observed_at,
    )


def _conflicted_state() -> tuple[SaveSyncState, str]:
    baseline = _snapshot(byte="a")
    local = _snapshot(byte="b")
    remote = _snapshot(byte="c")
    state = record_conflict(
        SaveSyncState(device_id="device-1"),
        group_id="psx:game",
        layout_id="retroarch-root",
        baseline=baseline,
        local=local,
        remote=remote,
        observed_at=_NOW,
    )
    return state, state.conflicts[0].conflict_id


def test_existing_state_constructor_remains_source_compatible() -> None:
    state = SaveSyncState(device_id="device-1")

    assert state.groups == ()
    assert state.conflicts == ()
    assert state.active_operation is None
    assert state.last_completed_operation_id is None
    assert state.effective_status is SaveSyncStatus.CLEAN


def test_full_v3_round_trip_preserves_orthogonal_metadata(tmp_path: Path) -> None:
    state, _ = _conflicted_state()
    state = replace(
        state,
        remote_observation=SaveRemoteObservation(
            SaveRemoteAvailability.UNAVAILABLE, _NOW, "provider timed out"
        ),
        active_operation=SaveSyncActiveOperation(
            "operation-2", "upload", "stage", _NOW, ("psx:game",)
        ),
        last_error=SaveSyncLastError("verify", "verification failed", _NOW, "operation-1"),
        last_completed_operation_id="operation-1",
    )

    path = tmp_path / "savesync-state.json"
    save_state(path, state)
    restored = load_state(path)

    assert restored == state
    assert restored.groups[0].condition is SaveGroupCondition.CONFLICT
    assert restored.remote_observation.availability is SaveRemoteAvailability.UNAVAILABLE
    assert restored.active_operation.operation_id == "operation-2"
    assert restored.last_completed_operation_id == "operation-1"


def test_load_creates_one_durable_device_identity(tmp_path: Path) -> None:
    path = tmp_path / "data" / "savesync-state.json"

    first = load_state(path)
    second = load_state(path)

    assert first.device_id == second.device_id
    assert json.loads(path.read_text())["version"] == CURRENT_STATE_VERSION


def test_v2_migration_preserves_legacy_baseline_and_records(tmp_path: Path) -> None:
    path = tmp_path / "savesync-state.json"
    artifact = {
        "relative_path": "psx/Game.srm",
        "size_bytes": 1,
        "content_hash": "a" * 64,
    }
    record = {
        "revision": "revision-1",
        "timestamp": _NOW,
        "device_id": "device-1",
        "manifest": [artifact],
    }
    path.write_text(
        json.dumps(
            {
                "version": 2,
                "device_id": "device-1",
                "last_upload": record,
                "last_download": None,
                "shared_manifest": [artifact],
                "last_reconcile": {
                    "revision": "reconcile-1",
                    "timestamp": _NOW,
                    "uploaded": 1,
                    "downloaded": 0,
                    "conflicts": 0,
                    "unchanged": 0,
                    "upload_bytes": 1,
                    "download_bytes": 0,
                    "conflict_paths": [],
                    "scope": "all_eligible",
                },
            }
        ),
        encoding="utf-8",
    )

    state = load_state(path)
    migrated = json.loads(path.read_text(encoding="utf-8"))

    assert state.device_id == "device-1"
    assert state.last_upload.revision == "revision-1"
    assert state.shared_manifest == (_artifact("psx/Game.srm"),)
    assert state.last_reconcile.scope == "all_eligible"
    assert state.groups == () and state.conflicts == ()
    assert migrated["version"] == CURRENT_STATE_VERSION
    assert migrated["device_id"] == "device-1"


def test_v1_migration_materializes_newest_force_manifest_as_baseline(
    tmp_path: Path,
) -> None:
    path = tmp_path / "savesync-state.json"
    artifact = {
        "relative_path": "psx/Game.srm",
        "size_bytes": 1,
        "content_hash": "a" * 64,
    }
    path.write_text(
        json.dumps(
            {
                "device_id": "device-1",
                "last_upload": {
                    "revision": "revision-1",
                    "timestamp": _NOW,
                    "device_id": "device-1",
                    "manifest": [artifact],
                },
                "last_download": None,
            }
        ),
        encoding="utf-8",
    )

    state = load_state(path)

    assert state.shared_manifest == (_artifact("psx/Game.srm"),)
    assert json.loads(path.read_text(encoding="utf-8"))["shared_manifest"] == [
        artifact
    ]


def test_mark_local_dirty_survives_store_reload_and_merges_hints(tmp_path: Path) -> None:
    store = SaveSyncStateStore(tmp_path / "savesync-state.json")
    store.load()

    store.mark_local_dirty(
        group_id="psx:game",
        layout_id="retroarch-root",
        paths=("psx/Game.state", "psx/Game.srm"),
    )
    store.mark_local_dirty(
        group_id="psx:game",
        layout_id="retroarch-root",
        paths=("psx/Game.srm",),
    )

    restored = SaveSyncStateStore(store.path).load()
    assert restored.groups[0].condition is SaveGroupCondition.LOCAL_DIRTY
    assert restored.groups[0].dirty_path_hints == (
        "psx/Game.srm",
        "psx/Game.state",
    )
    assert restored.effective_status is SaveSyncStatus.LOCAL_DIRTY


def test_local_and_remote_dirty_are_combined_conservatively() -> None:
    state = mark_remote_dirty(
        SaveSyncState(device_id="device-1"),
        group_id="psx:game",
        layout_id="retroarch-root",
        paths=("psx/Game.srm",),
    )

    state = mark_local_dirty(
        state,
        group_id="psx:game",
        layout_id="retroarch-root",
        paths=("psx/Game.state",),
    )

    assert state.groups[0].condition is SaveGroupCondition.CONFLICT
    assert state.groups[0].dirty_path_hints == (
        "psx/Game.srm",
        "psx/Game.state",
    )


def test_remote_unavailable_does_not_erase_local_dirty_state() -> None:
    state = mark_local_dirty(
        SaveSyncState(device_id="device-1"),
        group_id="psx:game",
        layout_id="retroarch-root",
    )
    state = replace(
        state,
        remote_observation=SaveRemoteObservation(
            SaveRemoteAvailability.UNAVAILABLE, _NOW, "offline"
        ),
    )

    assert state.groups[0].condition is SaveGroupCondition.LOCAL_DIRTY
    # Availability is reported separately; the lossy summary remains
    # local-first so an unavailable remote cannot hide unsynchronized work.
    assert state.effective_status is SaveSyncStatus.LOCAL_DIRTY


def test_acknowledgement_survives_reload_but_does_not_resolve(tmp_path: Path) -> None:
    path = tmp_path / "savesync-state.json"
    state, conflict_id = _conflicted_state()
    before = state.conflicts[0]

    acknowledged = acknowledge_conflict(
        state, conflict_id, acknowledged_at="2026-08-12T13:00:00+00:00"
    )
    save_state(path, acknowledged)
    restored = load_state(path)
    after = restored.conflicts[0]

    assert after.acknowledged is True
    assert after.resolved is False
    assert after.resolution is None
    assert after.resolution_revision is None
    assert replace(after, acknowledged_at=None) == before
    assert restored.active_conflicts == (after,)
    assert restored.groups[0].condition is SaveGroupCondition.CONFLICT
    assert restored.effective_status is SaveSyncStatus.CONFLICT


def test_same_conflict_keeps_acknowledgement_but_changed_content_does_not() -> None:
    state, conflict_id = _conflicted_state()
    state = acknowledge_conflict(state, conflict_id, acknowledged_at=_NOW)
    group = state.groups[0]

    same = record_conflict(
        state,
        group_id=group.group_id,
        layout_id=group.layout_id,
        baseline=group.baseline,
        local=group.local_observed,
        remote=group.remote_observed,
        observed_at="2026-08-12T13:00:00+00:00",
    )
    changed = record_conflict(
        same,
        group_id=group.group_id,
        layout_id=group.layout_id,
        baseline=group.baseline,
        local=group.local_observed,
        remote=_snapshot(byte="d"),
        observed_at="2026-08-12T14:00:00+00:00",
    )

    assert same.conflicts[0].conflict_id == conflict_id
    assert same.conflicts[0].acknowledged is True
    assert len(changed.active_conflicts) == 1
    assert changed.active_conflicts[0].conflict_id != conflict_id
    assert changed.active_conflicts[0].acknowledged is False
    assert len(changed.conflicts) == 2
    previous = next(item for item in changed.conflicts if item.conflict_id == conflict_id)
    assert previous.resolved is True
    assert previous.resolution is SaveConflictResolution.MANUAL


def test_resolution_receipt_is_distinct_from_acknowledgement() -> None:
    state, conflict_id = _conflicted_state()
    state = acknowledge_conflict(state, conflict_id, acknowledged_at=_NOW)

    state = resolve_conflict(
        state,
        conflict_id,
        resolution=SaveConflictResolution.KEEP_LOCAL,
        resolution_revision="revision-2",
        resolved_at="2026-08-12T15:00:00+00:00",
    )

    conflict = state.conflicts[0]
    assert conflict.acknowledged is True
    assert conflict.resolved is True
    assert conflict.resolution is SaveConflictResolution.KEEP_LOCAL
    assert conflict.resolution_revision == "revision-2"
    assert state.active_conflicts == ()
    # Resolution metadata alone cannot assert that the group was verified clean.
    assert state.groups[0].condition is SaveGroupCondition.CONFLICT


def test_resolved_conflict_history_survives_after_group_disappears(
    tmp_path: Path,
) -> None:
    state, conflict_id = _conflicted_state()
    state = resolve_conflict(
        state,
        conflict_id,
        resolution=SaveConflictResolution.KEEP_LOCAL,
        resolution_revision="revision-2",
        resolved_at="2026-08-12T15:00:00+00:00",
    )
    archived = replace(state, groups=())
    path = tmp_path / "savesync-state.json"

    save_state(path, archived)
    restored = load_state(path)

    assert restored.groups == ()
    assert restored.active_conflicts == ()
    assert restored.conflicts == archived.conflicts


def test_unresolved_conflict_cannot_survive_without_matching_group() -> None:
    state, _ = _conflicted_state()

    with pytest.raises(SaveSyncError, match="Active SaveSync conflict.*unknown group"):
        state_to_dict(replace(state, groups=()))


def test_unresolved_conflict_requires_group_to_remain_conflicted() -> None:
    state, _ = _conflicted_state()
    clean_group = replace(state.groups[0], condition=SaveGroupCondition.CLEAN)

    with pytest.raises(SaveSyncError, match="requires conflict group state"):
        state_to_dict(replace(state, groups=(clean_group,)))


def test_unresolved_conflict_requires_matching_group_layout() -> None:
    state, _ = _conflicted_state()
    original = state.groups[0]

    def retag(snapshot: SaveGroupSnapshot | None) -> SaveGroupSnapshot | None:
        return replace(snapshot, layout_id="replacement-layout") if snapshot else None

    replacement_group = replace(
        original,
        layout_id="replacement-layout",
        baseline=retag(original.baseline),
        local_observed=retag(original.local_observed),
        remote_observed=retag(original.remote_observed),
    )

    with pytest.raises(SaveSyncError, match="layout does not match"):
        state_to_dict(replace(state, groups=(replacement_group,)))


@pytest.mark.parametrize(
    "content",
    (
        "{not-json",
        json.dumps({"version": CURRENT_STATE_VERSION + 1, "device_id": "device-1"}),
        json.dumps({"hello": "world"}),
    ),
)
def test_corrupt_or_future_state_fails_closed_without_replacement(
    tmp_path: Path, content: str
) -> None:
    path = tmp_path / "savesync-state.json"
    path.write_text(content, encoding="utf-8")

    with pytest.raises(SaveSyncError):
        load_state(path)

    assert path.read_text(encoding="utf-8") == content


def test_conflict_id_ignores_observation_time_but_tracks_content() -> None:
    baseline = _snapshot(byte="a")
    local = _snapshot(byte="b", observed_at="2026-08-12T13:00:00+00:00")
    remote = _snapshot(byte="c")
    later_local = replace(local, observed_at="2026-08-12T15:00:00+00:00")

    first = conflict_id_for(
        group_id="psx:game",
        layout_id="retroarch-root",
        baseline=baseline,
        local=local,
        remote=remote,
    )
    same_content = conflict_id_for(
        group_id="psx:game",
        layout_id="retroarch-root",
        baseline=baseline,
        local=later_local,
        remote=remote,
    )
    changed = conflict_id_for(
        group_id="psx:game",
        layout_id="retroarch-root",
        baseline=baseline,
        local=later_local,
        remote=_snapshot(byte="d"),
    )

    assert first == same_content
    assert changed != first


def test_active_and_completed_operation_cannot_share_receipt() -> None:
    state = SaveSyncState(
        device_id="device-1",
        active_operation=SaveSyncActiveOperation("operation-1", "upload", "stage", _NOW),
        last_completed_operation_id="operation-1",
    )

    with pytest.raises(SaveSyncError, match="cannot also remain active"):
        state_to_dict(state)


def test_syncing_and_error_have_durable_summary_states() -> None:
    syncing = SaveSyncState(
        device_id="device-1",
        active_operation=SaveSyncActiveOperation(
            "operation-1", "download", "verify", _NOW
        ),
    )
    failed = replace(
        syncing,
        active_operation=None,
        last_error=SaveSyncLastError("verify", "hash mismatch", _NOW, "operation-1"),
    )

    assert syncing.effective_status is SaveSyncStatus.SYNCING
    assert failed.effective_status is SaveSyncStatus.ERROR


def test_save_cannot_replace_existing_device_identity(tmp_path: Path) -> None:
    path = tmp_path / "savesync-state.json"
    save_state(path, SaveSyncState(device_id="device-1"))

    with pytest.raises(SaveSyncError, match="cannot replace"):
        save_state(path, SaveSyncState(device_id="device-2"))

    assert load_state(path).device_id == "device-1"


def test_legacy_fields_remain_serializable_with_new_defaults() -> None:
    artifact = _artifact("psx/Game.srm")
    state = SaveSyncState(
        device_id="device-1",
        last_upload=SaveSyncRecord("revision-1", _NOW, "device-1", (artifact,)),
        shared_manifest=(artifact,),
        last_reconcile=SaveReconcileReport(
            "reconcile-1", _NOW, 1, 0, 0, 0, 1, 0, (), "all_eligible"
        ),
    )

    restored = state_from_dict(state_to_dict(state))

    assert restored == state


def test_invalid_dirty_hint_is_rejected_before_persistence(tmp_path: Path) -> None:
    path = tmp_path / "savesync-state.json"
    state = SaveSyncState(
        device_id="device-1",
        groups=(
            SaveGroupState(
                "psx:game",
                "retroarch-root",
                SaveGroupCondition.LOCAL_DIRTY,
                dirty_path_hints=("../outside",),
            ),
        ),
    )

    with pytest.raises(SaveSyncError, match="canonical relative POSIX path"):
        save_state(path, state)

    assert not path.exists()


def test_state_file_is_fsynced_before_atomic_replace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "savesync-state.json"
    events: list[str] = []
    real_fsync = state_module.os.fsync
    real_replace = state_module.os.replace

    def tracked_fsync(descriptor: int) -> None:
        events.append("fsync")
        real_fsync(descriptor)

    def tracked_replace(source: object, destination: object) -> None:
        events.append("replace")
        real_replace(source, destination)

    monkeypatch.setattr(state_module.os, "fsync", tracked_fsync)
    monkeypatch.setattr(state_module.os, "replace", tracked_replace)

    save_state(path, SaveSyncState(device_id="device-1"))

    assert events[0:2] == ["fsync", "replace"]
    assert load_state(path).device_id == "device-1"


def test_replace_failure_preserves_old_state_and_cleans_temporary_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "savesync-state.json"
    original = SaveSyncState(device_id="device-1")
    save_state(path, original)
    original_bytes = path.read_bytes()

    def fail_replace(source: object, destination: object) -> None:
        raise OSError("simulated replacement failure")

    monkeypatch.setattr(state_module.os, "replace", fail_replace)

    with pytest.raises(SaveSyncError, match="Cannot persist"):
        save_state(
            path,
            replace(
                original,
                remote_observation=SaveRemoteObservation(
                    SaveRemoteAvailability.UNAVAILABLE, _NOW, "offline"
                ),
            ),
        )

    assert path.read_bytes() == original_bytes
    assert list(tmp_path.glob(f".{path.name}.*")) == []


def test_file_fsync_failure_prevents_replace_and_preserves_old_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "savesync-state.json"
    original = SaveSyncState(device_id="device-1")
    save_state(path, original)
    original_bytes = path.read_bytes()
    replace_called = False

    def fail_fsync(descriptor: int) -> None:
        raise OSError("simulated fsync failure")

    def track_replace(source: object, destination: object) -> None:
        nonlocal replace_called
        replace_called = True

    monkeypatch.setattr(state_module.os, "fsync", fail_fsync)
    monkeypatch.setattr(state_module.os, "replace", track_replace)

    with pytest.raises(SaveSyncError, match="Cannot persist"):
        save_state(
            path,
            replace(
                original,
                remote_observation=SaveRemoteObservation(
                    SaveRemoteAvailability.UNAVAILABLE, _NOW, "offline"
                ),
            ),
        )

    assert replace_called is False
    assert path.read_bytes() == original_bytes
    assert list(tmp_path.glob(f".{path.name}.*")) == []


def test_failed_migration_rewrite_leaves_v2_document_untouched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "savesync-state.json"
    original = json.dumps(
        {
            "version": 2,
            "device_id": "device-1",
            "last_upload": None,
            "last_download": None,
            "shared_manifest": [],
            "last_reconcile": None,
        }
    )
    path.write_text(original, encoding="utf-8")

    monkeypatch.setattr(
        state_module.os,
        "replace",
        lambda *args: (_ for _ in ()).throw(OSError("simulated migration failure")),
    )

    with pytest.raises(SaveSyncError, match="Cannot persist"):
        load_state(path)

    assert path.read_text(encoding="utf-8") == original
    assert list(tmp_path.glob(f".{path.name}.*")) == []


def test_state_lock_is_released_when_mutation_raises(tmp_path: Path) -> None:
    store = SaveSyncStateStore(tmp_path / "savesync-state.json")
    store.load()

    def fail_update(state: SaveSyncState) -> SaveSyncState:
        raise RuntimeError("simulated update failure")

    with pytest.raises(RuntimeError, match="simulated update failure"):
        store.update(fail_update)

    restored = store.mark_local_dirty(
        group_id="psx:game",
        layout_id="retroarch-root",
        paths=("psx/Game.srm",),
    )
    assert restored.groups[0].condition is SaveGroupCondition.LOCAL_DIRTY
