"""Durable, schema-versioned SaveSync state persistence and transitions.

The state file is local ROMCloud metadata, not an authority for save content.
Dirty-path hints and cached observations are useful to a future watcher, but all
sync decisions must still verify the allowlisted filesystem content itself.

Schema v3 preserves the v2 upload/download/common-manifest fields while adding
orthogonal per-group, conflict, availability, operation, and error metadata.
Corrupt or newer state is rejected without replacing the original file or
silently generating a new device identity.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import uuid
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Callable, Iterator, Optional

from romcloud.core.exceptions import SaveSyncError
from romcloud.core.models.savesync import (
    SaveArtifact,
    SaveConflictRecord,
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
)
CURRENT_STATE_VERSION = 3
_MIGRATABLE_VERSIONS = frozenset({1, 2})


def new_state(*, device_id: Optional[str] = None) -> SaveSyncState:
    """Return an empty state with a stable-identity candidate.

    Use :func:`load_state` when the identity must immediately be persisted.
    """

    return SaveSyncState(device_id=device_id or uuid.uuid4().hex)


def state_to_dict(state: SaveSyncState) -> dict[str, object]:
    """Serialize and structurally validate one schema-v3 state document."""

    _validate_state(state)
    return {
        "version": CURRENT_STATE_VERSION,
        "device_id": state.device_id,
        "last_upload": _record_to_dict(state.last_upload),
        "last_download": _record_to_dict(state.last_download),
        "shared_manifest": [_artifact_to_dict(item) for item in state.shared_manifest],
        "last_reconcile": _report_to_dict(state.last_reconcile),
        "groups": [_group_to_dict(group) for group in state.groups],
        "conflicts": [_conflict_to_dict(conflict) for conflict in state.conflicts],
        "remote_observation": _remote_to_dict(state.remote_observation),
        "active_operation": _operation_to_dict(state.active_operation),
        "last_error": _error_to_dict(state.last_error),
        "last_completed_operation_id": state.last_completed_operation_id,
    }


def state_from_dict(payload: object) -> SaveSyncState:
    """Decode v1-v3 state, migrating known legacy documents in memory.

    Missing version metadata is recognized only as the historical v1 shape.
    Explicit unknown or future versions fail closed.
    """

    data = _mapping(payload, "SaveSync state")
    version = _document_version(data)
    if version not in _MIGRATABLE_VERSIONS and version != CURRENT_STATE_VERSION:
        relation = "newer" if version > CURRENT_STATE_VERSION else "unsupported"
        raise SaveSyncError(
            f"SaveSync state schema version {version} is {relation}; "
            f"this ROMCloud build supports up to version {CURRENT_STATE_VERSION}. "
            "The existing state file was not changed."
        )

    state = SaveSyncState(
        device_id=_nonempty_text(data.get("device_id"), "device_id"),
        last_upload=_record_from_dict(data.get("last_upload"), "last_upload"),
        last_download=_record_from_dict(data.get("last_download"), "last_download"),
        shared_manifest=_artifacts_from_list(
            data.get("shared_manifest", []), "shared_manifest"
        ),
        last_reconcile=_report_from_dict(data.get("last_reconcile")),
        groups=(
            _groups_from_list(data.get("groups", []))
            if version == CURRENT_STATE_VERSION
            else ()
        ),
        conflicts=(
            _conflicts_from_list(data.get("conflicts", []))
            if version == CURRENT_STATE_VERSION
            else ()
        ),
        remote_observation=(
            _remote_from_dict(data.get("remote_observation", {}))
            if version == CURRENT_STATE_VERSION
            else SaveRemoteObservation()
        ),
        active_operation=(
            _operation_from_dict(data.get("active_operation"))
            if version == CURRENT_STATE_VERSION
            else None
        ),
        last_error=(
            _error_from_dict(data.get("last_error"))
            if version == CURRENT_STATE_VERSION
            else None
        ),
        last_completed_operation_id=(
            _optional_text(
                data.get("last_completed_operation_id"),
                "last_completed_operation_id",
            )
            if version == CURRENT_STATE_VERSION
            else None
        ),
    )
    if version == 1 and not state.shared_manifest:
        # V1 recorded only force-operation receipts. A successful force made
        # both selected sides identical, so its newest manifest is the one
        # safe migration baseline. Persist it during the v3 rewrite; an empty
        # v3 shared_manifest can then unambiguously mean "verified empty".
        records = tuple(
            record
            for record in (state.last_upload, state.last_download)
            if record is not None
        )
        if records:
            newest = max(records, key=lambda record: record.timestamp)
            state = replace(state, shared_manifest=newest.manifest)
    _validate_state(state)
    return state


def read_state(path: Path) -> SaveSyncState:
    """Read state without acquiring a lock or writing migration output.

    This low-level form is intended for a service that already owns the
    SaveSync operation lock. A missing file returns a new in-memory state;
    malformed, corrupt, or future-version state raises :class:`SaveSyncError`.
    """

    state_path = Path(path)
    if not state_path.exists():
        return new_state()
    try:
        raw = state_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise SaveSyncError(f"Cannot read SaveSync state {state_path}: {exc}") from exc
    try:
        payload = json.loads(raw)
    except (UnicodeError, ValueError) as exc:
        raise SaveSyncError(
            f"SaveSync state {state_path} is corrupt; the existing file was not changed."
        ) from exc
    try:
        return state_from_dict(payload)
    except SaveSyncError as exc:
        raise SaveSyncError(f"Invalid SaveSync state {state_path}: {exc}") from exc


def write_state(path: Path, state: SaveSyncState) -> None:
    """Durably and atomically write v3 state without acquiring the lock.

    The complete temporary document is flushed and fsynced before replacement.
    On filesystems that support it, the containing directory is fsynced after
    ``os.replace`` so the new directory entry also survives a sudden power loss.
    """

    state_path = Path(path)
    document = json.dumps(state_to_dict(state), indent=2, sort_keys=True) + "\n"
    try:
        _durable_atomic_write_text(state_path, document)
    except OSError as exc:
        raise SaveSyncError(f"Cannot persist SaveSync state {state_path}: {exc}") from exc


@contextmanager
def state_file_lock(path: Path) -> Iterator[None]:
    """Hold the cross-process SaveSync lock shared with sync operations."""

    state_path = Path(path)
    lock_path = state_path.with_name(".savesync.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+b") as handle:
        _lock_handle(handle)
        try:
            yield
        finally:
            _unlock_handle(handle)


def load_state(path: Path) -> SaveSyncState:
    """Load/create state and durably rewrite recognized legacy schemas as v3."""

    state_path = Path(path)
    with state_file_lock(state_path):
        existed = state_path.exists()
        legacy = _on_disk_version(state_path) if existed else None
        state = read_state(state_path)
        if not existed or legacy != CURRENT_STATE_VERSION:
            write_state(state_path, state)
        return state


def save_state(path: Path, state: SaveSyncState) -> None:
    """Atomically save state while holding the shared cross-process lock."""

    state_path = Path(path)
    with state_file_lock(state_path):
        if state_path.exists():
            current = read_state(state_path)
            if current.device_id != state.device_id:
                raise SaveSyncError(
                    "SaveSync state save cannot replace the durable device identity"
                )
        write_state(state_path, state)


def mutate_state(
    path: Path, update: Callable[[SaveSyncState], SaveSyncState]
) -> SaveSyncState:
    """Apply one locked read-modify-write transition.

    A mutation cannot silently replace the durable device identity.
    """

    state_path = Path(path)
    with state_file_lock(state_path):
        current = read_state(state_path)
        updated = update(current)
        if not isinstance(updated, SaveSyncState):
            raise TypeError("SaveSync state update must return SaveSyncState")
        if updated.device_id != current.device_id:
            raise SaveSyncError("SaveSync state mutation cannot change device_id")
        write_state(state_path, updated)
        return updated


def mark_local_dirty(
    state: SaveSyncState,
    *,
    group_id: str,
    layout_id: str,
    paths: tuple[str, ...] = (),
) -> SaveSyncState:
    """Mark a registry-approved group locally dirty without performing sync.

    Paths are advisory hints only. If verified remote changes were already
    pending, the conservative combined state is ``conflict``.
    """

    return _mark_dirty(
        state,
        group_id=group_id,
        layout_id=layout_id,
        paths=paths,
        side=SaveGroupCondition.LOCAL_DIRTY,
    )


def mark_remote_dirty(
    state: SaveSyncState,
    *,
    group_id: str,
    layout_id: str,
    paths: tuple[str, ...] = (),
) -> SaveSyncState:
    """Mark a group remotely dirty without discarding local dirty state."""

    return _mark_dirty(
        state,
        group_id=group_id,
        layout_id=layout_id,
        paths=paths,
        side=SaveGroupCondition.REMOTE_DIRTY,
    )


def acknowledge_conflict(
    state: SaveSyncState,
    conflict_id: str,
    *,
    acknowledged_at: Optional[str] = None,
) -> SaveSyncState:
    """Acknowledge an active conflict without resolving or hiding it."""

    timestamp = acknowledged_at or _utc_now()
    _nonempty_text(timestamp, "acknowledged_at")
    found = False
    conflicts: list[SaveConflictRecord] = []
    for conflict in state.conflicts:
        if conflict.conflict_id != conflict_id:
            conflicts.append(conflict)
            continue
        found = True
        if conflict.resolved:
            raise SaveSyncError(f"SaveSync conflict {conflict_id} is already resolved")
        conflicts.append(replace(conflict, acknowledged_at=timestamp))
    if not found:
        raise SaveSyncError(f"Unknown SaveSync conflict: {conflict_id}")
    return replace(state, conflicts=tuple(conflicts))


def conflict_id_for(
    *,
    group_id: str,
    layout_id: str,
    baseline: Optional[SaveGroupSnapshot],
    local: SaveGroupSnapshot,
    remote: SaveGroupSnapshot,
) -> str:
    """Return a stable ID for one exact set of conflicting content.

    Observation timestamps are deliberately excluded: seeing the same bytes
    again must not manufacture a new conflict or lose acknowledgement state.
    """

    _nonempty_text(group_id, "group_id")
    _nonempty_text(layout_id, "layout_id")
    for label, snapshot in (("baseline", baseline), ("local", local), ("remote", remote)):
        if snapshot is not None:
            _validate_snapshot(snapshot, label, group_id=group_id, layout_id=layout_id)
    payload = {
        "group_id": group_id,
        "layout_id": layout_id,
        "baseline": _snapshot_content_dict(baseline),
        "local": _snapshot_content_dict(local),
        "remote": _snapshot_content_dict(remote),
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def record_conflict(
    state: SaveSyncState,
    *,
    group_id: str,
    layout_id: str,
    baseline: Optional[SaveGroupSnapshot],
    local: SaveGroupSnapshot,
    remote: SaveGroupSnapshot,
    observed_at: Optional[str] = None,
) -> SaveSyncState:
    """Persist current conflict evidence and mark its group conflicted.

    Re-observing the exact same active conflict preserves acknowledgement. A
    changed fingerprint is a new unacknowledged conflict. Stale unresolved
    evidence for that group is replaced, while resolved history is retained.
    """

    timestamp = observed_at or _utc_now()
    conflict_id = conflict_id_for(
        group_id=group_id,
        layout_id=layout_id,
        baseline=baseline,
        local=local,
        remote=remote,
    )
    existing = next(
        (
            item
            for item in state.conflicts
            if item.conflict_id == conflict_id and not item.resolved
        ),
        None,
    )
    conflict = (
        replace(
            existing,
            last_seen_at=timestamp,
            baseline=baseline,
            local=local,
            remote=remote,
        )
        if existing is not None
        else SaveConflictRecord(
            conflict_id=conflict_id,
            group_id=group_id,
            layout_id=layout_id,
            detected_at=timestamp,
            last_seen_at=timestamp,
            baseline=baseline,
            local=local,
            remote=remote,
        )
    )
    # Preserve earlier fingerprints as durable history. Only the current
    # group's exact fingerprint remains active; stale unresolved records are
    # explicitly superseded rather than silently disappearing.
    superseded = tuple(
        replace(
            item,
            resolved_at=(item.resolved_at or timestamp),
            resolution=(item.resolution or SaveConflictResolution.MANUAL),
            resolution_revision=(item.resolution_revision or f"superseded:{conflict_id}"),
        )
        if item.group_id == group_id
        and not item.resolved
        and item.conflict_id != conflict_id
        else item
        for item in state.conflicts
        if item.conflict_id != conflict_id
    )
    groups = _upsert_group(
        state.groups,
        SaveGroupState(
            group_id=group_id,
            layout_id=layout_id,
            condition=SaveGroupCondition.CONFLICT,
            baseline=baseline,
            local_observed=local,
            remote_observed=remote,
        ),
    )
    return replace(
        state,
        groups=groups,
        conflicts=tuple(sorted((*superseded, conflict), key=lambda item: item.conflict_id)),
    )


def resolve_conflict(
    state: SaveSyncState,
    conflict_id: str,
    *,
    resolution: SaveConflictResolution,
    resolution_revision: str,
    resolved_at: Optional[str] = None,
) -> SaveSyncState:
    """Record a verified resolution receipt.

    This transition deliberately does not invent a new clean group snapshot;
    the service must persist the verified post-commit group state separately.
    """

    timestamp = resolved_at or _utc_now()
    if not isinstance(resolution, SaveConflictResolution):
        raise SaveSyncError("resolution must be a SaveConflictResolution")
    _nonempty_text(resolution_revision, "resolution_revision")
    found = False
    conflicts: list[SaveConflictRecord] = []
    for conflict in state.conflicts:
        if conflict.conflict_id != conflict_id:
            conflicts.append(conflict)
            continue
        found = True
        if conflict.resolved:
            raise SaveSyncError(f"SaveSync conflict {conflict_id} is already resolved")
        conflicts.append(
            replace(
                conflict,
                resolved_at=timestamp,
                resolution=resolution,
                resolution_revision=resolution_revision,
            )
        )
    if not found:
        raise SaveSyncError(f"Unknown SaveSync conflict: {conflict_id}")
    return replace(state, conflicts=tuple(conflicts))


class SaveSyncStateStore:
    """Locked persistence facade suitable for a service or future watcher."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def load(self) -> SaveSyncState:
        return load_state(self.path)

    def save(self, state: SaveSyncState) -> None:
        save_state(self.path, state)

    def update(
        self, transition: Callable[[SaveSyncState], SaveSyncState]
    ) -> SaveSyncState:
        return mutate_state(self.path, transition)

    def mark_local_dirty(
        self, *, group_id: str, layout_id: str, paths: tuple[str, ...] = ()
    ) -> SaveSyncState:
        return self.update(
            lambda state: mark_local_dirty(
                state,
                group_id=group_id,
                layout_id=layout_id,
                paths=paths,
            )
        )

    def mark_remote_dirty(
        self, *, group_id: str, layout_id: str, paths: tuple[str, ...] = ()
    ) -> SaveSyncState:
        return self.update(
            lambda state: mark_remote_dirty(
                state,
                group_id=group_id,
                layout_id=layout_id,
                paths=paths,
            )
        )

    def acknowledge_conflict(
        self, conflict_id: str, *, acknowledged_at: Optional[str] = None
    ) -> SaveSyncState:
        return self.update(
            lambda state: acknowledge_conflict(
                state, conflict_id, acknowledged_at=acknowledged_at
            )
        )


def _mark_dirty(
    state: SaveSyncState,
    *,
    group_id: str,
    layout_id: str,
    paths: tuple[str, ...],
    side: SaveGroupCondition,
) -> SaveSyncState:
    _nonempty_text(group_id, "group_id")
    _nonempty_text(layout_id, "layout_id")
    hints = tuple(sorted({_canonical_path(path, "dirty path") for path in paths}))
    existing = next((group for group in state.groups if group.group_id == group_id), None)
    if existing is not None and existing.layout_id != layout_id:
        raise SaveSyncError(
            f"SaveSync group {group_id!r} belongs to layout {existing.layout_id!r}, "
            f"not {layout_id!r}"
        )
    if existing is None:
        updated = SaveGroupState(
            group_id=group_id,
            layout_id=layout_id,
            condition=side,
            dirty_path_hints=hints,
        )
    else:
        opposite = (
            SaveGroupCondition.REMOTE_DIRTY
            if side is SaveGroupCondition.LOCAL_DIRTY
            else SaveGroupCondition.LOCAL_DIRTY
        )
        condition = (
            SaveGroupCondition.CONFLICT
            if existing.condition in (opposite, SaveGroupCondition.CONFLICT)
            else side
        )
        updated = replace(
            existing,
            condition=condition,
            dirty_path_hints=tuple(sorted(set(existing.dirty_path_hints).union(hints))),
            verified_at=None,
        )
    return replace(state, groups=_upsert_group(state.groups, updated))


def _upsert_group(
    groups: tuple[SaveGroupState, ...], updated: SaveGroupState
) -> tuple[SaveGroupState, ...]:
    values = [group for group in groups if group.group_id != updated.group_id]
    values.append(updated)
    return tuple(sorted(values, key=lambda group: group.group_id))


def _validate_state(state: SaveSyncState) -> None:
    if not isinstance(state, SaveSyncState):
        raise SaveSyncError("SaveSync state must be a SaveSyncState")
    _nonempty_text(state.device_id, "device_id")
    _validate_record(state.last_upload, "last_upload")
    _validate_record(state.last_download, "last_download")
    _validate_artifacts(state.shared_manifest, "shared_manifest")
    if state.last_reconcile is not None:
        _validate_report(state.last_reconcile)

    group_ids: set[str] = set()
    groups: dict[str, SaveGroupState] = {}
    for group in state.groups:
        _validate_group(group)
        if group.group_id in group_ids:
            raise SaveSyncError(f"Duplicate SaveSync group_id: {group.group_id}")
        group_ids.add(group.group_id)
        groups[group.group_id] = group

    conflict_ids: set[str] = set()
    for conflict in state.conflicts:
        _validate_conflict(conflict)
        if conflict.conflict_id in conflict_ids:
            raise SaveSyncError(f"Duplicate SaveSync conflict_id: {conflict.conflict_id}")
        conflict_ids.add(conflict.conflict_id)
        if not conflict.resolved:
            group = groups.get(conflict.group_id)
            if group is None:
                raise SaveSyncError(
                    f"Active SaveSync conflict {conflict.conflict_id} "
                    "references an unknown group"
                )
            if group.layout_id != conflict.layout_id:
                raise SaveSyncError(
                    f"Active SaveSync conflict {conflict.conflict_id} "
                    "layout does not match its group"
                )
            if group.condition is not SaveGroupCondition.CONFLICT:
                raise SaveSyncError(
                    f"Active SaveSync conflict {conflict.conflict_id} "
                    "requires conflict group state"
                )

    _validate_remote(state.remote_observation)
    if state.active_operation is not None:
        _validate_operation(state.active_operation)
    if state.last_error is not None:
        _validate_error(state.last_error)
    if state.last_completed_operation_id is not None:
        _nonempty_text(
            state.last_completed_operation_id, "last_completed_operation_id"
        )
    if (
        state.active_operation is not None
        and state.active_operation.operation_id == state.last_completed_operation_id
    ):
        raise SaveSyncError("A completed SaveSync operation cannot also remain active")


def _validate_group(group: SaveGroupState) -> None:
    if not isinstance(group, SaveGroupState):
        raise SaveSyncError("groups must contain SaveGroupState values")
    _nonempty_text(group.group_id, "group_id")
    _nonempty_text(group.layout_id, "layout_id")
    if not isinstance(group.condition, SaveGroupCondition):
        raise SaveSyncError(f"Invalid condition for SaveSync group {group.group_id}")
    for label, snapshot in (
        ("baseline", group.baseline),
        ("local_observed", group.local_observed),
        ("remote_observed", group.remote_observed),
    ):
        if snapshot is not None:
            _validate_snapshot(
                snapshot,
                label,
                group_id=group.group_id,
                layout_id=group.layout_id,
            )
    for path in group.dirty_path_hints:
        _canonical_path(path, f"dirty path for {group.group_id}")
    if len(set(group.dirty_path_hints)) != len(group.dirty_path_hints):
        raise SaveSyncError(f"Duplicate dirty path hint for group {group.group_id}")
    if group.verified_at is not None:
        _nonempty_text(group.verified_at, "verified_at")


def _validate_conflict(conflict: SaveConflictRecord) -> None:
    if not isinstance(conflict, SaveConflictRecord):
        raise SaveSyncError("conflicts must contain SaveConflictRecord values")
    _nonempty_text(conflict.conflict_id, "conflict_id")
    _nonempty_text(conflict.group_id, "conflict group_id")
    _nonempty_text(conflict.layout_id, "conflict layout_id")
    _nonempty_text(conflict.detected_at, "detected_at")
    _nonempty_text(conflict.last_seen_at, "last_seen_at")
    for label, snapshot in (
        ("baseline", conflict.baseline),
        ("local", conflict.local),
        ("remote", conflict.remote),
    ):
        if snapshot is not None:
            _validate_snapshot(
                snapshot,
                label,
                group_id=conflict.group_id,
                layout_id=conflict.layout_id,
            )
    if conflict.acknowledged_at is not None:
        _nonempty_text(conflict.acknowledged_at, "acknowledged_at")
    resolution_fields = (
        conflict.resolved_at,
        conflict.resolution,
        conflict.resolution_revision,
    )
    if any(value is not None for value in resolution_fields) and not all(
        value is not None for value in resolution_fields
    ):
        raise SaveSyncError(
            f"SaveSync conflict {conflict.conflict_id} has incomplete resolution metadata"
        )
    if conflict.resolution is not None and not isinstance(
        conflict.resolution, SaveConflictResolution
    ):
        raise SaveSyncError(f"Invalid resolution for conflict {conflict.conflict_id}")
    expected_id = conflict_id_for(
        group_id=conflict.group_id,
        layout_id=conflict.layout_id,
        baseline=conflict.baseline,
        local=conflict.local,
        remote=conflict.remote,
    )
    if conflict.conflict_id != expected_id:
        raise SaveSyncError(
            f"SaveSync conflict {conflict.conflict_id} does not match its content evidence"
        )


def _validate_snapshot(
    snapshot: SaveGroupSnapshot,
    label: str,
    *,
    group_id: Optional[str] = None,
    layout_id: Optional[str] = None,
) -> None:
    if not isinstance(snapshot, SaveGroupSnapshot):
        raise SaveSyncError(f"{label} must be a SaveGroupSnapshot")
    _nonempty_text(snapshot.group_id, f"{label}.group_id")
    _nonempty_text(snapshot.layout_id, f"{label}.layout_id")
    if group_id is not None and snapshot.group_id != group_id:
        raise SaveSyncError(f"{label} snapshot belongs to a different SaveSync group")
    if layout_id is not None and snapshot.layout_id != layout_id:
        raise SaveSyncError(f"{label} snapshot belongs to a different save layout")
    _validate_artifacts(snapshot.artifacts, f"{label}.artifacts")
    if snapshot.observed_at is not None:
        _nonempty_text(snapshot.observed_at, f"{label}.observed_at")


def _validate_artifacts(artifacts: tuple[SaveArtifact, ...], label: str) -> None:
    paths: set[str] = set()
    for artifact in artifacts:
        if not isinstance(artifact, SaveArtifact):
            raise SaveSyncError(f"{label} must contain SaveArtifact values")
        path = _canonical_path(artifact.relative_path, f"{label} path")
        if path in paths:
            raise SaveSyncError(f"Duplicate artifact path in {label}: {path}")
        paths.add(path)
        if (
            isinstance(artifact.size_bytes, bool)
            or not isinstance(artifact.size_bytes, int)
            or artifact.size_bytes < 0
        ):
            raise SaveSyncError(f"Invalid artifact size for {path}")
        digest = artifact.content_hash
        if not isinstance(digest, str) or len(digest) != 64 or any(
            char not in "0123456789abcdefABCDEF" for char in digest
        ):
            raise SaveSyncError(f"Invalid SHA-256 content hash for {path}")


def _validate_record(record: Optional[SaveSyncRecord], label: str) -> None:
    if record is None:
        return
    if not isinstance(record, SaveSyncRecord):
        raise SaveSyncError(f"{label} must be a SaveSyncRecord")
    _nonempty_text(record.revision, f"{label}.revision")
    _nonempty_text(record.timestamp, f"{label}.timestamp")
    _nonempty_text(record.device_id, f"{label}.device_id")
    _validate_artifacts(record.manifest, f"{label}.manifest")


def _validate_report(report: SaveReconcileReport) -> None:
    if not isinstance(report, SaveReconcileReport):
        raise SaveSyncError("last_reconcile must be a SaveReconcileReport")
    _nonempty_text(report.revision, "last_reconcile.revision")
    _nonempty_text(report.timestamp, "last_reconcile.timestamp")
    _nonempty_text(report.scope, "last_reconcile.scope")
    for name in (
        "uploaded",
        "downloaded",
        "conflicts",
        "unchanged",
        "upload_bytes",
        "download_bytes",
    ):
        value = getattr(report, name)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise SaveSyncError(f"Invalid non-negative integer for last_reconcile.{name}")
    for path in report.conflict_paths:
        _canonical_path(path, "last_reconcile conflict path")


def _validate_remote(observation: SaveRemoteObservation) -> None:
    if not isinstance(observation, SaveRemoteObservation):
        raise SaveSyncError("remote_observation must be a SaveRemoteObservation")
    if not isinstance(observation.availability, SaveRemoteAvailability):
        raise SaveSyncError("Invalid SaveSync remote availability")
    if observation.checked_at is not None:
        _nonempty_text(observation.checked_at, "remote_observation.checked_at")
    if not isinstance(observation.detail, str):
        raise SaveSyncError("remote_observation.detail must be text")


def _validate_operation(operation: SaveSyncActiveOperation) -> None:
    if not isinstance(operation, SaveSyncActiveOperation):
        raise SaveSyncError("active_operation must be a SaveSyncActiveOperation")
    _nonempty_text(operation.operation_id, "active_operation.operation_id")
    _nonempty_text(operation.direction, "active_operation.direction")
    _nonempty_text(operation.phase, "active_operation.phase")
    _nonempty_text(operation.started_at, "active_operation.started_at")
    if len(set(operation.group_ids)) != len(operation.group_ids):
        raise SaveSyncError("active_operation has duplicate group IDs")
    for group_id in operation.group_ids:
        _nonempty_text(group_id, "active_operation.group_id")


def _validate_error(error: SaveSyncLastError) -> None:
    if not isinstance(error, SaveSyncLastError):
        raise SaveSyncError("last_error must be a SaveSyncLastError")
    _nonempty_text(error.code, "last_error.code")
    _nonempty_text(error.message, "last_error.message")
    _nonempty_text(error.occurred_at, "last_error.occurred_at")
    if error.operation_id is not None:
        _nonempty_text(error.operation_id, "last_error.operation_id")


def _artifact_to_dict(artifact: SaveArtifact) -> dict[str, object]:
    return {
        "relative_path": artifact.relative_path,
        "size_bytes": artifact.size_bytes,
        "content_hash": artifact.content_hash,
    }


def _artifact_from_dict(payload: object, label: str) -> SaveArtifact:
    data = _mapping(payload, label)
    size = data.get("size_bytes")
    if isinstance(size, bool) or not isinstance(size, int):
        raise SaveSyncError(f"{label}.size_bytes must be an integer")
    artifact = SaveArtifact(
        relative_path=_canonical_path(data.get("relative_path"), f"{label}.relative_path"),
        size_bytes=size,
        content_hash=_nonempty_text(data.get("content_hash"), f"{label}.content_hash"),
    )
    _validate_artifacts((artifact,), label)
    return artifact


def _artifacts_from_list(payload: object, label: str) -> tuple[SaveArtifact, ...]:
    values = _list(payload, label)
    artifacts = tuple(
        _artifact_from_dict(item, f"{label}[{index}]")
        for index, item in enumerate(values)
    )
    _validate_artifacts(artifacts, label)
    return tuple(sorted(artifacts, key=lambda item: item.relative_path))


def _snapshot_to_dict(snapshot: Optional[SaveGroupSnapshot]) -> Optional[dict[str, object]]:
    if snapshot is None:
        return None
    return {
        "group_id": snapshot.group_id,
        "layout_id": snapshot.layout_id,
        "artifacts": [_artifact_to_dict(item) for item in snapshot.artifacts],
        "observed_at": snapshot.observed_at,
    }


def _snapshot_content_dict(
    snapshot: Optional[SaveGroupSnapshot],
) -> Optional[dict[str, object]]:
    if snapshot is None:
        return None
    return {
        "group_id": snapshot.group_id,
        "layout_id": snapshot.layout_id,
        "artifacts": [
            _artifact_to_dict(item)
            for item in sorted(snapshot.artifacts, key=lambda value: value.relative_path)
        ],
    }


def _snapshot_from_dict(payload: object, label: str) -> Optional[SaveGroupSnapshot]:
    if payload is None:
        return None
    data = _mapping(payload, label)
    snapshot = SaveGroupSnapshot(
        group_id=_nonempty_text(data.get("group_id"), f"{label}.group_id"),
        layout_id=_nonempty_text(data.get("layout_id"), f"{label}.layout_id"),
        artifacts=_artifacts_from_list(data.get("artifacts", []), f"{label}.artifacts"),
        observed_at=_optional_text(data.get("observed_at"), f"{label}.observed_at"),
    )
    _validate_snapshot(snapshot, label)
    return snapshot


def _group_to_dict(group: SaveGroupState) -> dict[str, object]:
    return {
        "group_id": group.group_id,
        "layout_id": group.layout_id,
        "condition": group.condition.value,
        "baseline": _snapshot_to_dict(group.baseline),
        "local_observed": _snapshot_to_dict(group.local_observed),
        "remote_observed": _snapshot_to_dict(group.remote_observed),
        "dirty_path_hints": list(group.dirty_path_hints),
        "verified_at": group.verified_at,
    }


def _group_from_dict(payload: object, label: str) -> SaveGroupState:
    data = _mapping(payload, label)
    try:
        condition = SaveGroupCondition(data.get("condition", SaveGroupCondition.CLEAN.value))
    except (TypeError, ValueError) as exc:
        raise SaveSyncError(f"{label}.condition is invalid") from exc
    group = SaveGroupState(
        group_id=_nonempty_text(data.get("group_id"), f"{label}.group_id"),
        layout_id=_nonempty_text(data.get("layout_id"), f"{label}.layout_id"),
        condition=condition,
        baseline=_snapshot_from_dict(data.get("baseline"), f"{label}.baseline"),
        local_observed=_snapshot_from_dict(
            data.get("local_observed"), f"{label}.local_observed"
        ),
        remote_observed=_snapshot_from_dict(
            data.get("remote_observed"), f"{label}.remote_observed"
        ),
        dirty_path_hints=tuple(
            _canonical_path(item, f"{label}.dirty_path_hints")
            for item in _list(data.get("dirty_path_hints", []), f"{label}.dirty_path_hints")
        ),
        verified_at=_optional_text(data.get("verified_at"), f"{label}.verified_at"),
    )
    _validate_group(group)
    return group


def _groups_from_list(payload: object) -> tuple[SaveGroupState, ...]:
    groups = tuple(
        _group_from_dict(item, f"groups[{index}]")
        for index, item in enumerate(_list(payload, "groups"))
    )
    return tuple(sorted(groups, key=lambda item: item.group_id))


def _conflict_to_dict(conflict: SaveConflictRecord) -> dict[str, object]:
    return {
        "conflict_id": conflict.conflict_id,
        "group_id": conflict.group_id,
        "layout_id": conflict.layout_id,
        "detected_at": conflict.detected_at,
        "last_seen_at": conflict.last_seen_at,
        "baseline": _snapshot_to_dict(conflict.baseline),
        "local": _snapshot_to_dict(conflict.local),
        "remote": _snapshot_to_dict(conflict.remote),
        "acknowledged_at": conflict.acknowledged_at,
        "resolved_at": conflict.resolved_at,
        "resolution": conflict.resolution.value if conflict.resolution else None,
        "resolution_revision": conflict.resolution_revision,
    }


def _conflict_from_dict(payload: object, label: str) -> SaveConflictRecord:
    data = _mapping(payload, label)
    resolution_raw = data.get("resolution")
    try:
        resolution = (
            SaveConflictResolution(resolution_raw) if resolution_raw is not None else None
        )
    except (TypeError, ValueError) as exc:
        raise SaveSyncError(f"{label}.resolution is invalid") from exc
    local = _snapshot_from_dict(data.get("local"), f"{label}.local")
    remote = _snapshot_from_dict(data.get("remote"), f"{label}.remote")
    if local is None or remote is None:
        raise SaveSyncError(f"{label} requires local and remote snapshots")
    conflict = SaveConflictRecord(
        conflict_id=_nonempty_text(data.get("conflict_id"), f"{label}.conflict_id"),
        group_id=_nonempty_text(data.get("group_id"), f"{label}.group_id"),
        layout_id=_nonempty_text(data.get("layout_id"), f"{label}.layout_id"),
        detected_at=_nonempty_text(data.get("detected_at"), f"{label}.detected_at"),
        last_seen_at=_nonempty_text(data.get("last_seen_at"), f"{label}.last_seen_at"),
        baseline=_snapshot_from_dict(data.get("baseline"), f"{label}.baseline"),
        local=local,
        remote=remote,
        acknowledged_at=_optional_text(
            data.get("acknowledged_at"), f"{label}.acknowledged_at"
        ),
        resolved_at=_optional_text(data.get("resolved_at"), f"{label}.resolved_at"),
        resolution=resolution,
        resolution_revision=_optional_text(
            data.get("resolution_revision"), f"{label}.resolution_revision"
        ),
    )
    _validate_conflict(conflict)
    return conflict


def _conflicts_from_list(payload: object) -> tuple[SaveConflictRecord, ...]:
    conflicts = tuple(
        _conflict_from_dict(item, f"conflicts[{index}]")
        for index, item in enumerate(_list(payload, "conflicts"))
    )
    return tuple(sorted(conflicts, key=lambda item: item.conflict_id))


def _record_to_dict(record: Optional[SaveSyncRecord]) -> Optional[dict[str, object]]:
    if record is None:
        return None
    return {
        "revision": record.revision,
        "timestamp": record.timestamp,
        "device_id": record.device_id,
        "manifest": [_artifact_to_dict(item) for item in record.manifest],
    }


def _record_from_dict(payload: object, label: str) -> Optional[SaveSyncRecord]:
    if payload is None:
        return None
    data = _mapping(payload, label)
    record = SaveSyncRecord(
        revision=_nonempty_text(data.get("revision"), f"{label}.revision"),
        timestamp=_nonempty_text(data.get("timestamp"), f"{label}.timestamp"),
        device_id=_nonempty_text(data.get("device_id"), f"{label}.device_id"),
        manifest=_artifacts_from_list(data.get("manifest", []), f"{label}.manifest"),
    )
    _validate_record(record, label)
    return record


def _report_to_dict(report: Optional[SaveReconcileReport]) -> Optional[dict[str, object]]:
    return report.to_dict() if report is not None else None


def _report_from_dict(payload: object) -> Optional[SaveReconcileReport]:
    if payload is None:
        return None
    data = _mapping(payload, "last_reconcile")
    report = SaveReconcileReport(
        revision=_nonempty_text(data.get("revision"), "last_reconcile.revision"),
        timestamp=_nonempty_text(data.get("timestamp"), "last_reconcile.timestamp"),
        uploaded=_nonnegative_int(data.get("uploaded", 0), "last_reconcile.uploaded"),
        downloaded=_nonnegative_int(
            data.get("downloaded", 0), "last_reconcile.downloaded"
        ),
        conflicts=_nonnegative_int(
            data.get("conflicts", 0), "last_reconcile.conflicts"
        ),
        unchanged=_nonnegative_int(
            data.get("unchanged", 0), "last_reconcile.unchanged"
        ),
        upload_bytes=_nonnegative_int(
            data.get("upload_bytes", 0), "last_reconcile.upload_bytes"
        ),
        download_bytes=_nonnegative_int(
            data.get("download_bytes", 0), "last_reconcile.download_bytes"
        ),
        conflict_paths=tuple(
            _canonical_path(item, "last_reconcile.conflict_paths")
            for item in _list(data.get("conflict_paths", []), "last_reconcile.conflict_paths")
        ),
        scope=_nonempty_text(
            data.get("scope", "managed_games"), "last_reconcile.scope"
        ),
    )
    _validate_report(report)
    return report


def _remote_to_dict(observation: SaveRemoteObservation) -> dict[str, object]:
    return {
        "availability": observation.availability.value,
        "checked_at": observation.checked_at,
        "detail": observation.detail,
    }


def _remote_from_dict(payload: object) -> SaveRemoteObservation:
    data = _mapping(payload, "remote_observation")
    try:
        availability = SaveRemoteAvailability(
            data.get("availability", SaveRemoteAvailability.UNKNOWN.value)
        )
    except (TypeError, ValueError) as exc:
        raise SaveSyncError("remote_observation.availability is invalid") from exc
    observation = SaveRemoteObservation(
        availability=availability,
        checked_at=_optional_text(
            data.get("checked_at"), "remote_observation.checked_at"
        ),
        detail=_text(data.get("detail", ""), "remote_observation.detail"),
    )
    _validate_remote(observation)
    return observation


def _operation_to_dict(
    operation: Optional[SaveSyncActiveOperation],
) -> Optional[dict[str, object]]:
    if operation is None:
        return None
    return {
        "operation_id": operation.operation_id,
        "direction": operation.direction,
        "phase": operation.phase,
        "started_at": operation.started_at,
        "group_ids": list(operation.group_ids),
    }


def _operation_from_dict(payload: object) -> Optional[SaveSyncActiveOperation]:
    if payload is None:
        return None
    data = _mapping(payload, "active_operation")
    operation = SaveSyncActiveOperation(
        operation_id=_nonempty_text(
            data.get("operation_id"), "active_operation.operation_id"
        ),
        direction=_nonempty_text(data.get("direction"), "active_operation.direction"),
        phase=_nonempty_text(data.get("phase"), "active_operation.phase"),
        started_at=_nonempty_text(data.get("started_at"), "active_operation.started_at"),
        group_ids=tuple(
            _nonempty_text(item, "active_operation.group_ids")
            for item in _list(data.get("group_ids", []), "active_operation.group_ids")
        ),
    )
    _validate_operation(operation)
    return operation


def _error_to_dict(error: Optional[SaveSyncLastError]) -> Optional[dict[str, object]]:
    if error is None:
        return None
    return {
        "code": error.code,
        "message": error.message,
        "occurred_at": error.occurred_at,
        "operation_id": error.operation_id,
    }


def _error_from_dict(payload: object) -> Optional[SaveSyncLastError]:
    if payload is None:
        return None
    data = _mapping(payload, "last_error")
    error = SaveSyncLastError(
        code=_nonempty_text(data.get("code"), "last_error.code"),
        message=_nonempty_text(data.get("message"), "last_error.message"),
        occurred_at=_nonempty_text(data.get("occurred_at"), "last_error.occurred_at"),
        operation_id=_optional_text(data.get("operation_id"), "last_error.operation_id"),
    )
    _validate_error(error)
    return error


def _document_version(data: dict[str, object]) -> int:
    raw = data.get("version")
    if raw is None:
        # Historical v1 documents had no explicit version and only these
        # top-level fields. Requiring device_id avoids treating arbitrary JSON
        # as a recognized state shape.
        legacy_keys = frozenset({"device_id", "last_upload", "last_download"})
        if "device_id" not in data or not set(data).issubset(legacy_keys):
            raise SaveSyncError("SaveSync state has no recognized schema version")
        return 1
    if isinstance(raw, bool) or not isinstance(raw, int) or raw < 1:
        raise SaveSyncError("SaveSync state version must be a positive integer")
    return raw


def _on_disk_version(path: Path) -> int:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as exc:
        raise SaveSyncError(
            f"SaveSync state {path} is corrupt; the existing file was not changed."
        ) from exc
    return _document_version(_mapping(payload, "SaveSync state"))


def _mapping(payload: object, label: str) -> dict[str, object]:
    if not isinstance(payload, dict):
        raise SaveSyncError(f"{label} must be a JSON object")
    return payload


def _list(payload: object, label: str) -> list[object]:
    if not isinstance(payload, list):
        raise SaveSyncError(f"{label} must be a JSON array")
    return payload


def _text(value: object, label: str) -> str:
    if not isinstance(value, str):
        raise SaveSyncError(f"{label} must be text")
    return value


def _nonempty_text(value: object, label: str) -> str:
    result = _text(value, label)
    if not result.strip():
        raise SaveSyncError(f"{label} must not be empty")
    return result


def _optional_text(value: object, label: str) -> Optional[str]:
    return None if value is None else _nonempty_text(value, label)


def _nonnegative_int(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise SaveSyncError(f"{label} must be a non-negative integer")
    return value


def _canonical_path(value: object, label: str) -> str:
    path = _nonempty_text(value, label)
    if "\\" in path or path.startswith("/"):
        raise SaveSyncError(f"{label} must be a canonical relative POSIX path")
    pure = PurePosixPath(path)
    if any(part in ("", ".", "..") for part in pure.parts) or pure.as_posix() != path:
        raise SaveSyncError(f"{label} must be a canonical relative POSIX path")
    return path


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _durable_atomic_write_text(path: Path, content: str) -> None:
    """Write, fsync, and atomically replace one state document.

    Failure before ``os.replace`` leaves the existing state untouched and
    removes the temporary file. Directory fsync is necessarily best-effort on
    platforms such as Windows that cannot open a directory as a file handle.
    """

    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", dir=str(path.parent)
    )
    temporary = Path(temporary_name)
    descriptor_owned = True
    try:
        handle = os.fdopen(descriptor, "w", encoding="utf-8")
        descriptor_owned = False
        with handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    except BaseException:
        if descriptor_owned:
            try:
                os.close(descriptor)
            except OSError:
                pass
        temporary.unlink(missing_ok=True)
        raise


def _fsync_directory(directory: Path) -> None:
    """Best-effort persistence barrier for the replacement directory entry."""

    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    try:
        descriptor = os.open(directory, flags)
    except OSError:
        return
    try:
        try:
            os.fsync(descriptor)
        except OSError:
            # Some mounted/network filesystems accept the atomic rename but do
            # not implement fsync on directory handles. The already-fsynced
            # file remains valid; do not report a false pre-commit failure after
            # replacement has become visible.
            pass
    finally:
        try:
            os.close(descriptor)
        except OSError:
            # The replacement is already visible and the directory fsync was
            # attempted. Do not turn handle cleanup into a false pre-commit
            # failure that could make a caller roll back separately committed
            # save data while retaining the new state receipt.
            pass


def _lock_handle(handle) -> None:  # noqa: ANN001
    try:
        import fcntl
    except ImportError:  # pragma: no cover - Batocera is POSIX; useful for dev hosts
        import msvcrt

        handle.seek(0)
        if handle.read(1) == b"":
            handle.write(b"\0")
            handle.flush()
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
    else:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)


def _unlock_handle(handle) -> None:  # noqa: ANN001
    try:
        import fcntl
    except ImportError:  # pragma: no cover - Batocera is POSIX; useful for dev hosts
        import msvcrt

        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
