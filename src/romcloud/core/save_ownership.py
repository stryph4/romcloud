"""Conservative attribution of selected save paths to ROMCloud catalog games.

Catalog ``Game.id`` records are authoritative for ownership. Emulator naming
conventions are used only to decide whether a selected save artifact can be
reliably attributed to one of those owned games; they never create ownership.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Iterable

from romcloud.core.models.game import Game

_SONY_TITLE_ID = re.compile(r"(?<![A-Z0-9])([A-Z]{4}[0-9]{5})(?![A-Z0-9])", re.I)
_SWITCH_TITLE_ID = re.compile(r"(?<![0-9A-F])(01[0-9A-F]{14})(?![0-9A-F])", re.I)


@dataclass(frozen=True)
class ManagedSaveIdentity:
    game_id: str
    system: str
    rom_stem: str
    title_ids: frozenset[str]


class ManagedSaveOwnershipPolicy:
    """Answers whether one canonical save path is safely catalog-attributable."""

    def __init__(
        self,
        games: Iterable[Game],
        *,
        ambiguous_local_stems: dict[str, frozenset[str]] | None = None,
    ) -> None:
        ambiguous = ambiguous_local_stems or {}
        identities: list[ManagedSaveIdentity] = []
        for game in games:
            primary = game.primary_asset
            if primary is None:
                continue
            stem = PurePosixPath(primary.filename).stem.casefold()
            if stem in ambiguous.get(game.system, frozenset()):
                # A non-ROMCloud local ROM can produce the same emulator save
                # name. There is no reliable owner, so automatic sync abstains.
                continue
            searchable = " ".join(
                [
                    game.title,
                    *(asset.filename for asset in game.assets),
                    *(asset.relative_path for asset in game.assets),
                ]
            )
            ids = {
                match.group(1).upper()
                for pattern in (_SONY_TITLE_ID, _SWITCH_TITLE_ID)
                for match in pattern.finditer(searchable)
            }
            identities.append(
                ManagedSaveIdentity(game.id, game.system, stem, frozenset(ids))
            )
        by_system: dict[str, list[ManagedSaveIdentity]] = {}
        for identity in identities:
            by_system.setdefault(identity.system, []).append(identity)
        self._by_system = {
            system: tuple(records) for system, records in by_system.items()
        }

    def is_managed_path(self, canonical_path: str) -> bool:
        """Return true only for a path attributable to a catalog game.

        ``canonical_path`` includes the Batocera system prefix, for example
        ``snes/Game.srm`` or ``ps3/rpcs3/dev_hdd0/...``.
        """
        normalized = canonical_path.replace("\\", "/").strip("/")
        system, separator, relative = normalized.partition("/")
        if not separator:
            return False
        identities = self._by_system.get(system, ())
        if not identities:
            return False

        # Root-level save/state formats whose emulator filename is the ROM
        # basename. The selection policy has already constrained extensions.
        if "/" not in relative:
            filename = relative.casefold()
            return any(_root_filename_matches(filename, identity.rom_stem) for identity in identities)

        if system == "mame":
            parts = PurePosixPath(relative).parts
            if len(parts) >= 2 and parts[0] in {"nvram", "state"}:
                shortname = parts[1].casefold()
                return any(identity.rom_stem == shortname for identity in identities)

        if system == "ps3":
            return _title_id_path_matches(relative, identities, system="ps3")
        if system == "ppsspp":
            return _title_id_path_matches(relative, identities, system="ppsspp")
        if system == "yuzu":
            return _title_id_path_matches(relative, identities, system="yuzu")

        # Shared memory cards, opaque virtual disks, serial/CRC-named states,
        # and other emulator-wide trees cannot be assigned to one game safely.
        return False


def _root_filename_matches(filename: str, rom_stem: str) -> bool:
    if filename == f"{rom_stem}.srm" or filename == f"{rom_stem}.sav":
        return True
    if filename.startswith(f"{rom_stem}.state"):
        return True
    return any(
        filename == f"{rom_stem}{suffix}"
        for suffix in (
            ".eep",
            ".sra",
            ".fla",
            ".mpk",
            ".ndr",
            ".d6r",
            ".ram",
            ".mln",
            ".st0",
            ".st1",
            ".st2",
            ".st3",
            ".st4",
            ".st5",
            ".st6",
            ".st7",
            ".st8",
            ".st9",
        )
    )


def _title_id_path_matches(
    relative: str,
    identities: tuple[ManagedSaveIdentity, ...],
    *,
    system: str,
) -> bool:
    parts = PurePosixPath(relative).parts
    candidates: set[str] = set()
    if system == "ps3":
        if len(parts) >= 6 and parts[:3] == ("rpcs3", "dev_hdd0", "home"):
            if parts[4] == "savedata":
                candidates.add(parts[5].split("-")[0].upper())
        if len(parts) >= 4 and parts[:3] == ("rpcs3", "dev_hdd0", "game"):
            candidates.add(parts[3].upper())
        if parts and ".SAVESTAT" in parts[-1].upper():
            candidates.add(parts[0].split("-")[0].upper())
    elif system == "ppsspp":
        if len(parts) >= 3 and parts[:2] == ("PSP", "SAVEDATA"):
            candidates.add(parts[2][:9].upper())
    elif system == "yuzu":
        if len(parts) >= 4 and parts[0] == "0000000000000000":
            candidates.add(parts[2].upper())
    if not candidates:
        return False
    return any(identity.title_ids.intersection(candidates) for identity in identities)


ALLOW_NO_AUTOMATIC_SAVES = ManagedSaveOwnershipPolicy(())
