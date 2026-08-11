"""Authoritative persisted ROMCloud operating mode."""

from __future__ import annotations

import json
from pathlib import Path

from romcloud.infrastructure.atomic_file import atomic_write_text
from romcloud.infrastructure.config import AppConfig
from romcloud.core.capabilities import OperatingMode

STATE_FILENAME = "library-view.json"
STATE_VERSION = 3


def state_path(config: AppConfig) -> Path:
    return Path(config.data_path) / STATE_FILENAME


def operating_mode(config: AppConfig) -> OperatingMode:
    """Return and, when necessary, initialize the one persisted mode.

    Version 1 stored only the exceptional offline boolean. Version 2 stored
    ``nas`` versus ``offline`` while the configured access strategy still
    selected direct links versus cache proxies. Reading either once
    preserves that intent in the authoritative three-state schema.
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
    if payload == {"version": 1, "offline_library": True} or (
        isinstance(payload, dict)
        and payload.get("version") == 2
        and payload.get("mode") == "offline"
    ):
        mode = OperatingMode.OFFLINE
    elif isinstance(payload, dict) and payload.get("mode") == "nas":
        mode = OperatingMode.CONNECTED
    elif getattr(config, "game_access_mode", "smart_cache") == "direct_nas":
        mode = OperatingMode.CONNECTED
    else:
        mode = OperatingMode.CACHE
    write_operating_mode(config, mode)
    return mode


def write_operating_mode(config: AppConfig, mode: OperatingMode | str) -> None:
    """Atomically persist exactly one authoritative operating mode."""
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
    """Compatibility adapter; false restores Cache Mode."""
    write_operating_mode(
        config, OperatingMode.OFFLINE if enabled else OperatingMode.CACHE
    )
