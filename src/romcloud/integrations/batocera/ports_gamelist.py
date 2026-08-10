"""Batocera Ports `gamelist.xml` — ROMCloud entry generation (pure logic).

No I/O in this module: functions take the existing `gamelist.xml` XML (or
``None`` if the file doesn't exist yet) as a string and return the updated
document as a string. This keeps the transformation logic fully
unit-testable without a Batocera filesystem.

See :mod:`romcloud.integrations.batocera.ports_gamelist_config` for the I/O
layer that reads/writes the real file at
``/userdata/roms/ports/gamelist.xml``.

Format
------
EmulationStation's per-system `gamelist.xml` lives alongside the ROMs it
describes and looks like::

    <gameList>
      <game>
        <path>./ROMCloud.sh</path>
        <name>ROMCloud</name>
        <image>./images/ROMCloud.png</image>
      </game>
      ...
    </gameList>

The `<image>` path is relative to `gamelist.xml`'s own directory (i.e. the
Ports ROM folder itself, `/userdata/roms/ports`), pointing at a copy of the
artwork placed under that folder's own `images/` subdirectory — never an
absolute path into ROMCloud's install tree. This is the layout every
observed working Batocera Ports project uses (verified against
RetroGameSets/RGSX's `ports/RGSX/update_gamelist.py`, whose `RGSX_ENTRY`
uses `"image": "./images/RGSX.png"` alongside `ports/images/RGSX.png` on
disk — see `romcloud.integrations.batocera.ports_gamelist_config` for
where ROMCloud's own icon gets copied there).

Matching / update semantics
----------------------------
The ROMCloud entry is identified by the basename of its `<path>` (default
``ROMCloud.sh``), so it's found regardless of whether it was previously
written with a relative (``./ROMCloud.sh``) or absolute
(``/userdata/roms/ports/ROMCloud.sh``) path. When a match is found, only
its ``<name>``/`<image>`` are written — every other child element (e.g. a
user-set ``<favorite>``/``<lastplayed>``/``<desc>``) and every other
``<game>`` entry in the document are left completely untouched.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Optional
from xml.etree import ElementTree as ET

ROMCLOUD_ROM_PATH = "./ROMCloud.sh"
ROMCLOUD_GAME_NAME = "ROMCloud"


@dataclass(frozen=True)
class GeneratedGamelist:
    """Result of upserting the ROMCloud entry into a Ports `gamelist.xml`."""

    xml: str
    created: bool
    """True if a new `<game>` entry was appended; False if an existing one was updated in place."""


@dataclass(frozen=True)
class RemovedGamelistEntry:
    """Result of removing ROMCloud's entry from a Ports `gamelist.xml`."""

    xml: str
    removed: bool


def _parse_root(existing_xml: Optional[str]) -> ET.Element:
    if existing_xml:
        try:
            return ET.fromstring(existing_xml)
        except ET.ParseError:
            pass
    return ET.Element("gameList")


def _is_romcloud_entry(game_el: ET.Element, rom_path: str) -> bool:
    path_el = game_el.find("path")
    if path_el is None or not (path_el.text or "").strip():
        return False
    return PurePosixPath(path_el.text.strip()).name.lower() == PurePosixPath(rom_path).name.lower()


def upsert_romcloud_entry(
    existing_xml: Optional[str],
    *,
    image: str,
    rom_path: str = ROMCLOUD_ROM_PATH,
    name: str = ROMCLOUD_GAME_NAME,
) -> GeneratedGamelist:
    """Insert or update the ROMCloud `<game>` entry in a Ports `gamelist.xml`.

    Deterministic and idempotent: reapplying with the same *image*/*rom_path*/
    *name* against the previous result produces byte-identical output.
    """
    root = _parse_root(existing_xml)

    game_el = None
    for candidate in root.findall("game"):
        if _is_romcloud_entry(candidate, rom_path):
            game_el = candidate
            break

    created = game_el is None
    if game_el is None:
        game_el = ET.SubElement(root, "game")
        ET.SubElement(game_el, "path").text = rom_path
        ET.SubElement(game_el, "name").text = name
        ET.SubElement(game_el, "image").text = image
    else:
        name_el = game_el.find("name")
        if name_el is None:
            name_el = ET.SubElement(game_el, "name")
        name_el.text = name

        image_el = game_el.find("image")
        if image_el is None:
            image_el = ET.SubElement(game_el, "image")
        image_el.text = image

    ET.indent(root, space="  ")
    xml_body = ET.tostring(root, encoding="unicode")
    xml_text = '<?xml version="1.0"?>\n' + xml_body + "\n"
    return GeneratedGamelist(xml=xml_text, created=created)


def remove_romcloud_entry(
    existing_xml: str,
    *,
    rom_path: str = ROMCLOUD_ROM_PATH,
) -> RemovedGamelistEntry:
    """Remove only ROMCloud's `<game>` entry from an existing gamelist.

    Invalid XML is returned unchanged. Every unrelated entry and the
    shared `gamelist.xml` file itself remain owned by Batocera/the user.
    """
    try:
        root = ET.fromstring(existing_xml)
    except ET.ParseError:
        return RemovedGamelistEntry(xml=existing_xml, removed=False)

    removed = False
    for candidate in list(root.findall("game")):
        if _is_romcloud_entry(candidate, rom_path):
            root.remove(candidate)
            removed = True

    if not removed:
        return RemovedGamelistEntry(xml=existing_xml, removed=False)

    ET.indent(root, space="  ")
    xml_body = ET.tostring(root, encoding="unicode")
    return RemovedGamelistEntry(
        xml='<?xml version="1.0"?>\n' + xml_body + "\n",
        removed=True,
    )
