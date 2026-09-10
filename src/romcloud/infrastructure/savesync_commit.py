"""Remote SaveSync commit serialization, shared intent, and recovery.

This module owns the *cross-device* half of a SaveSync mutation. It does not
decide what to synchronize (that stays in :mod:`romcloud.services.saves`);
it guarantees that once a decision exists, exactly one device at a time may
turn it into committed remote state, and that any device can safely finish
or refuse an interrupted commit it finds.

Three pieces:

``commit_lock``
    The existing remote ``.savesync-journal.lock`` widened from "serialize a
    journal JSON rewrite" to "serialize the whole commit". Deliberately the
    same lock file rather than a second mutex: an old client appending to the
    journal already takes it, so widening the *scope* of a lock both sides
    already honor is what keeps mixed-version writers mutually excluded.
    Acquisition is re-entrant within one process (see :class:`_ReentrantFileLock`)
    because the commit sequence legitimately re-enters it through the legacy
    journal read/append helpers.

``CommitIntent``
    One shared file describing an in-flight commit precisely enough for a
    *different* device to classify and resolve it. Because commits are
    serialized by the lock, at most one intent exists at a time, so this is a
    single file rather than a directory protocol.

Recovery classification
    :func:`classify_payload` answers the only question that matters when a
    device finds someone else's abandoned intent: is the remote payload still
    at the recorded before-state (roll back / discard), exactly at the desired
    state (the payload commit succeeded — finish publication), or something
    else entirely (an unknown third version: preserve evidence, refuse).
"""

from __future__ import annotations

import json
import os
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Iterator, Mapping, Optional

from romcloud.core.exceptions import SaveSyncError
from romcloud.infrastructure.savesync_index import (
    IndexArtifact,
    compute_manifest_hash,
)

SCHEMA_VERSION = 1
INTENT_BASENAME = "INTENT.json"
_ABANDONED_INTENT_SUFFIX = ".unresolved"


class IntentPhase(str, Enum):
    """How far a commit had progressed when the intent was last updated.

    Ordering matters: every phase transition is published *after* the work it
    names has been durably verified, never before it is attempted. A crash
    therefore always leaves a phase that under-states progress, and recovery
    re-derives real progress from actual payload rather than trusting this.
    """

    PREPARED = "prepared"
    """Intent is durable; no payload byte has been promoted yet."""

    PROMOTING = "promoting"
    """Payload promotion has begun. Payload may be in any intermediate state."""

    PAYLOAD_VERIFIED = "payload-verified"
    """Desired payload is verified on disk; remote index not yet published."""

    INDEX_PUBLISHED = "index-published"
    """Remote index HEAD committed. This is the shared commit point."""

    JOURNALED = "journaled"
    """Legacy compatibility journal appended; only finalization remains."""


class PayloadState(Enum):
    """Where an interrupted commit's actual remote payload really is."""

    BEFORE = "before"
    DESIRED = "desired"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class IntentGroup:
    """One ownership group's compare-and-swap expectation and manifests.

    ``expected_group_generation`` is the generation this operation planned
    against. ``0`` means "this group is not in the remote index yet", which
    is the normal state for a dataset that has never completed a Full Sync
    under this protocol.
    """

    group_id: str
    layout_id: str
    system: str
    expected_group_generation: int
    before: tuple[IndexArtifact, ...] = ()
    desired: tuple[IndexArtifact, ...] = ()

    @property
    def before_manifest_hash(self) -> str:
        return compute_manifest_hash(self.before)

    @property
    def desired_manifest_hash(self) -> str:
        return compute_manifest_hash(self.desired)


class PublicationScope(str, Enum):
    """What the intent's originating operation was entitled to publish.

    A ``GROUP`` commit publishes exactly its own targeted groups, so another
    device may safely finish that publication forward. A ``FULL_SYNC`` commit
    belongs to a run that was going to rebuild the *complete* authoritative
    index afterwards; its intent covers only the groups that happened to be
    mutated, so completing it group-wise would fabricate a partial
    authoritative index. Recovery may classify and roll back such an intent,
    but must never publish from it.
    """

    GROUP = "group"
    FULL_SYNC = "full-sync"


@dataclass(frozen=True)
class CommitIntent:
    schema_version: int
    operation_id: str
    origin_device: str
    dataset_id: str
    base_index_generation: int
    base_journal_generation: int
    phase: str
    started_at: str
    groups: tuple[IntentGroup, ...] = ()
    publication_scope: str = PublicationScope.GROUP.value

    def with_phase(self, phase: IntentPhase) -> "CommitIntent":
        return CommitIntent(
            schema_version=self.schema_version,
            operation_id=self.operation_id,
            origin_device=self.origin_device,
            dataset_id=self.dataset_id,
            base_index_generation=self.base_index_generation,
            base_journal_generation=self.base_journal_generation,
            phase=phase.value,
            started_at=self.started_at,
            groups=self.groups,
            publication_scope=self.publication_scope,
        )

    @property
    def publishable_by_recovery(self) -> bool:
        return self.publication_scope == PublicationScope.GROUP.value

    @property
    def group_ids(self) -> frozenset[str]:
        return frozenset(group.group_id for group in self.groups)

    @property
    def affected_layout_ids(self) -> frozenset[str]:
        return frozenset(group.layout_id for group in self.groups)

    @property
    def desired_paths(self) -> frozenset[str]:
        return frozenset(
            artifact.path for group in self.groups for artifact in group.desired
        )

    @property
    def before_paths(self) -> frozenset[str]:
        return frozenset(
            artifact.path for group in self.groups for artifact in group.before
        )


# ── re-entrant exclusive remote lock ────────────────────────────────────────


class _ReentrantFileLock:
    """Process-wide re-entrant wrapper over one exclusive ``flock``.

    The underlying ``fcntl.flock`` is attached to an *open file description*,
    so a second independent ``open()`` in the same process would block against
    the first — which is exactly what the commit sequence would do when it
    re-enters through the legacy journal helpers. A ``threading.RLock``
    provides same-thread re-entrancy and correct cross-thread serialization,
    while the single underlying descriptor keeps cross-*process* and
    cross-*device* exclusion entirely delegated to the filesystem lock.
    """

    __slots__ = ("_rlock", "_depth", "_handle")

    def __init__(self) -> None:
        self._rlock = threading.RLock()
        self._depth = 0
        self._handle = None

    @contextmanager
    def hold(self, lock_path: Path) -> Iterator[None]:
        self._rlock.acquire()
        try:
            if self._depth == 0:
                lock_path.parent.mkdir(parents=True, exist_ok=True)
                handle = lock_path.open("a+b")
                try:
                    _lock_handle(handle)
                except BaseException:
                    handle.close()
                    raise
                self._handle = handle
            self._depth += 1
            try:
                yield
            finally:
                self._depth -= 1
                if self._depth == 0:
                    handle = self._handle
                    self._handle = None
                    try:
                        if handle is not None:
                            _unlock_handle(handle)
                    finally:
                        if handle is not None:
                            handle.close()
        finally:
            self._rlock.release()


_LOCKS: dict[str, _ReentrantFileLock] = {}
_LOCKS_GUARD = threading.Lock()


def _lock_for(lock_path: Path) -> _ReentrantFileLock:
    key = str(Path(lock_path).absolute())
    with _LOCKS_GUARD:
        lock = _LOCKS.get(key)
        if lock is None:
            lock = _ReentrantFileLock()
            _LOCKS[key] = lock
        return lock


def _lock_handle(handle) -> None:  # noqa: ANN001
    if os.name == "nt":
        import msvcrt

        handle.seek(0)
        if handle.tell() == handle.seek(0, os.SEEK_END):
            handle.write(b"\0")
            handle.flush()
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
    else:
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)


def _unlock_handle(handle) -> None:  # noqa: ANN001
    if os.name == "nt":
        import msvcrt

        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@contextmanager
def exclusive_lock(lock_path: Path) -> Iterator[None]:
    """Hold *lock_path* exclusively, re-entrantly within this process."""
    with _lock_for(lock_path).hold(Path(lock_path)):
        yield


def commit_lock_path(remote_data_root: Path) -> Path:
    """The one lock guarding both the legacy journal and the commit sequence."""
    return Path(remote_data_root) / ".savesync-journal.lock"


@contextmanager
def commit_lock(remote_data_root: Path) -> Iterator[None]:
    with exclusive_lock(commit_lock_path(remote_data_root)):
        yield


# ── shared intent persistence ───────────────────────────────────────────────


def intent_path(index_root: Path) -> Path:
    return Path(index_root) / INTENT_BASENAME


def _artifact_to_dict(artifact: IndexArtifact) -> dict:
    return {
        "path": artifact.path,
        "size_bytes": artifact.size_bytes,
        "sha256": artifact.sha256,
    }


def _artifacts_from_list(payload: object, label: str) -> tuple[IndexArtifact, ...]:
    if not isinstance(payload, list):
        raise SaveSyncError(f"SaveSync intent {label} must be a list")
    artifacts: list[IndexArtifact] = []
    seen: set[str] = set()
    for entry in payload:
        if not isinstance(entry, dict):
            raise SaveSyncError(f"SaveSync intent {label} entry must be an object")
        path = entry.get("path")
        if not isinstance(path, str) or not path:
            raise SaveSyncError(f"SaveSync intent {label} path must be non-empty text")
        if path in seen:
            raise SaveSyncError(f"SaveSync intent {label} has a duplicate path: {path!r}")
        seen.add(path)
        size_bytes = entry.get("size_bytes")
        if isinstance(size_bytes, bool) or not isinstance(size_bytes, int) or size_bytes < 0:
            raise SaveSyncError(f"SaveSync intent {label} size_bytes must be a non-negative integer")
        sha256 = entry.get("sha256")
        if not isinstance(sha256, str) or len(sha256) != 64:
            raise SaveSyncError(f"SaveSync intent {label} sha256 must be a sha256 hex digest")
        artifacts.append(IndexArtifact(path=path, size_bytes=size_bytes, sha256=sha256))
    return tuple(artifacts)


def intent_to_dict(intent: CommitIntent) -> dict:
    return {
        "schema_version": intent.schema_version,
        "operation_id": intent.operation_id,
        "origin_device": intent.origin_device,
        "dataset_id": intent.dataset_id,
        "base_index_generation": intent.base_index_generation,
        "base_journal_generation": intent.base_journal_generation,
        "phase": intent.phase,
        "started_at": intent.started_at,
        "publication_scope": intent.publication_scope,
        "groups": [
            {
                "group_id": group.group_id,
                "layout_id": group.layout_id,
                "system": group.system,
                "expected_group_generation": group.expected_group_generation,
                "before_manifest_hash": group.before_manifest_hash,
                "desired_manifest_hash": group.desired_manifest_hash,
                "before": [_artifact_to_dict(artifact) for artifact in group.before],
                "desired": [_artifact_to_dict(artifact) for artifact in group.desired],
            }
            for group in sorted(intent.groups, key=lambda item: item.group_id)
        ],
    }


def intent_from_dict(payload: object, *, path: Path) -> CommitIntent:
    if not isinstance(payload, dict):
        raise SaveSyncError(f"SaveSync intent is invalid: {path}")
    version = payload.get("schema_version")
    if version != SCHEMA_VERSION:
        raise SaveSyncError(f"SaveSync intent schema version {version!r} is not supported: {path}")

    def _text(key: str) -> str:
        value = payload.get(key)
        if not isinstance(value, str) or not value.strip():
            raise SaveSyncError(f"SaveSync intent {key} must be non-empty text")
        return value

    def _generation(key: str) -> int:
        value = payload.get(key)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise SaveSyncError(f"SaveSync intent {key} must be a non-negative integer")
        return value

    phase = payload.get("phase")
    if phase not in {item.value for item in IntentPhase}:
        raise SaveSyncError(f"SaveSync intent phase {phase!r} is not recognized: {path}")
    publication_scope = payload.get("publication_scope", PublicationScope.GROUP.value)
    if publication_scope not in {item.value for item in PublicationScope}:
        raise SaveSyncError(
            f"SaveSync intent publication_scope {publication_scope!r} is not recognized: {path}"
        )
    raw_groups = payload.get("groups")
    if not isinstance(raw_groups, list):
        raise SaveSyncError(f"SaveSync intent groups must be a list: {path}")
    groups: list[IntentGroup] = []
    seen_group_ids: set[str] = set()
    for entry in raw_groups:
        if not isinstance(entry, dict):
            raise SaveSyncError(f"SaveSync intent group must be an object: {path}")
        group_id = entry.get("group_id")
        if not isinstance(group_id, str) or not group_id:
            raise SaveSyncError("SaveSync intent group_id must be non-empty text")
        if group_id in seen_group_ids:
            raise SaveSyncError(f"SaveSync intent has a duplicate group: {group_id!r}")
        seen_group_ids.add(group_id)
        layout_id = entry.get("layout_id")
        if not isinstance(layout_id, str) or not layout_id:
            raise SaveSyncError("SaveSync intent layout_id must be non-empty text")
        system = entry.get("system")
        if not isinstance(system, str) or not system:
            raise SaveSyncError("SaveSync intent system must be non-empty text")
        expected = entry.get("expected_group_generation")
        if isinstance(expected, bool) or not isinstance(expected, int) or expected < 0:
            raise SaveSyncError(
                "SaveSync intent expected_group_generation must be a non-negative integer"
            )
        group = IntentGroup(
            group_id=group_id,
            layout_id=layout_id,
            system=system,
            expected_group_generation=expected,
            before=_artifacts_from_list(entry.get("before"), "group.before"),
            desired=_artifacts_from_list(entry.get("desired"), "group.desired"),
        )
        for label, declared, actual in (
            ("before_manifest_hash", entry.get("before_manifest_hash"), group.before_manifest_hash),
            ("desired_manifest_hash", entry.get("desired_manifest_hash"), group.desired_manifest_hash),
        ):
            if declared != actual:
                raise SaveSyncError(
                    f"SaveSync intent group {group_id!r} {label} does not match its manifest"
                )
        groups.append(group)
    return CommitIntent(
        schema_version=version,
        operation_id=_text("operation_id"),
        origin_device=_text("origin_device"),
        dataset_id=_text("dataset_id"),
        base_index_generation=_generation("base_index_generation"),
        base_journal_generation=_generation("base_journal_generation"),
        phase=str(phase),
        started_at=_text("started_at"),
        groups=tuple(groups),
        publication_scope=str(publication_scope),
    )


def _durable_atomic_write_text(path: Path, content: str) -> None:
    import tempfile

    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
        _fsync_directory(path.parent)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def write_intent(index_root: Path, intent: CommitIntent) -> None:
    """Publish/refresh the shared intent. Caller must hold the commit lock."""
    _durable_atomic_write_text(
        intent_path(index_root),
        json.dumps(intent_to_dict(intent), indent=2, sort_keys=True) + "\n",
    )


def load_intent(index_root: Path) -> Optional[CommitIntent]:
    path = intent_path(index_root)
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SaveSyncError(f"SaveSync intent is corrupt: {path}") from exc
    return intent_from_dict(payload, path=path)


def clear_intent(index_root: Path) -> None:
    """Retire a fully finalized intent. Caller must hold the commit lock."""
    path = intent_path(index_root)
    try:
        path.unlink(missing_ok=True)
        _fsync_directory(path.parent)
    except OSError:
        log_path = path.with_name(path.name + ".stale")
        try:
            path.replace(log_path)
        except OSError:
            pass


def preserve_unresolved_intent(index_root: Path, intent: CommitIntent) -> Path:
    """Move an unresolvable intent aside as evidence without deleting it.

    Used only for the unknown-third-version case: the operation is refused,
    but the record of what was expected must survive for diagnosis and for
    the Full Sync that has to repair the dataset.
    """
    source = intent_path(index_root)
    target = source.with_name(f"{INTENT_BASENAME}.{intent.operation_id}{_ABANDONED_INTENT_SUFFIX}")
    try:
        if source.exists():
            source.replace(target)
            _fsync_directory(target.parent)
    except OSError:
        return source
    return target


# ── recovery classification ─────────────────────────────────────────────────


def classify_payload(
    intent: CommitIntent, observed: Mapping[str, IndexArtifact]
) -> PayloadState:
    """Compare actual remote payload against an intent's recorded manifests.

    *observed* must be a fresh, cache-bypassing observation restricted to the
    union of the intent's before/desired paths. Classification is per-group
    and all-or-nothing across the whole intent: a partially promoted commit
    (some groups desired, some before) is deliberately ``UNKNOWN`` rather
    than a guess, because completing or rolling back only part of a
    multi-group transaction is exactly the outcome the transaction layer
    exists to prevent.
    """
    all_before = True
    all_desired = True
    for group in intent.groups:
        actual = tuple(
            sorted(
                (
                    observed[artifact.path]
                    for artifact in (*group.before, *group.desired)
                    if artifact.path in observed
                ),
                key=lambda item: item.path,
            )
        )
        actual_unique = tuple(
            artifact for index, artifact in enumerate(actual)
            if index == 0 or artifact.path != actual[index - 1].path
        )
        actual_hash = compute_manifest_hash(actual_unique)
        if actual_hash != group.before_manifest_hash:
            all_before = False
        if actual_hash != group.desired_manifest_hash:
            all_desired = False
    if all_desired:
        return PayloadState.DESIRED
    if all_before:
        return PayloadState.BEFORE
    return PayloadState.UNKNOWN
