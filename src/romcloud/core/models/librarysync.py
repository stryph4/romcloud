"""Library Sync result models."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class LibrarySyncReport:
    direction: str
    metadata_added: int = 0
    metadata_updated: int = 0
    media_added: int = 0
    media_transferred: int = 0
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
            "unchanged": self.unchanged,
            "rendered": self.rendered,
            "conflicts": list(self.conflicts),
            "failures": list(self.failures),
        }
