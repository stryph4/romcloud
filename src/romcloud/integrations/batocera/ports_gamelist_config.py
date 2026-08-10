"""Batocera Ports `gamelist.xml` — I/O layer.

Delegates all XML transformation to the pure functions in
:mod:`romcloud.integrations.batocera.ports_gamelist`; this module only owns
reading the real (optional) on-disk file and writing it back atomically,
plus copying ROMCloud's bundled icon into the Ports artwork directory that
`<image>` is resolved against.

Layout verified against RetroGameSets/RGSX (a known-working Batocera Ports
project — see ``ports/RGSX/update_gamelist.py``'s ``RGSX_ENTRY`` and the
file layout documented in its README): artwork lives in an ``images/``
folder *alongside* `gamelist.xml` itself (i.e. directly under
``/userdata/roms/ports``, not inside ROMCloud's own install tree), and the
`<image>` element is a relative path into it (``./images/RGSX.png`` for
RGSX; ``./images/ROMCloud.png`` here). An absolute path into
``/userdata/system/romcloud/...`` — ROMCloud's previous approach — is not
how EmulationStation reliably resolves Ports artwork on real hardware.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from romcloud.infrastructure.logging import get_logger
from romcloud.integrations.batocera.ports_gamelist import (
    ROMCLOUD_GAME_NAME,
    ROMCLOUD_ROM_PATH,
    remove_romcloud_entry,
    upsert_romcloud_entry,
)

log = get_logger("batocera.ports_gamelist")

DEFAULT_GAMELIST_PATH = Path("/userdata/roms/ports/gamelist.xml")

ROMCLOUD_IMAGE_FILENAME = "ROMCloud.png"
ROMCLOUD_IMAGE_RELATIVE_PATH = f"./images/{ROMCLOUD_IMAGE_FILENAME}"
"""`<image>` value written into the gamelist entry — relative to
`gamelist.xml`'s own directory, matching the RGSX-verified convention."""


def sync_icon(*, source_icon: Path, ports_dir: Path, filename: str = ROMCLOUD_IMAGE_FILENAME) -> bool:
    """Copy *source_icon* (ROMCloud's bundled icon) into
    ``<ports_dir>/images/<filename>`` — the location `<image>` in
    `gamelist.xml` is actually resolved against.

    Atomic (write-temp-then-rename) and idempotent: skips the write
    entirely if the destination already holds identical bytes. Every other
    file already present in ``<ports_dir>/images`` is left untouched.
    Returns ``True`` if the file was created/updated, ``False`` if it was
    already up to date.
    """
    data = source_icon.read_bytes()
    dest = ports_dir / "images" / filename
    if dest.exists() and dest.read_bytes() == data:
        return False

    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = dest.with_name(f".{dest.name}.tmp")
    tmp_path.write_bytes(data)
    tmp_path.replace(dest)
    return True


def reconcile(
    *,
    image: str = ROMCLOUD_IMAGE_RELATIVE_PATH,
    gamelist_path: Path = DEFAULT_GAMELIST_PATH,
    rom_path: str = ROMCLOUD_ROM_PATH,
    name: str = ROMCLOUD_GAME_NAME,
) -> bool:
    """Ensure the ROMCloud port entry exists in *gamelist_path*, pointing its
    `<image>` at *image* (a gamelist-relative path string — see
    :data:`ROMCLOUD_IMAGE_RELATIVE_PATH` — never a filesystem `Path`, so a
    leading ``./`` is preserved verbatim rather than normalized away).

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

    result = upsert_romcloud_entry(existing_xml, image=image, rom_path=rom_path, name=name)

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


def remove(
    *,
    ports_dir: Path,
    gamelist_path: Optional[Path] = None,
) -> bool:
    """Remove ROMCloud's exact Ports artifacts without touching shared data."""
    changed = False
    path = gamelist_path or ports_dir / "gamelist.xml"
    if path.exists():
        try:
            existing_xml = path.read_text(encoding="utf-8")
            result = remove_romcloud_entry(existing_xml)
            if result.removed:
                tmp_path = path.with_name(f".{path.name}.tmp")
                tmp_path.write_text(result.xml, encoding="utf-8")
                tmp_path.replace(path)
                changed = True
        except OSError as exc:
            log.warning("Failed to remove ROMCloud entry from %s: %s", path, exc)

    for owned_path in (
        ports_dir / "ROMCloud.sh",
        ports_dir / "images" / ROMCLOUD_IMAGE_FILENAME,
    ):
        if owned_path.exists():
            owned_path.unlink()
            changed = True
    return changed
