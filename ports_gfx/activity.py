"""Structured, bounded activity history for graphical operations."""

from __future__ import annotations

import json
from collections import deque
from dataclasses import dataclass


PROGRESS_PREFIX = "@romcloud-progress "


@dataclass(frozen=True)
class ActivityEvent:
    timestamp: str
    operation: str
    stage: str
    status: str
    message: str
    detail: str = ""

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
    )


class ActivityLog:
    def __init__(self, max_events: int = 100) -> None:
        self._events: deque[ActivityEvent] = deque(maxlen=max_events)

    @property
    def events(self) -> list[ActivityEvent]:
        return list(self._events)

    def ingest(self, line: str) -> ActivityEvent | None:
        event = parse_progress_line(line)
        if event is not None:
            self._events.append(event)
        return event

    def user_lines(self, limit: int = 5) -> list[str]:
        return [event.display_line for event in self.events[-limit:]]

    def detail_lines(self, limit: int = 5) -> list[str]:
        return [
            f"{event.stage}: {event.detail}"
            for event in self.events
            if event.detail
        ][-limit:]
