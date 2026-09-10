"""Tiny stdlib-only bridge from the system-Python GUI to diagnostics.db.

``ports_gfx`` cannot import the venv-owned ``romcloud`` package. This writer
therefore implements only the shared schema's insert boundary and fails open.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

_SECRET = re.compile(
    r"(?i)(password|secret|token|api[_-]?key|authorization)(\s*[:=]\s*)\S+"
)
_SAFE_KEYS = frozenset(
    {
        "event", "elapsed", "monotonic", "width", "height", "pygame_version",
        "driver", "display", "environment", "status", "reason", "count",
        "joystick_count", "controller_module_present", "controllers", "detail",
    }
)


class GuiDiagnosticWriter:
    def __init__(self, path: Path, *, operation_id: str) -> None:
        self.path = Path(path)
        self.operation_id = operation_id

    def write(self, event_code: str, message: str, fields: dict[str, object]) -> None:
        try:
            safe = {
                key: _clean(value) if key in _SAFE_KEYS else "[REDACTED:UNAPPROVED_FIELD]"
                for key, value in fields.items()
            }
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with sqlite3.connect(str(self.path), timeout=0.25) as conn:
                conn.execute("PRAGMA busy_timeout = 250")
                conn.execute("PRAGMA journal_mode = WAL")
                conn.execute(
                    """CREATE TABLE IF NOT EXISTS diagnostic_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp_utc TEXT NOT NULL,
                    monotonic_ns INTEGER NOT NULL, level TEXT NOT NULL,
                    subsystem TEXT NOT NULL, event_code TEXT NOT NULL,
                    message TEXT NOT NULL, operation_id TEXT, parent_operation_id TEXT,
                    process_id INTEGER NOT NULL, thread_id INTEGER NOT NULL,
                    thread_name TEXT NOT NULL, process_name TEXT NOT NULL,
                    romcloud_version TEXT NOT NULL, build TEXT, metadata_json TEXT NOT NULL,
                    exception_type TEXT, exception_message TEXT)"""
                )
                conn.execute(
                    """INSERT INTO diagnostic_events (
                    timestamp_utc, monotonic_ns, level, subsystem, event_code, message,
                    operation_id, parent_operation_id, process_id, thread_id, thread_name,
                    process_name, romcloud_version, build, metadata_json,
                    exception_type, exception_message)
                    VALUES (?, ?, 'INFO', 'gui', ?, ?, ?, NULL, ?, ?, ?, 'romcloud-ports',
                    'system-python-gui', NULL, ?, NULL, NULL)""",
                    (
                        datetime.now(timezone.utc).isoformat(timespec="microseconds"),
                        time.monotonic_ns(), event_code[:128], _clean(message)[:8192],
                        self.operation_id, os.getpid(), threading.get_ident(),
                        threading.current_thread().name[:128],
                        json.dumps(safe, sort_keys=True, separators=(",", ":"))[:32768],
                    ),
                )
        except Exception:
            pass


def _clean(value: object) -> object:
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, dict):
        return {str(key): _clean(item) for key, item in list(value.items())[:50]}
    if isinstance(value, (list, tuple)):
        return [_clean(item) for item in value[:50]]
    text = str(value)
    if "PRIVATE KEY-----" in text.upper():
        return "[REDACTED]"
    return _SECRET.sub(r"\1\2[REDACTED]", text)[:2048]
