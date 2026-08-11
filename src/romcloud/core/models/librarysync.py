"""Library Sync result models."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class LibraryImportPreview:
    """Cheap source-import preflight that never hashes or copies media."""

    games_eligible: int
    systems: tuple[str, ...]
    gamelist_files: int
    gamelist_bytes: int
    media_references: int
    artwork_references: int
    video_references: int
    other_media_references: int

    def as_dict(self) -> dict[str, object]:
        estimated_files = self.gamelist_files + self.media_references
        return {
            "games_eligible": self.games_eligible,
            "systems": list(self.systems),
            "gamelist_files": self.gamelist_files,
            "gamelist_bytes": self.gamelist_bytes,
            "media_references": self.media_references,
            "artwork_references": self.artwork_references,
            "video_references": self.video_references,
            "other_media_references": self.other_media_references,
            "estimated_files": estimated_files,
            "estimated_bytes": None,
            "duration_estimate": None,
            "duration_note": (
                "Duration depends on referenced media size and storage/network speed; "
                "ROMCloud reports actual transfer bytes only when changed files are copied."
            ),
        }


@dataclass
class LibrarySyncReport:
    direction: str
    metadata_added: int = 0
    metadata_updated: int = 0
    media_added: int = 0
    media_transferred: int = 0
    media_examined: int = 0
    media_skipped: int = 0
    media_hashed: int = 0
    media_bytes_hashed: int = 0
    media_bytes_transferred: int = 0
    unchanged: int = 0
    rendered: int = 0
    conflicts: list[str] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.failures

    def as_dict(self) -> dict[str, object]:
        return {
            "direction": self.direction,
            "metadata_added": self.metadata_added,
            "metadata_updated": self.metadata_updated,
            "media_added": self.media_added,
            "media_transferred": self.media_transferred,
            "media_examined": self.media_examined,
            "media_skipped": self.media_skipped,
            "media_hashed": self.media_hashed,
            "media_bytes_hashed": self.media_bytes_hashed,
            "media_bytes_transferred": self.media_bytes_transferred,
            "unchanged": self.unchanged,
            "rendered": self.rendered,
            "conflicts": list(self.conflicts),
            "failures": list(self.failures),
        }
