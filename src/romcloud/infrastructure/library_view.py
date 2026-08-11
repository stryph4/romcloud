"""Persisted Smart Cache library-presentation state."""

from __future__ import annotations

import json
from pathlib import Path

from romcloud.infrastructure.atomic_file import atomic_write_text
from romcloud.infrastructure.config import AppConfig

STATE_FILENAME = "library-view.json"
STATE_VERSION = 1


def state_path(config: AppConfig) -> Path:
    return Path(config.data_path) / STATE_FILENAME


def offline_library_enabled(config: AppConfig) -> bool:
    """Return persisted cached-only presentation state; malformed state is off."""
    path = state_path(config)
    if not path.is_file() or path.is_symlink():
        return False
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return payload == {"version": STATE_VERSION, "offline_library": True}


def write_offline_library_state(config: AppConfig, enabled: bool) -> None:
    """Persist enabled state atomically; online is represented by no state file."""
    path = state_path(config)
    if not enabled:
        path.unlink(missing_ok=True)
        return
    atomic_write_text(
        path,
        json.dumps(
            {"version": STATE_VERSION, "offline_library": True}, indent=2
        )
        + "\n",
    )
