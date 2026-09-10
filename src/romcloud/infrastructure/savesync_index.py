"""Durable remote SaveSync current-state index — shadow/descriptive only.

This is a sibling of the existing remote mutation journal
(:mod:`romcloud.infrastructure.savesync_journal`), not a replacement for it in
this phase. The journal remains the sole input to Quick Sync scoping and is
never modified here, and its ``schema_version`` must never be bumped by this
module — an unknown journal schema version causes an old client to
destructively reset the journal file (see :func:`savesync_journal.load_or_reset`),
which this index must never trigger.

The index instead publishes a small atomic ``HEAD.json`` plus one immutable
JSON object per :class:`~romcloud.core.save_selection.SaveLayout` ("shard"),
each carrying the verified current manifest for every registered group Full
Sync has ever observed under that layout. Only Full Sync ever builds or
publishes this index, strictly *after* it has already reconciled and verified
actual remote content — the index describes committed payload, never journal
inference. Normal Quick Sync/gameStart/gameStop reconciliation decisions must
not read or depend on this index yet: a mismatch between the journal's own
``generation`` and this index's recorded ``journal_generation`` is detectable
here (an old client wrote through the journal without updating this index)
but this phase never acts on that beyond diagnostics — see
:func:`journal_diverged`.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from romcloud.core.exceptions import SaveSyncError

SCHEMA_VERSION = 1
_SHA256_HEX = re.compile(r"[0-9a-f]{64}")
_SAFE_LAYOUT_ID = re.compile(r"[A-Za-z0-9_-]+")
_EMPTY_MANIFEST_HASH = hashlib.sha256(b"").hexdigest()


def default_index_root(remote_saves_root: Path) -> Path:
    return Path(remote_saves_root).parent / "savesync-index"


def head_path(index_root: Path) -> Path:
    return Path(index_root) / "HEAD.json"


def _previous_head_path(index_root: Path) -> Path:
    return Path(index_root) / "HEAD.previous.json"


def _safe_layout_id(layout_id: str) -> str:
    if not layout_id or not _SAFE_LAYOUT_ID.fullmatch(layout_id):
        raise SaveSyncError(f"Unsafe SaveSync index layout ID: {layout_id!r}")
    return layout_id


def shard_path(index_root: Path, layout_id: str, generation: int) -> Path:
    if isinstance(generation, bool) or not isinstance(generation, int) or generation < 1:
        raise SaveSyncError("SaveSync index shard generation must be a positive integer")
    return Path(index_root) / "layouts" / f"{_safe_layout_id(layout_id)}.{generation}.json"


@dataclass(frozen=True)
class IndexArtifact:
    path: str
    size_bytes: int
    sha256: str


@dataclass(frozen=True)
class IndexGroup:
    """Verified current remote state for one registered save group.

    ``artifacts`` empty plus ``tombstoned=True`` is an explicit verified
    absence, distinct from the group simply not appearing in the shard at
    all (never observed under this layout).
    """

    group_id: str
    layout_id: str
    system: str
    group_generation: int
    artifacts: tuple[IndexArtifact, ...] = ()
    tombstoned: bool = False
    origin_device: str = ""
    completed_transaction_id: Optional[str] = None
    container_head: Optional[str] = None

    @property
    def manifest_hash(self) -> str:
        return compute_manifest_hash(self.artifacts)


@dataclass(frozen=True)
class IndexShard:
    schema_version: int
    dataset_id: str
    layout_id: str
    generation: int
    groups: tuple[IndexGroup, ...] = ()


@dataclass(frozen=True)
class IndexLayoutHead:
    generation: int
    object: str
    digest: str


@dataclass(frozen=True)
class IndexHead:
    schema_version: int
    dataset_id: str
    index_generation: int
    journal_generation: int
    layouts: dict[str, IndexLayoutHead] = field(default_factory=dict)


def compute_manifest_hash(artifacts: tuple[IndexArtifact, ...]) -> str:
    """Deterministic digest of a group's exact artifact set.

    The digest of an empty tuple is a fixed, well-known value (sha256 of the
    empty string) — a verified-empty group is always distinguishable from a
    group that was never observed (absent from the shard entirely).
    """
    digest = hashlib.sha256()
    for artifact in sorted(artifacts, key=lambda item: item.path):
        digest.update(artifact.path.encode("utf-8"))
        digest.update(b"\0")
        digest.update(str(artifact.size_bytes).encode("ascii"))
        digest.update(b"\0")
        digest.update(artifact.sha256.encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest()


# ── serialization ──────────────────────────────────────────────────────────


def _canonical_json(payload: dict) -> str:
    return json.dumps(payload, indent=2, sort_keys=True) + "\n"


def _artifact_to_dict(artifact: IndexArtifact) -> dict:
    return {
        "path": artifact.path,
        "size_bytes": artifact.size_bytes,
        "sha256": artifact.sha256,
    }


def _group_to_dict(group: IndexGroup) -> dict:
    return {
        "layout_id": group.layout_id,
        "system": group.system,
        "group_generation": group.group_generation,
        "manifest_hash": group.manifest_hash,
        "artifacts": [_artifact_to_dict(artifact) for artifact in group.artifacts],
        "tombstoned": group.tombstoned,
        "origin_device": group.origin_device,
        "completed_transaction_id": group.completed_transaction_id,
        "container_head": group.container_head,
    }


def shard_to_dict(shard: IndexShard) -> dict:
    return {
        "schema_version": shard.schema_version,
        "dataset_id": shard.dataset_id,
        "layout_id": shard.layout_id,
        "generation": shard.generation,
        "groups": {group.group_id: _group_to_dict(group) for group in shard.groups},
    }


def head_to_dict(head: IndexHead) -> dict:
    return {
        "schema_version": head.schema_version,
        "dataset_id": head.dataset_id,
        "index_generation": head.index_generation,
        "journal_generation": head.journal_generation,
        "layouts": {
            layout_id: {
                "generation": layout_head.generation,
                "object": layout_head.object,
                "digest": layout_head.digest,
            }
            for layout_id, layout_head in head.layouts.items()
        },
    }


def _nonempty_text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise SaveSyncError(f"SaveSync index {label} must be non-empty text")
    return value


def _nonneg_int(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise SaveSyncError(f"SaveSync index {label} must be a non-negative integer")
    return value


def _sha256_hex(value: object, label: str) -> str:
    if not isinstance(value, str) or not _SHA256_HEX.fullmatch(value):
        raise SaveSyncError(f"SaveSync index {label} must be a sha256 hex digest")
    return value


def _canonical_artifact_path(value: object, *, system: str) -> str:
    if not isinstance(value, str) or not value:
        raise SaveSyncError("SaveSync index artifact path must be non-empty text")
    if (
        "\\" in value
        or value.startswith("/")
        or value.endswith("/")
        or any(part in {"", ".", ".."} for part in value.split("/"))
    ):
        raise SaveSyncError(f"Unsafe SaveSync index artifact path: {value!r}")
    if not (value == system or value.startswith(f"{system}/")):
        raise SaveSyncError(
            f"SaveSync index artifact path {value!r} is outside its group's system {system!r}"
        )
    return value


def _artifact_from_dict(payload: object, *, system: str) -> IndexArtifact:
    if not isinstance(payload, dict):
        raise SaveSyncError("SaveSync index artifact entry must be an object")
    return IndexArtifact(
        path=_canonical_artifact_path(payload.get("path"), system=system),
        size_bytes=_nonneg_int(payload.get("size_bytes"), "artifact.size_bytes"),
        sha256=_sha256_hex(payload.get("sha256"), "artifact.sha256"),
    )


def _group_from_dict(group_id: str, payload: object, *, layout_id: str) -> IndexGroup:
    if not isinstance(payload, dict):
        raise SaveSyncError(f"SaveSync index group {group_id!r} must be an object")
    if not group_id or not group_id.startswith(f"{layout_id}/"):
        raise SaveSyncError(
            f"SaveSync index group ID {group_id!r} does not belong to layout {layout_id!r}"
        )
    declared_layout_id = _nonempty_text(payload.get("layout_id"), "group.layout_id")
    if declared_layout_id != layout_id:
        raise SaveSyncError(
            f"SaveSync index group {group_id!r} layout_id {declared_layout_id!r} "
            f"does not match its shard's layout {layout_id!r}"
        )
    system = _nonempty_text(payload.get("system"), "group.system")
    raw_artifacts = payload.get("artifacts")
    if not isinstance(raw_artifacts, list):
        raise SaveSyncError(f"SaveSync index group {group_id!r} artifacts must be a list")
    artifacts: list[IndexArtifact] = []
    seen_paths: set[str] = set()
    for entry in raw_artifacts:
        artifact = _artifact_from_dict(entry, system=system)
        if artifact.path in seen_paths:
            raise SaveSyncError(
                f"SaveSync index group {group_id!r} has a duplicate artifact path: "
                f"{artifact.path!r}"
            )
        seen_paths.add(artifact.path)
        artifacts.append(artifact)
    group = IndexGroup(
        group_id=group_id,
        layout_id=layout_id,
        system=system,
        group_generation=_nonneg_int(payload.get("group_generation"), "group.group_generation"),
        artifacts=tuple(artifacts),
        tombstoned=bool(payload.get("tombstoned", False)),
        origin_device=str(payload.get("origin_device") or ""),
        completed_transaction_id=(
            _nonempty_text(payload["completed_transaction_id"], "group.completed_transaction_id")
            if payload.get("completed_transaction_id") is not None
            else None
        ),
        container_head=(
            _nonempty_text(payload["container_head"], "group.container_head")
            if payload.get("container_head") is not None
            else None
        ),
    )
    declared_hash = _sha256_hex(payload.get("manifest_hash"), "group.manifest_hash")
    if declared_hash != group.manifest_hash:
        raise SaveSyncError(
            f"SaveSync index group {group_id!r} manifest_hash does not match its artifacts"
        )
    if group.tombstoned and group.artifacts:
        raise SaveSyncError(
            f"SaveSync index group {group_id!r} cannot be tombstoned with artifacts present"
        )
    return group


def validate_shard_document(payload: object, *, path: Path) -> IndexShard:
    if not isinstance(payload, dict):
        raise SaveSyncError(f"SaveSync index shard is invalid: {path}")
    version = payload.get("schema_version")
    if version != SCHEMA_VERSION:
        raise SaveSyncError(
            f"SaveSync index shard schema version {version!r} is not supported: {path}"
        )
    dataset_id = _nonempty_text(payload.get("dataset_id"), "shard.dataset_id")
    layout_id = _safe_layout_id(_nonempty_text(payload.get("layout_id"), "shard.layout_id"))
    generation = payload.get("generation")
    if isinstance(generation, bool) or not isinstance(generation, int) or generation < 1:
        raise SaveSyncError(f"SaveSync index shard generation must be a positive integer: {path}")
    raw_groups = payload.get("groups")
    if not isinstance(raw_groups, dict):
        raise SaveSyncError(f"SaveSync index shard groups must be an object: {path}")
    groups = tuple(
        _group_from_dict(str(group_id), group_payload, layout_id=layout_id)
        for group_id, group_payload in raw_groups.items()
    )
    return IndexShard(
        schema_version=version,
        dataset_id=dataset_id,
        layout_id=layout_id,
        generation=generation,
        groups=groups,
    )


def _layout_head_from_dict(layout_id: str, payload: object) -> IndexLayoutHead:
    if not isinstance(payload, dict):
        raise SaveSyncError(f"SaveSync index HEAD layout entry {layout_id!r} must be an object")
    generation = payload.get("generation")
    if isinstance(generation, bool) or not isinstance(generation, int) or generation < 1:
        raise SaveSyncError(
            f"SaveSync index HEAD layout {layout_id!r} generation must be a positive integer"
        )
    object_name = _nonempty_text(payload.get("object"), "HEAD layout.object")
    expected_prefix = f"{_safe_layout_id(layout_id)}."
    if not object_name.startswith(expected_prefix) or "/" in object_name or "\\" in object_name:
        raise SaveSyncError(
            f"SaveSync index HEAD layout {layout_id!r} object name is unsafe: {object_name!r}"
        )
    digest = _sha256_hex(payload.get("digest"), "HEAD layout.digest")
    return IndexLayoutHead(generation=generation, object=object_name, digest=digest)


def validate_head_document(payload: object, *, path: Path) -> IndexHead:
    if not isinstance(payload, dict):
        raise SaveSyncError(f"SaveSync index HEAD is invalid: {path}")
    version = payload.get("schema_version")
    if version != SCHEMA_VERSION:
        raise SaveSyncError(f"SaveSync index HEAD schema version {version!r} is not supported: {path}")
    dataset_id = _nonempty_text(payload.get("dataset_id"), "HEAD.dataset_id")
    index_generation = _nonneg_int(payload.get("index_generation"), "HEAD.index_generation")
    journal_generation = _nonneg_int(payload.get("journal_generation"), "HEAD.journal_generation")
    raw_layouts = payload.get("layouts")
    if not isinstance(raw_layouts, dict):
        raise SaveSyncError(f"SaveSync index HEAD layouts must be an object: {path}")
    layouts = {
        _safe_layout_id(str(layout_id)): _layout_head_from_dict(str(layout_id), entry)
        for layout_id, entry in raw_layouts.items()
    }
    return IndexHead(
        schema_version=version,
        dataset_id=dataset_id,
        index_generation=index_generation,
        journal_generation=journal_generation,
        layouts=layouts,
    )


# ── durable I/O ─────────────────────────────────────────────────────────────


def _durable_atomic_write_text(path: Path, content: str) -> None:
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


def write_shard(index_root: Path, shard: IndexShard) -> IndexLayoutHead:
    """Publish one immutable layout shard, verifying prior content on reuse.

    A shard file name embeds its own generation, so it is written at most
    once: if the exact path already exists, its content must already match
    byte-for-byte (proving idempotent retry), never silently overwritten.
    """
    path = shard_path(index_root, shard.layout_id, shard.generation)
    content = _canonical_json(shard_to_dict(shard))
    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
    if path.exists():
        existing = path.read_text(encoding="utf-8")
        if existing != content:
            raise SaveSyncError(
                f"SaveSync index shard {path} already exists with different content"
            )
    else:
        _durable_atomic_write_text(path, content)
    return IndexLayoutHead(generation=shard.generation, object=path.name, digest=digest)


def load_shard(index_root: Path, layout_id: str, layout_head: IndexLayoutHead) -> IndexShard:
    path = Path(index_root) / "layouts" / layout_head.object
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise SaveSyncError(f"Cannot read SaveSync index shard: {path}") from exc
    try:
        payload = json.loads(raw)
    except ValueError as exc:
        raise SaveSyncError(f"SaveSync index shard is corrupt: {path}") from exc
    digest = hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()
    if digest != layout_head.digest:
        raise SaveSyncError(f"SaveSync index shard digest mismatch: {path}")
    shard = validate_shard_document(payload, path=path)
    if shard.layout_id != layout_id or shard.generation != layout_head.generation:
        raise SaveSyncError(f"SaveSync index shard identity mismatch: {path}")
    return shard


def write_head(index_root: Path, head: IndexHead, *, keep_previous: bool = True) -> None:
    path = head_path(index_root)
    if keep_previous and path.exists():
        try:
            _durable_atomic_write_text(
                _previous_head_path(index_root), path.read_text(encoding="utf-8")
            )
        except OSError:
            pass  # best-effort recovery material only, never authoritative
    _durable_atomic_write_text(path, _canonical_json(head_to_dict(head)))


def load_head(index_root: Path) -> Optional[IndexHead]:
    path = head_path(index_root)
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SaveSyncError(f"SaveSync index HEAD is corrupt: {path}") from exc
    return validate_head_document(payload, path=path)


def load_head_safe(index_root: Path) -> Optional[IndexHead]:
    """Never destructive: a corrupt/unreadable HEAD is treated as absent."""
    try:
        return load_head(index_root)
    except SaveSyncError:
        return None


def load_previous_head_safe(index_root: Path) -> Optional[IndexHead]:
    path = _previous_head_path(index_root)
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return validate_head_document(payload, path=path)
    except (OSError, ValueError, SaveSyncError):
        return None


def load_changed_layout_shards(
    index_root: Path,
    head: IndexHead,
    known_generations: dict[str, int],
) -> dict[str, IndexShard]:
    """Fetch only shards whose layout generation advanced past *known_generations*.

    Ready for a future consumer (e.g. Quick Sync); not called by any
    reconciliation decision path in this phase.
    """
    changed: dict[str, IndexShard] = {}
    for layout_id, layout_head in head.layouts.items():
        if known_generations.get(layout_id, 0) < layout_head.generation:
            changed[layout_id] = load_shard(index_root, layout_id, layout_head)
    return changed


def journal_diverged(head: IndexHead, journal_generation: int) -> bool:
    """True when the journal advanced without a matching index rebuild.

    This only ever happens when a client that does not build this index
    (an old client, or a build that failed after journaling but before index
    publication) mutated the dataset. Diagnostic signal only in this phase.
    """
    return journal_generation != head.journal_generation


def build_index(
    *,
    previous_head: Optional[IndexHead],
    dataset_id: Optional[str],
    journal_generation: int,
    layout_groups: dict[str, tuple[IndexGroup, ...]],
    load_previous_shard: Callable[[str, IndexLayoutHead], Optional[IndexShard]],
) -> tuple[IndexHead, dict[str, IndexShard]]:
    """Compute the next HEAD and any shards whose content actually changed.

    *layout_groups* must already be the verified, complete current group set
    for every layout Full Sync is publishing this run (built from actual
    scanned remote content, never from journal inference). A layout absent
    here but present in *previous_head* keeps its previous generation/
    object/digest unchanged \u2014 this function never drops a layout Full Sync
    did not touch.

    *load_previous_shard* is called at most once per already-published
    layout, only to compare its stored groups against the freshly scanned
    ones; a failure to load/validate it is treated as "changed" (always
    conservative: republish rather than silently trust a broken pointer).

    Returns the new head (with placeholder layout entries for anything about
    to change \u2014 the caller must overwrite them with the real
    :class:`IndexLayoutHead` returned by :func:`write_shard` before calling
    :func:`write_head`) plus the shards that must actually be written.
    """
    resolved_dataset_id = (
        previous_head.dataset_id if previous_head is not None else None
    ) or dataset_id or uuid.uuid4().hex
    previous_layouts = dict(previous_head.layouts) if previous_head is not None else {}
    next_layouts = dict(previous_layouts)
    shards_to_write: dict[str, IndexShard] = {}
    any_changed = False
    for layout_id, groups in layout_groups.items():
        sorted_groups = tuple(sorted(groups, key=lambda group: group.group_id))
        previous_layout_head = previous_layouts.get(layout_id)
        unchanged = False
        if previous_layout_head is not None:
            try:
                previous_shard = load_previous_shard(layout_id, previous_layout_head)
            except SaveSyncError:
                previous_shard = None
            unchanged = previous_shard is not None and previous_shard.groups == sorted_groups
        if unchanged:
            continue
        candidate_generation = 1 if previous_layout_head is None else previous_layout_head.generation + 1
        shards_to_write[layout_id] = IndexShard(
            schema_version=SCHEMA_VERSION,
            dataset_id=resolved_dataset_id,
            layout_id=layout_id,
            generation=candidate_generation,
            groups=sorted_groups,
        )
        any_changed = True
    index_generation = (previous_head.index_generation if previous_head is not None else 0) + (
        1 if any_changed else 0
    )
    head = IndexHead(
        schema_version=SCHEMA_VERSION,
        dataset_id=resolved_dataset_id,
        index_generation=index_generation,
        journal_generation=journal_generation,
        layouts=next_layouts,
    )
    return head, shards_to_write


def publish_full_sync_index(
    index_root: Path,
    *,
    dataset_id_hint: Optional[str],
    journal_generation: int,
    layout_groups: dict[str, tuple[IndexGroup, ...]],
) -> Optional[IndexHead]:
    """Full Sync's one entry point: build, write, and publish the index.

    Returns ``None`` (no publication) when nothing changed relative to the
    current HEAD, i.e. this Full Sync's verified remote content is identical
    to what the index already describes.
    """
    previous_head = load_head_safe(index_root)

    def _load_previous(layout_id: str, layout_head: IndexLayoutHead) -> Optional[IndexShard]:
        return load_shard(index_root, layout_id, layout_head)

    head, shards_to_write = build_index(
        previous_head=previous_head,
        dataset_id=dataset_id_hint,
        journal_generation=journal_generation,
        layout_groups=layout_groups,
        load_previous_shard=_load_previous,
    )
    if not shards_to_write:
        return None
    published_layouts = dict(head.layouts)
    for layout_id, shard in shards_to_write.items():
        published_layouts[layout_id] = write_shard(index_root, shard)
    head = IndexHead(
        schema_version=head.schema_version,
        dataset_id=head.dataset_id,
        index_generation=head.index_generation,
        journal_generation=head.journal_generation,
        layouts=published_layouts,
    )
    write_head(index_root, head)
    return head

