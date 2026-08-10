"""EmulationStation `es_systems` override — I/O layer.

Delegates all XML transformation to the pure functions in
:mod:`romcloud.integrations.batocera.es_systems`; this module only owns
reading the real stock file and writing/removing ROMCloud's own override.

Verified command format on Batocera 42
---------------------------------------
``/usr/share/emulationstation/es_systems.cfg`` uses::

    emulatorlauncher %CONTROLLERSCONFIG% -system %SYSTEM% -rom %ROM% \\
        -gameinfoxml %GAMEINFOXML% -systemname %SYSTEMNAME%

``batocera-run`` does not exist on Batocera 42.

Override mechanism
-------------------
ROMCloud **never** modifies ``/usr/share/emulationstation/es_systems.cfg``
(Batocera's stock file). Instead it writes its own, separate override file
using Batocera's persistent override mechanism:

    /userdata/system/configs/emulationstation/es_systems_romcloud.cfg

This file contains **only** the systems actually managed by the ROMCloud
catalog (see the ``managed_systems`` argument below — callers typically pass
``container.game_repo.list_systems()``). Each entry contains only its name,
the stock extension list with ``.romcloud`` appended, and the stock command
with its executable swapped for ``romcloud-run``. Batocera inherits every
other field from its stock definition. Every other Batocera system, and any
other user override file, is left completely untouched. Removing ROMCloud's
integration (:func:`remove`) only ever deletes this one file.

Known limitation — folder-specific settings
----------------------------------------------
``snes.folder["/userdata/roms/snes"].*`` overrides are keyed on the ROM's
containing directory. Cached ROMs live under
``/userdata/romcloud-cache/<system>/...``, which differs from the original
``/userdata/roms/<system>/`` directory used as the folder key — those
overrides will not apply to cached ROMs. System-level (``snes.*``) and
per-game (``snes["Game.sfc"].*``) settings are unaffected and work
correctly, since the original filename is always preserved.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from romcloud.core.exceptions import ProviderError
from romcloud.infrastructure.atomic_file import atomic_write_text
from romcloud.infrastructure.logging import get_logger
from romcloud.integrations.batocera.es_systems import (
    GeneratedOverride,
    generate_override,
    parse_override_systems,
)

log = get_logger("batocera.es_config")

STOCK_ES_SYSTEMS_PATH = Path("/usr/share/emulationstation/es_systems.cfg")
ROMCLOUD_OVERRIDE_PATH = Path(
    "/userdata/system/configs/emulationstation/es_systems_romcloud.cfg"
)
WRAPPER_SCRIPT_PATH = Path("/userdata/system/romcloud/bin/romcloud-run")


class ESConfigError(ProviderError):
    """Reading the stock es_systems.cfg or writing the override failed."""


@dataclass(frozen=True)
class ESIntegrationStatus:
    """Snapshot of ROMCloud's EmulationStation integration state."""

    wrapper_installed: bool
    override_exists: bool
    managed_systems: list[str]
    """Systems the catalog currently manages (may differ from what's on disk
    if the override is stale — see ``up_to_date``)."""

    systems_in_override: list[str]
    """Systems actually present in the on-disk override file, if any."""

    up_to_date: bool
    """True if regenerating right now would produce byte-identical content."""


def _read_stock_xml(stock_path: Path) -> str:
    if not stock_path.exists():
        raise ESConfigError(
            f"Batocera stock es_systems.cfg not found at {stock_path}. "
            "Is this running on a Batocera system?"
        )
    try:
        return stock_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ESConfigError(f"Failed to read {stock_path}: {exc}") from exc


def _generate(
    managed_systems: Iterable[str],
    *,
    stock_path: Path,
    wrapper_path: Path,
) -> GeneratedOverride:
    stock_xml = _read_stock_xml(stock_path)
    return generate_override(stock_xml, managed_systems, str(wrapper_path))


def install(
    managed_systems: Iterable[str],
    *,
    stock_path: Path = STOCK_ES_SYSTEMS_PATH,
    override_path: Path = ROMCLOUD_OVERRIDE_PATH,
    wrapper_path: Path = WRAPPER_SCRIPT_PATH,
) -> GeneratedOverride:
    """Generate and write the ROMCloud override for the first time (or refresh it).

    Idempotent: writing the same *managed_systems* against an unchanged stock
    file always produces the same file content.
    """
    result = _generate(managed_systems, stock_path=stock_path, wrapper_path=wrapper_path)
    override_path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(override_path, result.xml)
    log.info(
        "Wrote ES override for %d system(s) to %s (missing from stock: %s)",
        len(result.included_systems),
        override_path,
        result.missing_systems or "none",
    )
    return result


def refresh(
    managed_systems: Iterable[str],
    *,
    stock_path: Path = STOCK_ES_SYSTEMS_PATH,
    override_path: Path = ROMCLOUD_OVERRIDE_PATH,
    wrapper_path: Path = WRAPPER_SCRIPT_PATH,
) -> GeneratedOverride:
    """Regenerate the override to match the catalog's current managed systems.

    Identical to :func:`install` — kept as a distinct name for CLI clarity
    (``romcloud es refresh`` after ``romcloud refresh``).
    """
    return install(
        managed_systems,
        stock_path=stock_path,
        override_path=override_path,
        wrapper_path=wrapper_path,
    )


def status(
    managed_systems: Iterable[str],
    *,
    stock_path: Path = STOCK_ES_SYSTEMS_PATH,
    override_path: Path = ROMCLOUD_OVERRIDE_PATH,
    wrapper_path: Path = WRAPPER_SCRIPT_PATH,
) -> ESIntegrationStatus:
    """Report the current state of ROMCloud's ES integration without changing anything."""
    managed_list = sorted(set(managed_systems))
    override_exists = override_path.exists()
    systems_in_override: list[str] = []
    up_to_date = False

    if override_exists:
        try:
            current_xml = override_path.read_text(encoding="utf-8")
        except OSError:
            current_xml = ""
        systems_in_override = parse_override_systems(current_xml)

        try:
            fresh = _generate(managed_list, stock_path=stock_path, wrapper_path=wrapper_path)
            up_to_date = fresh.xml == current_xml
        except ESConfigError:
            up_to_date = False

    return ESIntegrationStatus(
        wrapper_installed=wrapper_path.exists(),
        override_exists=override_exists,
        managed_systems=managed_list,
        systems_in_override=systems_in_override,
        up_to_date=up_to_date,
    )


def remove(*, override_path: Path = ROMCLOUD_OVERRIDE_PATH) -> bool:
    """Delete ROMCloud's override file only.

    Never touches the stock ``es_systems.cfg`` or any other override file.
    Returns True if a file was removed, False if there was nothing to do.
    """
    if not override_path.exists():
        return False
    override_path.unlink()
    log.info("Removed ES override: %s", override_path)
    return True
