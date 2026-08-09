"""
Known Batocera system folder names.

ROMCloud matches these names when scanning a remote ROM root.
Folders that do not appear in this set are ignored rather than
auto-inferred, keeping the behavior predictable and safe.

This list should grow conservatively. Do not add systems you have
not confirmed exist as folder names in a real Batocera install.
"""

BATOCERA_SYSTEMS: frozenset[str] = frozenset(
    {
        # ── Nintendo ──────────────────────────────────────────────────────────
        "nes",
        "snes",
        "n64",
        "gamecube",
        "wii",
        "wiiu",
        "switch",
        "gb",
        "gbc",
        "gba",
        "nds",
        "3ds",
        "virtualboy",
        "pokemini",
        # ── Sony ──────────────────────────────────────────────────────────────
        "psx",
        "ps2",
        "ps3",
        "ps4",
        "psp",
        "psvita",
        # ── Sega ──────────────────────────────────────────────────────────────
        "mastersystem",
        "megadrive",
        "genesis",
        "saturn",
        "dreamcast",
        "gamegear",
        "sg1000",
        "segacd",
        "sega32x",
        "naomi",
        "naomi2",
        # ── Microsoft ─────────────────────────────────────────────────────────
        "xbox",
        "xbox360",
        # ── SNK ───────────────────────────────────────────────────────────────
        "neogeo",
        "neogeocd",
        "ngp",
        "ngpc",
        # ── Atari ─────────────────────────────────────────────────────────────
        "atari2600",
        "atari7800",
        "atarist",
        "atarilynx",
        "jaguar",
        # ── NEC ───────────────────────────────────────────────────────────────
        "pcengine",
        "pcenginecd",
        "supergrafx",
        "pc88",
        "pc98",
        # ── 3DO / Panasonic ───────────────────────────────────────────────────
        "3do",
        # ── Arcade ────────────────────────────────────────────────────────────
        "arcade",
        "mame",
        "fbneo",
        "naomigd",
        # ── PC / DOS ──────────────────────────────────────────────────────────
        "dos",
        "pc",
        "scummvm",
        "windows",
        "windows3x",
        # ── Home computers ────────────────────────────────────────────────────
        "amiga",
        "amigacd32",
        "amstradcpc",
        "c64",
        "c128",
        "msx",
        "msx2",
        "zxspectrum",
        "ti99",
        "apple2",
        "apple2gs",
        "vic20",
        "plus4",
        "pet",
        "bbc",
        # ── Handhelds ─────────────────────────────────────────────────────────
        "wonderswan",
        "wonderswancolor",
        "gw",
        "supervision",
        "odyssey2",
        "vectrex",
        "channelf",
        "colecovision",
        "intellivision",
        "astrocade",
        "msx1",
        # ── Ports / misc ──────────────────────────────────────────────────────
        "ports",
        "love",
        "lutro",
        "pico8",
        "tic80",
    }
)
