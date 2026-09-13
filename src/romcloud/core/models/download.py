"""Durable Download Manager workflow models."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Optional


class DownloadOrigin(str, Enum):
    MANUAL = "manual"
    PINNED = "pinned"
    SELECTED = "selected"


class DownloadState(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    PAUSED = "paused"
    INTERRUPTED = "interrupted"
    VERIFYING = "verifying"
    FAILED = "failed"
    CANCELLED = "cancelled"
    COMPLETE = "complete"


ACTIVE_DOWNLOAD_STATES = frozenset(
    {
        DownloadState.QUEUED,
        DownloadState.RUNNING,
        DownloadState.PAUSED,
        DownloadState.INTERRUPTED,
        DownloadState.VERIFYING,
    }
)


@dataclass(frozen=True)
class DownloadItem:
    id: str
    batch_id: Optional[str]
    game_id: Optional[str]
    game_title: str
    system: str
    origin: DownloadOrigin
    state: DownloadState
    queue_seq: int
    worker_instance_id: Optional[str]
    bytes_total: int
    bytes_present: int
    error_code: Optional[str]
    error_message: Optional[str]
    created_at: datetime
    updated_at: datetime
    started_at: Optional[datetime]
    finished_at: Optional[datetime]

    def as_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "batch_id": self.batch_id,
            "game_id": self.game_id,
            "game_title": self.game_title,
            "system": self.system,
            "origin": self.origin.value,
            "state": self.state.value,
            "queue_seq": self.queue_seq,
            "worker_instance_id": self.worker_instance_id,
            "bytes_total": self.bytes_total,
            "bytes_present": self.bytes_present,
            "error_code": self.error_code,
            "error_message": self.error_message,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
        }
