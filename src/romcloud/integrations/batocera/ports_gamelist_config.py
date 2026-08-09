"""Batocera Ports `gamelist.xml` — I/O layer.

Delegates all XML transformation to the pure functions in
:mod:`romcloud.integrations.batocera.ports_gamelist`; this module only owns
reading the real (optional) on-disk file and writing it back atomically.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from romcloud.infrastructure.logging import get_logger
from romcloud.integrations.batocera.ports_gamelist import (
    ROMCLOUD_GAME_NAME,
    ROMCLOUD_ROM_PATH,
    upsert_romcloud_entry,
)

log = get_logger("batocera.ports_gamelist")

DEFAULT_GAMELIST_PATH = Path("/userdata/roms/ports/gamelist.xml")


def reconcile(
    *,
    image_path: Path,
    gamelist_path: Path = DEFAULT_GAMELIST_PATH,
    rom_path: str = ROMCLOUD_ROM_PATH,
    name: str = ROMCLOUD_GAME_NAME,
) -> bool:
    """Ensure the ROMCloud port entry exists in *gamelist_path*, pointing its
    `<image>` at *image_path*.

    Idempotent: only writes the file when its content actually changes.
    Every other `<game>` entry — and every other field on the ROMCloud
    entry itself — is preserved untouched. Returns ``True`` if the file was
    created or updated, ``False`` if it was already up to date.
    """
    existing_xml: Optional[str] = None
    if gamelist_path.exists():
        try:
            existing_xml = gamelist_path.read_text(encoding="utf-8")
        except OSError as exc:
            log.warning("Failed to read %s: %s", gamelist_path, exc)
            existing_xml = None

    result = upsert_romcloud_entry(existing_xml, image=str(image_path), rom_path=rom_path, name=name)

    if existing_xml == result.xml:
        return False

    gamelist_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = gamelist_path.with_name(f".{gamelist_path.name}.tmp")
    tmp_path.write_text(result.xml, encoding="utf-8")
    tmp_path.replace(gamelist_path)
    log.info(
        "%s ROMCloud port entry in %s",
        "Created" if result.created else "Updated",
        gamelist_path,
    )
    return True
