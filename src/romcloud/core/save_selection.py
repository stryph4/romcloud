"""Save-selection policy — which files under a save root are ROMCloud's
concern for SaveSync v1.

Pure logic: takes plain path strings and never touches the filesystem.
Extend the per-system rule table here as additional real Batocera save
layouts are validated on real hardware — never guess an unverified layout;
an unlisted system is intentionally left unsupported (excluded) rather
than blindly copied.
"""

from __future__ import annotations

import fnmatch
from dataclasses import dataclass
from typing import Optional

FLATPAK_DIRNAME = "flatpak"
XBOX_SYSTEM = "xbox"
XBOX_HDD_RELATIVE_PATH = "xbox_hdd.qcow2"
"""Relative to the ``xbox`` system's own save directory — xemu's single
opaque virtual hard drive file. Never parsed/extracted; treated exactly
like any other whole file."""

_HEX_GLOB = "[0-9A-Fa-f]"
YUZU_ACCOUNT_SAVE_GLOB = (
    "0000000000000000/"
    f"{_HEX_GLOB * 32}/"  # Yuzu account/user id
    f"{_HEX_GLOB * 16}/"  # Nintendo title id
    "**"
)
"""Files within a Yuzu account save for one title.

Batocera's ``/userdata/saves/yuzu`` can contain Yuzu's broader data tree,
including keys, firmware/NAND content, caches, and logs.  Actual game-progress
account saves use Yuzu's save hierarchy
``0000000000000000/<32-hex user id>/<16-hex title id>/...``.  Once inside a
validated title directory every descendant is treated as opaque game-owned
save content: individual games choose their own filenames and subdirectories.
"""


def _match(path: str, pattern: str) -> bool:
    """Match *path* (posix-relative, no leading slash) against *pattern*.

    ``**`` is written in the rule tables purely to signal "any depth" to a
    human reader; ``fnmatch``'s ``*`` already matches ``/`` too, so it is
    normalized to a single ``*`` before matching.
    """
    return fnmatch.fnmatch(path, pattern.replace("**", "*"))


@dataclass(frozen=True)
class SaveSystemRule:
    """Selection rule for one emulator/system's save directory.

    ``include``/``exclude`` are glob patterns matched against a
    candidate's path *relative to that system's own save directory*
    (e.g. ``"memcards/shared_card_1.mcd"``, never including the leading
    system name). ``exclude`` always wins over ``include``.
    """

    include: tuple[str, ...]
    exclude: tuple[str, ...] = ()
    optional: bool = False
    """True for a heavyweight, opt-in-only artifact (Original Xbox)."""
    default_enabled: bool = True
    """Whether this system is synced when present, absent an explicit
    setting. Only meaningful when ``optional`` is True — non-optional
    systems are always eligible."""


# Verified against real Batocera v43 save layouts. A system not listed
# here is intentionally unsupported in v1 (excluded) — see module
# docstring — rather than an invented/guessed layout. 3DS/Citra and
# Dolphin are deliberately absent: they require system-specific selection
# of actual game-progress data that has not yet been validated.
_RULES: dict[str, SaveSystemRule] = {
    "psx": SaveSystemRule(include=("*.srm",)),
    "duckstation": SaveSystemRule(include=("memcards/**",), exclude=("*_resume.sav",)),
    "pcsx2": SaveSystemRule(include=("Mcd*.ps2",), exclude=("sstates/**", "videos/**")),
    "ppsspp": SaveSystemRule(include=("PSP/SAVEDATA/**",), exclude=("PPSSPP_STATE/**",)),
    "xbox360": SaveSystemRule(include=("**",)),
    "yuzu": SaveSystemRule(include=(YUZU_ACCOUNT_SAVE_GLOB,)),
    XBOX_SYSTEM: SaveSystemRule(
        include=(XBOX_HDD_RELATIVE_PATH,), optional=True, default_enabled=False
    ),
}


class SaveSelectionPolicy:
    """Static, extensible table of which save files SaveSync v1 manages."""

    def __init__(self, rules: Optional[dict[str, SaveSystemRule]] = None) -> None:
        self._rules: dict[str, SaveSystemRule] = dict(_RULES if rules is None else rules)

    def known_systems(self) -> frozenset[str]:
        return frozenset(self._rules)

    def is_known_system(self, system: str) -> bool:
        return system in self._rules

    def is_optional(self, system: str) -> bool:
        rule = self._rules.get(system)
        return bool(rule and rule.optional)

    def default_enabled(self, system: str) -> bool:
        rule = self._rules.get(system)
        return True if rule is None else rule.default_enabled

    def is_included(self, system: str, relative_path: str) -> bool:
        """*relative_path* is relative to *system*'s own save directory."""
        rule = self._rules.get(system)
        if rule is None:
            return False
        normalized = relative_path.replace("\\", "/")
        if any(_match(normalized, pattern) for pattern in rule.exclude):
            return False
        return any(_match(normalized, pattern) for pattern in rule.include)

    def excluded_top_level_dirs(self) -> frozenset[str]:
        """Top-level directories under the save root that are never
        emulator systems and must always be skipped entirely (e.g. Flatpak
        app data, which is never game-progress save data)."""
        return frozenset({FLATPAK_DIRNAME})


DEFAULT_SAVE_SELECTION_POLICY = SaveSelectionPolicy()
