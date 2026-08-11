"""Authoritative persisted ROMCloud operating mode."""

from __future__ import annotations

import json
from pathlib import Path

from romcloud.infrastructure.atomic_file import atomic_write_text
from romcloud.infrastructure.config import AppConfig
from romcloud.core.capabilities import OperatingMode

STATE_FILENAME = "library-view.json"
STATE_VERSION = 2


def state_path(config: AppConfig) -> Path:
    return Path(config.data_path) / STATE_FILENAME


def operating_mode(config: AppConfig) -> OperatingMode:
    """Return and, when necessary, initialize the one persisted mode.

    Version 1 stored only the exceptional offline boolean.  Reading it once
    migrates that intent to the explicit two-state schema.  Missing or
    malformed legacy state becomes an explicit NAS state for compatibility
    with installations that previously represented online by absence.
    """
    path = state_path(config)
    payload = None
    if path.is_file() and not path.is_symlink():
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            payload = None
    if isinstance(payload, dict) and payload.get("version") == STATE_VERSION:
        try:
            return OperatingMode(payload.get("mode"))
        except (TypeError, ValueError):
            pass
    mode = (
        OperatingMode.OFFLINE
        if payload == {"version": 1, "offline_library": True}
        else OperatingMode.NAS
    )
    write_operating_mode(config, mode)
    return mode


def write_operating_mode(config: AppConfig, mode: OperatingMode | str) -> None:
    """Atomically persist exactly one of the two valid operating modes."""
    selected = OperatingMode(mode)
    atomic_write_text(
        state_path(config),
        json.dumps({"version": STATE_VERSION, "mode": selected.value}, indent=2)
        + "\n",
    )


def offline_library_enabled(config: AppConfig) -> bool:
    """Compatibility adapter for cached-only presentation consumers."""
    return operating_mode(config) is OperatingMode.OFFLINE


def write_offline_library_state(config: AppConfig, enabled: bool) -> None:
    """Compatibility adapter; false is explicit NAS, never file absence."""
    write_operating_mode(
        config, OperatingMode.OFFLINE if enabled else OperatingMode.NAS
    )
