"""Save-sync domain model — pure dataclasses, no I/O.

SaveSync v1 is manual, directional, whole-dataset synchronization of
game-progress save data. See :mod:`romcloud.core.save_selection` for which
files qualify, and :mod:`romcloud.services.saves` for the engine that
produces/consumes these types.
"""

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
            "entries": [
                {
                    "relative_path": e.relative_path,
                    "change": e.change.value,
                    "local": _artifact_to_dict(e.local),
                    "remote": _artifact_to_dict(e.remote),
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
            )
            for item in data.get("entries", [])
        )
        return cls(direction=data["direction"], entries=entries)


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
