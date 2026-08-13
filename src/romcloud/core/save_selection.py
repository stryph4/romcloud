"""Positive registry for the Batocera save layouts ROMCloud supports.

The registry is intentionally the sole eligibility boundary.  Filesystem
consumers start at :class:`SaveLayout` roots returned by :meth:`watch_roots`;
they must never discover arbitrary children of ``/userdata/saves`` and then
try to exclude unsafe content afterward.
"""

from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Optional

FLATPAK_DIRNAME = "flatpak"  # compatibility constant; never traversed
XBOX_SYSTEM = "xbox"
XBOX_HDD_RELATIVE_PATH = "xbox_hdd.qcow2"

# Retained as a compatibility import for older callers/configuration.  RPCS3
# installed applications are no longer represented by an eligible layout.
RPCS3_INSTALLED_GAMES_GROUP = "rpcs3_installed_games"
RPCS3_DEV_HDD0_PREFIX = "rpcs3/dev_hdd0"

_HEX_GLOB = "[0-9A-Fa-f]"
YUZU_ACCOUNT_SAVE_GLOB = (
    "0000000000000000/"
    f"{_HEX_GLOB * 32}/"
    f"{_HEX_GLOB * 16}/"
    "**"
)


def _match(path: str, pattern: str) -> bool:
    return fnmatch.fnmatch(path, pattern.replace("**", "*"))


@dataclass(frozen=True)
class SaveOptionalGroup:
    """Compatibility model for old custom policies.

    The production registry deliberately contains no optional RPCS3
    application-data group.
    """

    group_id: str
    include: tuple[str, ...]


@dataclass(frozen=True)
class SaveSystemRule:
    """Legacy/custom rule input retained for API compatibility."""

    include: tuple[str, ...]
    root_include: tuple[str, ...] = ()
    exclude: tuple[str, ...] = ()
    optional_groups: tuple[SaveOptionalGroup, ...] = ()
    optional: bool = False
    default_enabled: bool = True


@dataclass(frozen=True)
class SaveSelectionDecision:
    included: bool
    optional_group: Optional[str] = None
    excluded_reason: str = ""


@dataclass(frozen=True)
class SaveLayout:
    """One auditable, positively traversable save layout.

    ``root_pattern`` is relative to ``/userdata/saves/<system>``. Literal
    segments are opened directly; brace tokens are enumerated one
    level at a time and validated before ROMCloud descends into them.
    ``eligible_files`` is relative to the resolved root.  A non-recursive
    layout examines direct children only.
    """

    layout_id: str
    system: str
    root_pattern: str
    recursive: bool
    eligible_files: tuple[str, ...]
    exclusions: tuple[str, ...] = ()
    shared: bool = False
    group_by: str = "root_stem"
    requires_opt_in: bool = False
    default_enabled: bool = True
    description: str = ""


@dataclass(frozen=True)
class SaveGroupDescriptor:
    """Stable conflict/dirty-state identity for one supported path."""

    group_id: str
    layout_id: str
    system: str
    shared: bool


@dataclass(frozen=True)
class SaveWatchRoot:
    """A concrete approved root reusable by discovery and a future watcher."""

    layout_id: str
    path: Path
    canonical_root: str
    recursive: bool


_TOKEN_VALIDATORS = {
    "{digits8}": re.compile(r"[0-9]{8}").fullmatch,
    "{hex8}": re.compile(r"[0-9A-Fa-f]{8}").fullmatch,
    "{hex32}": re.compile(r"[0-9A-Fa-f]{32}").fullmatch,
    "{hex16}": re.compile(r"[0-9A-Fa-f]{16}").fullmatch,
    "{sony_title_id}": re.compile(r"[A-Za-z]{4}[0-9]{5}").fullmatch,
    "{dolphin_gc_region}": frozenset({"EUR", "USA", "JAP", "JPN"}).__contains__,
    "{dolphin_gc_card}": frozenset({"Card A", "Card B"}).__contains__,
    # Dolphin's game, downloadable-channel, and game-with-channel title
    # namespaces. System channels, DLC, hidden channels, and other NAND
    # namespaces are deliberately not traversable SaveSync roots.
    "{dolphin_wii_save_type}": frozenset(
        {"00010000", "00010001", "00010004"}
    ).__contains__,
}

_DOLPHIN_GC_MEMORY_CARD_FILES = tuple(
    filename
    for slot in ("A", "B")
    for region in ("EUR", "USA", "JAP", "JPN")
    for filename in (
        f"MemoryCard{slot}.{region}.raw",
        *(f"MemoryCard{slot}.{region}.{blocks}.raw" for blocks in (59, 123, 251, 507, 1019)),
    )
)


# Batocera systems whose audited default is a root-only RetroArch save/state.
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

_SPECIAL_ROOT_SYSTEMS = frozenset({"mame", "n64", "n64dd", "nds"})


def _layout(
    layout_id: str,
    system: str,
    *,
    root: str = "",
    recursive: bool = False,
    files: tuple[str, ...] = ("*",),
    exclusions: tuple[str, ...] = (),
    shared: bool = False,
    group_by: str = "root_stem",
    requires_opt_in: bool = False,
    default_enabled: bool = True,
    description: str = "",
) -> SaveLayout:
    return SaveLayout(
        layout_id,
        system,
        root,
        recursive,
        files,
        exclusions,
        shared,
        group_by,
        requires_opt_in,
        default_enabled,
        description,
    )


_LAYOUTS: tuple[SaveLayout, ...] = tuple(
    _layout(
        f"retroarch-root-{system}",
        system,
        files=("*.srm", "*.state*"),
        description="Root-only RetroArch save RAM and save states",
    )
    for system in sorted(_RETROARCH_SRM_SYSTEMS - _SPECIAL_ROOT_SYSTEMS)
) + (
    _layout(
        "n64-root",
        "n64",
        files=("*.srm", "*.eep", "*.sra", "*.fla", "*.mpk", "*.sav", "*.state*", "*.st?", "*.st??"),
        group_by="n64_title",
        description="Standalone/RetroArch N64 root saves and states",
    ),
    _layout(
        "n64dd-root",
        "n64dd",
        files=(
            "*.srm", "*.eep", "*.sra", "*.fla", "*.mpk", "*.sav",
            "*.ndr", "*.d6r", "*.ram", "*.state*", "*.st?", "*.st??",
        ),
        group_by="n64_title",
        description="N64DD root saves, persistent disks, and states",
    ),
    _layout(
        "nds-root",
        "nds",
        files=("*.srm", "*.sav", "*.state*", "*.mln"),
        description="Nintendo DS root saves and states; shared SD images omitted",
    ),
    _layout("mame-root", "mame", files=("*.srm", "*.state*")),
    _layout("mame-nvram", "mame", root="nvram", recursive=True, group_by="first_descendant"),
    _layout("mame-state", "mame", root="state", recursive=True, group_by="first_descendant"),
    _layout(
        "duckstation-memory-cards",
        "duckstation",
        root="memcards",
        recursive=True,
        exclusions=("*_resume.sav",),
        shared=True,
        group_by="layout",
    ),
    _layout("duckstation-root-sav", "duckstation", files=("*.sav",), exclusions=("*_resume.sav",)),
    _layout(
        "pcsx2-legacy-memory-cards", "pcsx2", files=("Mcd*.ps2",),
        shared=True, group_by="layout",
    ),
    _layout(
        "pcsx2-legacy-states", "pcsx2", root="sstates", recursive=True,
        shared=True, group_by="layout",
    ),
    _layout(
        "pcsx2-memory-cards", "ps2", root="pcsx2", files=("Mcd*.ps2",),
        shared=True, group_by="layout",
    ),
    _layout(
        "pcsx2-states", "ps2", root="pcsx2/sstates", recursive=True,
        shared=True, group_by="layout",
    ),
    _layout(
        "ppsspp-savedata", "ppsspp", root="PSP/SAVEDATA", recursive=True,
        group_by="first_descendant",
    ),
    _layout("ppsspp-states", "ppsspp", root="PPSSPP_STATE", recursive=True, group_by="root_stem"),
    _layout(
        "rpcs3-savedata",
        "ps3",
        root=f"{RPCS3_DEV_HDD0_PREFIX}/home/{{digits8}}/savedata",
        recursive=True,
        group_by="first_descendant",
        description="RPCS3 per-account savedata only",
    ),
    _layout(
        "rpcs3-trophies",
        "ps3",
        root=f"{RPCS3_DEV_HDD0_PREFIX}/home/{{digits8}}/trophy",
        recursive=True,
        group_by="first_descendant",
        shared=True,
    ),
    _layout(
        "rpcs3-virtual-memory-cards",
        "ps3",
        root=f"{RPCS3_DEV_HDD0_PREFIX}/savedata/vmc",
        recursive=True,
        shared=True,
        group_by="layout",
    ),
    _layout(
        "rpcs3-savestates",
        "ps3",
        root="{sony_title_id}",
        files=("*.SAVESTAT*",),
        group_by="root",
    ),
    _layout(
        "xenia-content",
        "xbox360",
        recursive=True,
        shared=True,
        group_by="layout",
        description="Intentionally supported opaque Xenia content tree",
    ),
    _layout(
        "yuzu-account-title-save",
        "yuzu",
        root="0000000000000000/{hex32}/{hex16}",
        recursive=True,
        group_by="root",
        description="Yuzu user/title saves; NAND, keys, cache, shaders and config omitted",
    ),
    _layout(
        "dolphin-gc-memory-card-images",
        "dolphin-emu",
        root="GC",
        files=_DOLPHIN_GC_MEMORY_CARD_FILES,
        shared=True,
        group_by="root_stem",
        description="Opaque Dolphin GameCube memory-card images",
    ),
    _layout(
        "dolphin-gc-gci-saves",
        "dolphin-emu",
        root="GC/{dolphin_gc_region}/{dolphin_gc_card}",
        files=("*.gci",),
        group_by="root_file",
        description="Dolphin GCI folder-card saves, one physical GCI per conflict unit",
    ),
    _layout(
        "dolphin-wii-title-saves",
        "dolphin-emu",
        root="Wii/title/{dolphin_wii_save_type}/{hex8}/data",
        recursive=True,
        group_by="root",
        description="Per-title Dolphin Wii save data; other NAND content omitted",
    ),
    _layout(
        "xemu-hdd",
        XBOX_SYSTEM,
        files=(XBOX_HDD_RELATIVE_PATH,),
        shared=True,
        group_by="layout",
        requires_opt_in=True,
        default_enabled=False,
        description="Whole opaque xemu HDD image",
    ),
)


def _segments(value: str) -> tuple[str, ...]:
    normalized = value.replace("\\", "/").strip("/")
    return tuple(PurePosixPath(normalized).parts) if normalized else ()


def _segment_matches(pattern: str, value: str) -> bool:
    validator = _TOKEN_VALIDATORS.get(pattern)
    return bool(validator(value)) if validator is not None else pattern == value


def _match_layout(
    layout: SaveLayout, relative_path: str
) -> tuple[tuple[str, ...], tuple[str, ...]] | None:
    if (
        not relative_path
        or "\\" in relative_path
        or relative_path.startswith("/")
        or relative_path.endswith("/")
    ):
        return None
    pure = PurePosixPath(relative_path)
    parts = pure.parts
    if pure.as_posix() != relative_path or any(part in {".", ".."} for part in parts):
        return None
    roots = _segments(layout.root_pattern)
    if len(parts) <= len(roots):
        return None
    actual_root = parts[: len(roots)]
    if any(not _segment_matches(pattern, actual) for pattern, actual in zip(roots, actual_root)):
        return None
    remainder = parts[len(roots) :]
    if not layout.recursive and len(remainder) != 1:
        return None
    remainder_path = "/".join(remainder)
    if any(_match(remainder_path, pattern) for pattern in layout.exclusions):
        return None
    if not any(_match(remainder_path, pattern) for pattern in layout.eligible_files):
        return None
    return actual_root, remainder


def _root_stem(filename: str) -> str:
    folded = filename.casefold()
    for marker in (".savestat", ".state"):
        index = folded.find(marker)
        if index > 0:
            return filename[:index].casefold()
    return PurePosixPath(filename).stem.casefold()


def _group_key(layout: SaveLayout, root: tuple[str, ...], remainder: tuple[str, ...]) -> str:
    if layout.group_by == "layout":
        return "dataset"
    if layout.group_by == "root":
        return "/".join(root).casefold() or "root"
    if layout.group_by == "first_descendant":
        prefix = "/".join(root).casefold()
        child = remainder[0].casefold()
        return f"{prefix}/{child}".strip("/")
    if layout.group_by == "root_stem":
        return _root_stem(remainder[-1])
    if layout.group_by == "root_file":
        filename = PurePosixPath(remainder[-1]).stem
        return "/".join((*root, filename)).casefold()
    if layout.group_by == "n64_title":
        # Mupen can emit controller-slot files such as ``Game.1.sav`` beside
        # ``Game.srm``. They are one game dataset, so independent edits across
        # those siblings must conflict rather than merge silently.
        return re.sub(r"\.\d+$", "", _root_stem(remainder[-1]))
    raise ValueError(f"unknown SaveSync grouping strategy: {layout.group_by}")


def _legacy_layouts(rules: dict[str, SaveSystemRule]) -> tuple[SaveLayout, ...]:
    """Conservatively turn the small public custom-rule API into roots."""
    result: list[SaveLayout] = []
    for system, rule in sorted(rules.items()):
        direct = list(rule.root_include)
        for pattern in rule.include:
            normalized = pattern.replace("\\", "/")
            if normalized == "**":
                continue
            if "/" not in normalized:
                direct.append(normalized)
                continue
            if normalized.endswith("/**"):
                prefix = normalized[:-3].strip("/")
                if prefix and not any(char in prefix for char in "*?["):
                    result.append(
                        _layout(
                            f"custom-{system}-{prefix.replace('/', '-')}",
                            system,
                            root=prefix,
                            recursive=True,
                            exclusions=rule.exclude,
                            requires_opt_in=rule.optional,
                            default_enabled=rule.default_enabled,
                        )
                    )
        if direct:
            result.append(
                _layout(
                    f"custom-{system}-root",
                    system,
                    files=tuple(dict.fromkeys(direct)),
                    exclusions=rule.exclude,
                    requires_opt_in=rule.optional,
                    default_enabled=rule.default_enabled,
                )
            )
        if "**" in rule.include:
            result.append(
                _layout(
                    f"custom-{system}-tree",
                    system,
                    recursive=True,
                    exclusions=rule.exclude,
                    requires_opt_in=rule.optional,
                    default_enabled=rule.default_enabled,
                )
            )
    return tuple(result)


class SaveSelectionPolicy:
    """Central registry, matcher, conflict grouper, and watcher resolver."""

    def __init__(
        self,
        rules: Optional[dict[str, SaveSystemRule]] = None,
        *,
        layouts: Optional[tuple[SaveLayout, ...]] = None,
    ) -> None:
        if rules is not None and layouts is not None:
            raise ValueError("provide rules or layouts, not both")
        self._legacy_rules = dict(rules) if rules is not None else None
        selected = (
            layouts
            if layouts is not None
            else (_legacy_layouts(rules) if rules is not None else _LAYOUTS)
        )
        self._layouts = tuple(selected)
        ids = [layout.layout_id for layout in self._layouts]
        if len(ids) != len(set(ids)):
            raise ValueError("SaveSync layout ids must be unique")
        self._by_id = {layout.layout_id: layout for layout in self._layouts}

    @property
    def layouts(self) -> tuple[SaveLayout, ...]:
        return self._layouts

    def layout(self, layout_id: str) -> SaveLayout:
        return self._by_id[layout_id]

    def known_systems(self) -> frozenset[str]:
        return frozenset(layout.system for layout in self._layouts)

    def is_known_system(self, system: str) -> bool:
        return any(layout.system == system for layout in self._layouts)

    def is_optional(self, system: str) -> bool:
        layouts = tuple(layout for layout in self._layouts if layout.system == system)
        return bool(layouts) and all(layout.requires_opt_in for layout in layouts)

    def default_enabled(self, system: str) -> bool:
        layouts = tuple(layout for layout in self._layouts if layout.system == system)
        return all(layout.default_enabled for layout in layouts) if layouts else True

    def classify(
        self,
        system: str,
        relative_path: str,
        *,
        enabled_optional_groups: frozenset[str] = frozenset(),
    ) -> SaveSelectionDecision:
        # Keep exact legacy optional-group behavior for external custom policies.
        if self._legacy_rules is not None:
            rule = self._legacy_rules.get(system)
            if rule is None:
                return SaveSelectionDecision(False, excluded_reason="unsupported system")
            normalized = relative_path.replace("\\", "/")
            if any(_match(normalized, pattern) for pattern in rule.exclude):
                return SaveSelectionDecision(False, excluded_reason="generated/cache exclusion")
            for group in rule.optional_groups:
                if any(_match(normalized, pattern) for pattern in group.include):
                    enabled = group.group_id in enabled_optional_groups
                    return SaveSelectionDecision(
                        enabled,
                        optional_group=group.group_id,
                        excluded_reason="" if enabled else "optional large-content group disabled",
                    )
            if "/" not in normalized and any(
                _match(normalized, pattern) for pattern in rule.root_include
            ):
                return SaveSelectionDecision(True)
            if any(_match(normalized, pattern) for pattern in rule.include):
                return SaveSelectionDecision(True)
            return SaveSelectionDecision(False, excluded_reason="not selected by policy")

        if any(
            layout.system == system and _match_layout(layout, relative_path) is not None
            for layout in self._layouts
        ):
            return SaveSelectionDecision(True)
        reason = (
            "unsupported system"
            if not self.is_known_system(system)
            else "not selected by registry"
        )
        return SaveSelectionDecision(False, excluded_reason=reason)

    def is_included(
        self,
        system: str,
        relative_path: str,
        *,
        enabled_optional_groups: frozenset[str] = frozenset(),
    ) -> bool:
        return self.classify(
            system,
            relative_path,
            enabled_optional_groups=enabled_optional_groups,
        ).included

    def is_canonical_path_supported(self, canonical_path: str) -> bool:
        return self.group_for_path(canonical_path) is not None

    def group_for_path(self, canonical_path: str) -> SaveGroupDescriptor | None:
        if (
            not canonical_path
            or "\\" in canonical_path
            or canonical_path.startswith("/")
            or canonical_path.endswith("/")
            or PurePosixPath(canonical_path).as_posix() != canonical_path
        ):
            return None
        system, separator, relative = canonical_path.partition("/")
        if not separator or any(
            part in {".", ".."} for part in PurePosixPath(canonical_path).parts
        ):
            return None
        for layout in self._layouts:
            if layout.system != system:
                continue
            matched = _match_layout(layout, relative)
            if matched is None:
                continue
            root, remainder = matched
            key = _group_key(layout, root, remainder)
            return SaveGroupDescriptor(
                group_id=f"{layout.layout_id}/{key}",
                layout_id=layout.layout_id,
                system=system,
                shared=layout.shared,
            )
        return None

    def watch_roots(
        self,
        root: Path,
        *,
        enabled_optional_systems: frozenset[str] = frozenset(),
        canonical_prefix: str = "",
    ) -> tuple[SaveWatchRoot, ...]:
        """Resolve only existing approved roots, validating dynamic segments.

        ``canonical_prefix`` maps an external physical tree into the canonical
        namespace, currently used for Batocera's legacy RPCS3 ``dev_hdd0``.
        """
        root = Path(root)
        # A configured SaveSync root may be a symlink (for example a mounted
        # remote-data indirection). Treat that root as valid while still
        # refusing symlink traversal below it.
        if not root.is_dir():
            return ()
        prefix = _segments(canonical_prefix)
        resolved: list[SaveWatchRoot] = []
        for layout in self._layouts:
            if layout.requires_opt_in and layout.system not in enabled_optional_systems:
                continue
            pattern = (layout.system, *_segments(layout.root_pattern))
            if prefix:
                if len(prefix) > len(pattern) or any(
                    segment in _TOKEN_VALIDATORS or segment != actual
                    for segment, actual in zip(pattern[: len(prefix)], prefix)
                ):
                    continue
                remaining = pattern[len(prefix) :]
                candidates: list[tuple[Path, tuple[str, ...]]] = [(root, prefix)]
            else:
                remaining = pattern
                candidates = [(root, ())]

            for segment in remaining:
                validator = _TOKEN_VALIDATORS.get(segment)
                next_candidates: list[tuple[Path, tuple[str, ...]]] = []
                for physical, canonical in candidates:
                    if validator is None:
                        candidate = physical / segment
                        if not candidate.is_symlink() and candidate.is_dir():
                            next_candidates.append((candidate, (*canonical, segment)))
                        continue
                    if not physical.is_dir() or physical.is_symlink():
                        continue
                    for candidate in sorted(physical.iterdir(), key=lambda path: path.name):
                        # Validate the name before any stat or descent into it.
                        if not validator(candidate.name):
                            continue
                        if candidate.is_symlink() or not candidate.is_dir():
                            continue
                        next_candidates.append((candidate, (*canonical, candidate.name)))
                candidates = next_candidates
                if not candidates:
                    break

            for physical, canonical in candidates:
                resolved.append(
                    SaveWatchRoot(
                        layout.layout_id,
                        physical,
                        "/".join(canonical),
                        layout.recursive,
                    )
                )
        return tuple(sorted(resolved, key=lambda item: (item.canonical_root, item.layout_id)))

    def optional_group_ids(self) -> frozenset[str]:
        if self._legacy_rules is None:
            return frozenset()
        return frozenset(
            group.group_id
            for rule in self._legacy_rules.values()
            for group in rule.optional_groups
        )

    def excluded_top_level_dirs(self) -> frozenset[str]:
        return frozenset({FLATPAK_DIRNAME})


DEFAULT_SAVE_SELECTION_POLICY = SaveSelectionPolicy()
