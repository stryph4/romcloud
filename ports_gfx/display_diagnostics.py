"""Best-effort timing diagnostics for the Batocera display handoff."""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path


DISPLAY_LOG_ENV = "ROMCLOUD_DISPLAY_LOG"
_DISPLAY_ENV_KEYS = (
    "SDL_VIDEODRIVER",
    "DISPLAY",
    "WAYLAND_DISPLAY",
    "XDG_SESSION_TYPE",
    "SDL_VIDEO_WINDOW_POS",
)


def default_display_log(romcloud_bin: str) -> Path:
    """Return the install-local log path beside ROMCloud's other logs."""
    configured = os.environ.get(DISPLAY_LOG_ENV)
    if configured:
        return Path(configured)
    return Path(romcloud_bin).resolve().parent.parent / "logs" / "gui-display.log"


class DisplayDiagnostics:
    """Append short, correlated display events without affecting startup."""

    def __init__(self, romcloud_bin: str) -> None:
        self.path = default_display_log(romcloud_bin)
        self.started = time.monotonic()

    def environment(self) -> dict[str, str]:
        return {key: os.environ.get(key, "") for key in _DISPLAY_ENV_KEYS}

    def record(self, event: str, **fields: object) -> None:
        try:
            now = time.monotonic()
            values: dict[str, object] = {
                "wall_utc": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
                "monotonic": round(now, 6),
                "elapsed": round(now - self.started, 6),
                "pid": os.getpid(),
                "event": event,
                **fields,
            }
            line = " ".join(
                f"{key}={json.dumps(value, ensure_ascii=True, separators=(',', ':'))}"
                for key, value in values.items()
            )
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as stream:
                stream.write(f"{line}\n")
        except Exception:  # noqa: BLE001 - diagnostics must never affect the GUI
            pass
