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

from dataclasses import dataclass
from pathlib import Path
from typing import Optional
from xml.etree import ElementTree as ET

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
        # An unreadable existing shared file must never be treated as absent;
        # doing so would replace third-party entries with a new empty document.
        existing_xml = gamelist_path.read_text(encoding="utf-8")

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


@dataclass(frozen=True)
class PortsOwnership:
    launcher_owned: bool = False
    icon_owned: bool = False
    entry_owned: bool = False
    warnings: tuple[str, ...] = ()


def inspect_ownership(
    *,
    ports_dir: Path,
    expected_wrapper: Path,
    expected_icon: Path | None = None,
    gamelist_path: Optional[Path] = None,
) -> PortsOwnership:
    """Prove ownership without trusting the reserved filenames alone."""
    warnings: list[str] = []
    launcher = ports_dir / "ROMCloud.sh"
    launcher_owned = False
    if launcher.exists() or launcher.is_symlink():
        try:
            content = launcher.read_text(encoding="utf-8")
            expected_exec = f'exec "{expected_wrapper}" "$@"'
            expected_log = str(expected_wrapper.parent.parent / "logs" / "gui-display.log")
            launcher_owned = (
                launcher.is_file()
                and not launcher.is_symlink()
                and content.startswith("#!/bin/bash\n")
                and content.rstrip().endswith(expected_exec)
                and content.count('event="port_entry_start"') == 1
                and f'ROMCLOUD_DISPLAY_LOG="{expected_log}"' in content
            )
        except (OSError, UnicodeError):
            launcher_owned = False
        if not launcher_owned:
            warnings.append(f"Preserved foreign or unreadable Ports launcher: {launcher}")

    icon = ports_dir / "images" / ROMCLOUD_IMAGE_FILENAME
    icon_owned = False
    if icon.exists() or icon.is_symlink():
        try:
            icon_owned = bool(
                expected_icon is not None
                and expected_icon.is_file()
                and not icon.is_symlink()
                and icon.is_file()
                and icon.read_bytes() == expected_icon.read_bytes()
            )
        except OSError:
            icon_owned = False
        if not icon_owned:
            warnings.append(f"Preserved foreign or unverifiable Ports icon: {icon}")

    entry_owned = False
    path = gamelist_path or ports_dir / "gamelist.xml"
    if path.exists() or path.is_symlink():
        if path.is_symlink() or not path.is_file():
            warnings.append(f"Preserved unsafe shared Ports gamelist: {path}")
        else:
            try:
                root = ET.fromstring(path.read_text(encoding="utf-8"))
            except (OSError, ET.ParseError, UnicodeError):
                warnings.append(f"Preserved malformed or unreadable shared Ports gamelist: {path}")
            else:
                matches = []
                for element in root.findall("game"):
                    if (element.findtext("path") or "").strip() == ROMCLOUD_ROM_PATH:
                        matches.append(element)
                if len(matches) == 1:
                    element = matches[0]
                    fields = [(child.tag, (child.text or "").strip()) for child in element]
                    entry_owned = sorted(fields) == sorted(
                        [
                            ("path", ROMCLOUD_ROM_PATH),
                            ("name", ROMCLOUD_GAME_NAME),
                            ("image", ROMCLOUD_IMAGE_RELATIVE_PATH),
                        ]
                    )
                if matches and not entry_owned:
                    warnings.append(f"Preserved unverified ROMCloud Ports gamelist entry: {path}")
    return PortsOwnership(launcher_owned, icon_owned, entry_owned, tuple(warnings))


def remove(
    *,
    ports_dir: Path,
    gamelist_path: Optional[Path] = None,
    expected_wrapper: Path | None = None,
    expected_icon: Path | None = None,
) -> bool:
    """Remove only positively verified ROMCloud Ports artifacts."""
    if expected_wrapper is None:
        log.warning("Ports cleanup skipped: no expected ROMCloud wrapper was supplied")
        return False
    ownership = inspect_ownership(
        ports_dir=ports_dir,
        expected_wrapper=expected_wrapper,
        expected_icon=expected_icon,
        gamelist_path=gamelist_path,
    )
    for warning in ownership.warnings:
        log.warning("%s", warning)
    changed = False
    path = gamelist_path or ports_dir / "gamelist.xml"
    if ownership.launcher_owned and ownership.entry_owned:
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
            raise RuntimeError(f"Owned Ports gamelist removal failed: {path}: {exc}") from exc

    launcher = ports_dir / "ROMCloud.sh"
    if ownership.launcher_owned:
        launcher.unlink()
        changed = True
    icon = ports_dir / "images" / ROMCLOUD_IMAGE_FILENAME
    if ownership.icon_owned:
        icon.unlink()
        changed = True
    return changed
