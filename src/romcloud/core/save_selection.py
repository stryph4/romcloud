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
    system name). ``root_include`` uses the same syntax but only matches
    files directly in the system directory. This distinction is important
    for Batocera's libretro layout, where native ``.srm`` files and
    savestates share a directory and nested non-save trees must not match by
    accident. ``exclude`` always wins over either kind of include.
    """

    include: tuple[str, ...]
    root_include: tuple[str, ...] = ()
    exclude: tuple[str, ...] = ()
    optional: bool = False
    """True for a heavyweight, opt-in-only artifact (Original Xbox)."""
    default_enabled: bool = True
    """Whether this system is synced when present, absent an explicit
    setting. Only meaningful when ``optional`` is True — non-optional
    systems are always eligible."""


# Batocera's libretro generator places both save files and savestates directly
# under /userdata/saves/<system>. RetroArch gives native save RAM the .srm
# extension. These are the current Batocera system names whose default generator
# is libretro; the N64 variants are also included because Batocera selects
# libretro/mupen64plus-next on several supported hardware profiles. Keeping this
# an explicit audited set avoids treating an arbitrary top-level directory as a
# save system while allowing the same safe root-file rule to be reused.
_RETROARCH_SRM_SYSTEMS = frozenset(
    """
    3do adam advision amiga500 amiga1200 amigacd32 amigacdtv amstradcpc pcw
    apfm1000 arcadia archimedes arduboy astrocade atari2600 atari5200
    atari7800 atari800 atarist atom atomiswave bbcmicro bk bennugd c64
    camplynx cassettevision cave3rd cavestory cgenie channelf coco
    colecovision crvision ctvboy dice dos dragon64 mc10 dreamcast electron
    enterprise fbneo fds fm7 gaelco gamate gameandwatch gamecom gamegear
    gamepock gb gb2players gba gbc gbc2players gmaster gong gp32 gx4000
    intellivision jaguar jaguarcd laser310 lcdgames loopy lowresnx lutro lynx
    macintosh mame mastersystem megadrive megadrive-msu megaduck mrboom msx1
    msx2 msx2+ msxturbor mz2000 mz2500 mz700 mz800 mz80k multivision n64
    n64dd namco22 naomi naomi2 nds neogeo neogeocd nes ngp ngpc odyssey2
    oricatmos pc60 pc88 pc98 pcengine pcenginecd pcfx pdp1 pico pico8 pokemini
    prboom psx pv1000 pv2000 pc80 quake reminiscence rx78 satellaview saturn
    scv sega32x megacd sc3000 segaai beena sg1000 sgb sgb-msu1 socrates
    spectravideo superbroswar sufami supracan snes snes-msu1 supergrafx
    supervision sv8000 systemsp thomson ti99 tic80 trs80 tutor tvc tvgames
    uzebox vc4000 vectrex vgmplay videopacplus vircon32 virtualboy vis vsmile
    wasm4 wswan wswanc x1 x68000 xegs xrick zc210 zx81 zxspectrum
    """.split()
)


# Verified against real Batocera v43 save layouts and the current Batocera
# generators. Structured emulator-owned trees remain explicit. 3DS/Citra and
# Dolphin are deliberately absent: selecting their actual game-progress data
# safely requires more specific validated rules.
_RULES: dict[str, SaveSystemRule] = {
    system: SaveSystemRule(include=(), root_include=("*.srm",))
    for system in _RETROARCH_SRM_SYSTEMS
}
_RULES.update(
    {
        # Standalone Mupen64Plus stores these native per-game artifacts in the
        # same n64 directory as its savestates. Root-only matching keeps states
        # out.
        "n64": SaveSystemRule(
            include=(),
            root_include=("*.srm", "*.eep", "*.sra", "*.fla", "*.mpk", "*.sav"),
        ),
        "n64dd": SaveSystemRule(
            include=(),
            root_include=(
                "*.srm",
                "*.eep",
                "*.sra",
                "*.fla",
                "*.mpk",
                "*.sav",
                "*.ndr",
                "*.d6r",
                "*.ram",
            ),
        ),
        # Standalone melonDS uses per-game .sav files here. Its DLDI/DSi SD
        # images are shared .bin files and intentionally do not match.
        "nds": SaveSystemRule(include=(), root_include=("*.srm", "*.sav")),
        # MAME's NVRAM subtree is game progress; cfg, input, state, diff,
        # comments, and plugins are separate sibling trees and remain excluded.
        "mame": SaveSystemRule(include=("nvram/**",), root_include=("*.srm",)),
        "duckstation": SaveSystemRule(
            include=("memcards/**",), exclude=("*_resume.sav",)
        ),
        "pcsx2": SaveSystemRule(
            include=("Mcd*.ps2",), exclude=("sstates/**", "videos/**")
        ),
        "ppsspp": SaveSystemRule(
            include=("PSP/SAVEDATA/**",), exclude=("PPSSPP_STATE/**",)
        ),
        "xbox360": SaveSystemRule(include=("**",)),
        "yuzu": SaveSystemRule(include=(YUZU_ACCOUNT_SAVE_GLOB,)),
        XBOX_SYSTEM: SaveSystemRule(
            include=(XBOX_HDD_RELATIVE_PATH,), optional=True, default_enabled=False
        ),
    }
)


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
        if "/" not in normalized and any(
            _match(normalized, pattern) for pattern in rule.root_include
        ):
            return True
        return any(_match(normalized, pattern) for pattern in rule.include)

    def excluded_top_level_dirs(self) -> frozenset[str]:
        """Top-level directories under the save root that are never
        emulator systems and must always be skipped entirely (e.g. Flatpak
        app data, which is never game-progress save data)."""
        return frozenset({FLATPAK_DIRNAME})


DEFAULT_SAVE_SELECTION_POLICY = SaveSelectionPolicy()
