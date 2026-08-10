"""Structured, bounded activity history for graphical operations."""

from __future__ import annotations

import json
from collections import deque
from dataclasses import dataclass, field
from typing import Any


PROGRESS_PREFIX = "@romcloud-progress "


@dataclass(frozen=True)
class ActivityEvent:
    timestamp: str
    operation: str
    stage: str
    status: str
    message: str
    detail: str = ""
    current: int | None = None
    total: int | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def display_line(self) -> str:
        marker = "✓" if self.status == "success" else "!" if self.status == "error" else "•"
        return f"[{self.timestamp}] {marker} {self.message}"


def parse_progress_line(line: str) -> ActivityEvent | None:
    if not line.startswith(PROGRESS_PREFIX):
        return None
    try:
        payload = json.loads(line[len(PROGRESS_PREFIX) :])
    except (TypeError, ValueError):
        return None
    if not isinstance(payload, dict) or not payload.get("message"):
        return None
    return ActivityEvent(
        timestamp=str(payload.get("timestamp", "")),
        operation=str(payload.get("operation", "")),
        stage=str(payload.get("stage", "")),
        status=str(payload.get("status", "info")),
        message=str(payload["message"]),
        detail=str(payload.get("detail", "")),
        current=_optional_int(payload.get("current")),
        total=_optional_int(payload.get("total")),
        metadata=dict(payload.get("metadata") or {}),
    )


def _optional_int(value: object) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


class ActivityLog:
    def __init__(self, max_events: int = 100) -> None:
        self._events: deque[ActivityEvent] = deque(maxlen=max_events)
        self.scroll_offset = 0
        self.auto_scroll = True
        self.details_expanded = False

    @property
    def events(self) -> list[ActivityEvent]:
        return list(self._events)

    def ingest(self, line: str) -> ActivityEvent | None:
        event = parse_progress_line(line)
        if event is not None:
            self._events.append(event)
            if self.auto_scroll:
                self.scroll_offset = 0
        return event

    def append(self, event: ActivityEvent) -> None:
        self._events.append(event)
        if self.auto_scroll:
            self.scroll_offset = 0

    def scroll(self, delta: int, viewport_rows: int) -> None:
        maximum = max(0, len(self._events) - max(1, viewport_rows))
        self.scroll_offset = max(0, min(self.scroll_offset + delta, maximum))
        self.auto_scroll = self.scroll_offset == 0

    def visible_events(self, viewport_rows: int) -> list[ActivityEvent]:
        events = self.events
        if viewport_rows <= 0:
            return []
        maximum = max(0, len(events) - viewport_rows)
        offset = max(0, min(self.scroll_offset, maximum))
        end = len(events) - offset
        return events[max(0, end - viewport_rows) : end]

    def user_lines(self, limit: int = 5) -> list[str]:
        return [event.display_line for event in self.events[-limit:]]

    def detail_lines(self, limit: int = 5) -> list[str]:
        return [
            f"{event.stage}: {event.detail}"
            for event in self.events
            if event.detail
        ][-limit:]
