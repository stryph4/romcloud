"""Presentation-neutral progress events for long-running ROMCloud work."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any, Callable, Mapping, Optional


PROGRESS_PREFIX = "@romcloud-progress "


@dataclass(frozen=True)
class ProgressEvent:
    operation: str
    stage: str
    status: str
    message: str
    detail: str = ""
    timestamp: str = ""
    current: int | None = None
    total: int | None = None
    metadata: Mapping[str, Any] | None = None

    def redacted(self, *secrets: str) -> "ProgressEvent":
        message = redact_text(self.message, *secrets)
        detail = redact_text(self.detail, *secrets)
        return ProgressEvent(
            operation=self.operation,
            stage=self.stage,
            status=self.status,
            message=message,
            detail=detail,
            timestamp=self.timestamp or datetime.now().strftime("%H:%M:%S"),
            current=self.current,
            total=self.total,
            metadata=self.metadata,
        )

    def wire_line(self, *secrets: str) -> str:
        return PROGRESS_PREFIX + json.dumps(
            asdict(self.redacted(*secrets)), separators=(",", ":")
        )


ProgressSink = Optional[Callable[[ProgressEvent], None]]


def emit_progress(
    sink: ProgressSink,
    operation: str,
    stage: str,
    status: str,
    message: str,
    *,
    detail: str = "",
    current: int | None = None,
    total: int | None = None,
    metadata: Mapping[str, Any] | None = None,
) -> None:
    if sink is not None:
        sink(
            ProgressEvent(
                operation,
                stage,
                status,
                message,
                detail,
                current=current,
                total=total,
                metadata=metadata,
            )
        )


def redact_text(text: str, *secrets: str) -> str:
    safe = str(text)
    for secret in secrets:
        if secret:
            safe = safe.replace(secret, "***")
    return safe
