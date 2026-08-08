"""Game domain model."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


@dataclass(frozen=True)
class GameAsset:
    """A single file or directory that is part of a logical game.

    Examples
    --------
    - Single-file game:  one primary asset (the .iso / .rom / etc.)
    - Multi-disc (.m3u): one primary asset (the .m3u), several secondary discs
    - .cue + .bin:       one primary asset (the .cue), one or more .bin tracks
    - PS3 directory:     one primary asset (the BCES00000/ directory entry)
    """

    filename: str
    """Bare filename (no parent directory) of the asset.

    .. important::
        Must equal the exact original source filename.  Batocera's
        ``configgen`` matches per-game settings (e.g.
        ``snes["Some Game.sfc"].*``) against this filename.  Renaming
        during caching would silently break any per-game emulator, core,
        or shader configuration the user has set in Batocera.
    """

    relative_path: str
    """Path relative to the *source ROM root* — e.g. ``ps2/Game.iso``."""

    size_bytes: Optional[int] = None
    is_primary: bool = False


@dataclass
class Game:
    """A logical game in the ROMCloud catalog.

    A game may span multiple :class:`GameAsset` objects (e.g. multi-disc),
    but is treated as a single unit for caching, pinning, and eviction.
    """

    id: str
    system: str
    title: str
    source_provider: str
    source_root: str
    assets: list[GameAsset]
    added_at: datetime
    last_played: Optional[datetime] = None

    # ── factory ───────────────────────────────────────────────────────────────

    @classmethod
    def create(
        cls,
        system: str,
        title: str,
        source_provider: str,
        source_root: str,
        assets: list[GameAsset],
    ) -> Game:
        return cls(
            id=str(uuid.uuid4()),
            system=system,
            title=title,
            source_provider=source_provider,
            source_root=source_root,
            assets=assets,
            added_at=datetime.now(timezone.utc),
        )

    # ── helpers ───────────────────────────────────────────────────────────────

    @property
    def primary_asset(self) -> Optional[GameAsset]:
        """Return the primary asset, falling back to the first asset."""
        for asset in self.assets:
            if asset.is_primary:
                return asset
        return self.assets[0] if self.assets else None

    @property
    def total_size_bytes(self) -> Optional[int]:
        """Sum of all known asset sizes, or None if any size is unknown."""
        sizes = [a.size_bytes for a in self.assets]
        if any(s is None for s in sizes):
            return None
        return sum(s for s in sizes if s is not None)  # type: ignore[misc]

    def __repr__(self) -> str:  # noqa: D105
        return f"<Game id={self.id!r} system={self.system!r} title={self.title!r}>"


def derive_title(filename: str) -> str:
    """Derive a human-readable title from a ROM filename.

    Strips the file extension(s).  More sophisticated parsing
    (region codes, disc markers) may be added later without
    changing the domain contract.
    """
    return Path(filename).stem
