"""Save/state synchronization domain model — pure dataclasses, no I/O."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional


@dataclass(frozen=True)
class SaveArtifact:
    """One selected save file, identified by its path relative to the save
    root (e.g. ``"duckstation/memcards/shared_card_1.mcd"``)."""

    relative_path: str
    size_bytes: int
    content_hash: str
    """sha256 hex digest of the file's content."""


class SaveChangeKind(Enum):
    ADDED = "added"
    CHANGED = "changed"
    REMOVED = "removed"
    UNCHANGED = "unchanged"


@dataclass(frozen=True)
class SaveDiffEntry:
    """One path's status in a preview — never both ``local``/``remote`` None."""

    relative_path: str
    change: SaveChangeKind
    local: Optional[SaveArtifact]
    remote: Optional[SaveArtifact]
    conflict: bool = False
    """Both sides changed since the last shared state.

    Force upload/download may still replace the destination, but the preview
    must surface this before the deliberate confirmation step.
    """


def _artifact_to_dict(artifact: Optional[SaveArtifact]) -> Optional[dict]:
    if artifact is None:
        return None
    return {
        "relative_path": artifact.relative_path,
        "size_bytes": artifact.size_bytes,
        "content_hash": artifact.content_hash,
    }


def _artifact_from_dict(payload: Optional[dict]) -> Optional[SaveArtifact]:
    if payload is None:
        return None
    return SaveArtifact(
        relative_path=payload["relative_path"],
        size_bytes=int(payload["size_bytes"]),
        content_hash=payload["content_hash"],
    )


@dataclass(frozen=True)
class SaveDiff:
    """Preview of what an upload/download would do.

    Building a :class:`SaveDiff` never modifies anything on disk — see
    ``SaveSyncService.commit_upload``/``commit_download`` for the
    stage/verify/commit step that actually applies it.
    """

    direction: str
    """``"upload"`` or ``"download"``."""

    entries: tuple[SaveDiffEntry, ...]
    excluded_files: int = 0
    excluded_bytes: int = 0
    optional_groups: tuple[tuple[str, int, int], ...] = ()
    """``(group_id, file_count, bytes)`` summaries for disabled groups."""

    def _by_kind(self, kind: SaveChangeKind) -> tuple[SaveDiffEntry, ...]:
        return tuple(e for e in self.entries if e.change == kind)

    @property
    def added(self) -> tuple[SaveDiffEntry, ...]:
        return self._by_kind(SaveChangeKind.ADDED)

    @property
    def changed(self) -> tuple[SaveDiffEntry, ...]:
        return self._by_kind(SaveChangeKind.CHANGED)

    @property
    def removed(self) -> tuple[SaveDiffEntry, ...]:
        return self._by_kind(SaveChangeKind.REMOVED)

    @property
    def unchanged(self) -> tuple[SaveDiffEntry, ...]:
        return self._by_kind(SaveChangeKind.UNCHANGED)

    @property
    def conflicts(self) -> tuple[SaveDiffEntry, ...]:
        return tuple(e for e in self.entries if e.conflict)

    @property
    def transfer_bytes(self) -> int:
        """Bytes that must actually reach the destination (added + changed)."""
        total = 0
        for entry in self.entries:
            if entry.change in (SaveChangeKind.ADDED, SaveChangeKind.CHANGED):
                source = entry.local if self.direction == "upload" else entry.remote
                if source is not None:
                    total += source.size_bytes
        return total

    def to_dict(self) -> dict:
        return {
            "direction": self.direction,
            "excluded_files": self.excluded_files,
            "excluded_bytes": self.excluded_bytes,
            "optional_groups": [
                {"group": group, "files": files, "bytes": size_bytes}
                for group, files, size_bytes in self.optional_groups
            ],
            "entries": [
                {
                    "relative_path": e.relative_path,
                    "change": e.change.value,
                    "local": _artifact_to_dict(e.local),
                    "remote": _artifact_to_dict(e.remote),
                    "conflict": e.conflict,
                }
                for e in self.entries
            ],
        }

    @classmethod
    def from_dict(cls, data: dict) -> "SaveDiff":
        entries = tuple(
            SaveDiffEntry(
                relative_path=item["relative_path"],
                change=SaveChangeKind(item["change"]),
                local=_artifact_from_dict(item.get("local")),
                remote=_artifact_from_dict(item.get("remote")),
                conflict=bool(item.get("conflict", False)),
            )
            for item in data.get("entries", [])
        )
        optional_groups = tuple(
            (str(item["group"]), int(item["files"]), int(item["bytes"]))
            for item in data.get("optional_groups", [])
        )
        return cls(
            direction=data["direction"],
            entries=entries,
            excluded_files=int(data.get("excluded_files", 0)),
            excluded_bytes=int(data.get("excluded_bytes", 0)),
            optional_groups=optional_groups,
        )


class SaveReconcileAction(Enum):
    UPLOAD = "upload"
    DOWNLOAD = "download"
    CONFLICT = "conflict"
    UNCHANGED = "unchanged"


@dataclass(frozen=True)
class SaveReconcileEntry:
    relative_path: str
    action: SaveReconcileAction
    local: Optional[SaveArtifact]
    remote: Optional[SaveArtifact]
    baseline: Optional[SaveArtifact]


@dataclass(frozen=True)
class SaveReconcilePlan:
    """Three-way comparison against the last state shared by both sides."""

    entries: tuple[SaveReconcileEntry, ...]
    excluded_files: int = 0
    excluded_bytes: int = 0
    optional_groups: tuple[tuple[str, int, int], ...] = ()
    scope: str = "managed_games"

    def _by_action(self, action: SaveReconcileAction) -> tuple[SaveReconcileEntry, ...]:
        return tuple(entry for entry in self.entries if entry.action is action)

    @property
    def uploads(self) -> tuple[SaveReconcileEntry, ...]:
        return self._by_action(SaveReconcileAction.UPLOAD)

    @property
    def downloads(self) -> tuple[SaveReconcileEntry, ...]:
        return self._by_action(SaveReconcileAction.DOWNLOAD)

    @property
    def conflicts(self) -> tuple[SaveReconcileEntry, ...]:
        return self._by_action(SaveReconcileAction.CONFLICT)

    @property
    def unchanged(self) -> tuple[SaveReconcileEntry, ...]:
        return self._by_action(SaveReconcileAction.UNCHANGED)

    @property
    def upload_bytes(self) -> int:
        return sum(entry.local.size_bytes for entry in self.uploads if entry.local)

    @property
    def download_bytes(self) -> int:
        return sum(entry.remote.size_bytes for entry in self.downloads if entry.remote)

    def to_dict(self) -> dict:
        return {
            "uploads": len(self.uploads),
            "downloads": len(self.downloads),
            "conflicts": len(self.conflicts),
            "unchanged": len(self.unchanged),
            "upload_bytes": self.upload_bytes,
            "download_bytes": self.download_bytes,
            "excluded_files": self.excluded_files,
            "excluded_bytes": self.excluded_bytes,
            "optional_groups": [
                {"group": group, "files": files, "bytes": size_bytes}
                for group, files, size_bytes in self.optional_groups
            ],
            "conflict_paths": [entry.relative_path for entry in self.conflicts],
            "scope": self.scope,
        }


@dataclass(frozen=True)
class SaveReconcileReport:
    revision: str
    timestamp: str
    uploaded: int
    downloaded: int
    conflicts: int
    unchanged: int
    upload_bytes: int
    download_bytes: int
    conflict_paths: tuple[str, ...] = ()
    scope: str = "managed_games"

    def to_dict(self) -> dict:
        return {
            "revision": self.revision,
            "timestamp": self.timestamp,
            "uploaded": self.uploaded,
            "downloaded": self.downloaded,
            "conflicts": self.conflicts,
            "unchanged": self.unchanged,
            "upload_bytes": self.upload_bytes,
            "download_bytes": self.download_bytes,
            "conflict_paths": list(self.conflict_paths),
            "scope": self.scope,
        }


@dataclass(frozen=True)
class SaveSyncRecord:
    """A successfully committed upload or download, including the exact
    manifest (relative path, size, content hash) that was applied."""

    revision: str
    timestamp: str
    """ISO-8601 UTC."""
    device_id: str
    manifest: tuple[SaveArtifact, ...]

    @property
    def artifact_count(self) -> int:
        return len(self.manifest)

    @property
    def total_bytes(self) -> int:
        return sum(a.size_bytes for a in self.manifest)


@dataclass(frozen=True)
class SaveSyncState:
    """Persisted SaveSync state. ``last_upload``/``last_download`` only
    ever advance after a fully verified commit."""

    device_id: str
    last_upload: Optional[SaveSyncRecord] = None
    last_download: Optional[SaveSyncRecord] = None
    shared_manifest: tuple[SaveArtifact, ...] = ()
    """Last content known to be identical on local and remote sides."""
    last_reconcile: Optional[SaveReconcileReport] = None
