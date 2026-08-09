"""Per-controller custom mapping persistence.

Storage location is derived purely from the already-known ``romcloud_bin``
path (the same value ``ports_gfx`` receives everywhere else — see
``client.py``) — never a ``romcloud`` import. State is kept in a sibling
``ports-gfx-state`` directory next to the installer-managed ``ports-gfx``
tree, specifically so that a reinstall/update (which wipes and recopies
``ports-gfx/ports_gfx`` — see ``romcloud.infrastructure.installer``) never
destroys a user's custom controller mapping.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional


def state_dir(romcloud_bin: str) -> Path:
    """``<ROMCLOUD_HOME>/ports-gfx-state`` — *not* under ``ports-gfx/``,
    which is deleted and recreated wholesale on every install/update."""
    romcloud_home = Path(romcloud_bin).resolve().parent.parent
    return romcloud_home / "ports-gfx-state"


def mappings_path(romcloud_bin: str) -> Path:
    return state_dir(romcloud_bin) / "controller_mappings.json"


def load_all_mappings(romcloud_bin: str) -> dict[str, dict]:
    """Never raises — a missing/corrupt file just means "no custom
    mappings yet", not a crash."""
    path = mappings_path(romcloud_bin)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def load_mapping(romcloud_bin: str, controller_key: str) -> Optional[dict]:
    return load_all_mappings(romcloud_bin).get(controller_key)


def save_mapping(romcloud_bin: str, controller_key: str, mapping: dict) -> None:
    """Atomic write (write-temp-then-rename), preserving every other
    controller's stored mapping."""
    path = mappings_path(romcloud_bin)
    all_mappings = load_all_mappings(romcloud_bin)
    all_mappings[controller_key] = mapping

    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f".{path.name}.tmp")
    tmp_path.write_text(json.dumps(all_mappings, indent=2) + "\n", encoding="utf-8")
    tmp_path.replace(path)


def make_loader(romcloud_bin: str):
    """A ``ConfigLoader`` closure for ``controller.ControllerManager``."""

    def _loader(controller_key: str) -> Optional[dict]:
        return load_mapping(romcloud_bin, controller_key)

    return _loader


def make_saver(romcloud_bin: str):
    """A ``ConfigSaver`` closure for ``controller.ControllerManager``."""

    def _saver(controller_key: str, mapping: dict) -> None:
        save_mapping(romcloud_bin, controller_key, mapping)

    return _saver
