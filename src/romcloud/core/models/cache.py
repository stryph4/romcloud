"""Cache domain model."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum


class CacheStatus(str, Enum):
    """Lifecycle state of a cache entry."""

    TRANSFERRING = "transferring"
    """Transfer is in progress (or was interrupted)."""

    COMPLETE = "complete"
    """All assets are present and validated."""

    FAILED = "failed"
    """Transfer failed; partial data may exist."""


@dataclass
class CacheEntry:
    """Represents a game's presence in the local cache.

    ``cache_path`` is the *container directory* for the game inside the
    cache root, e.g. ``/userdata/romcloud-cache/ps2/<game_id>/``.
    Individual asset files live inside that directory.
    """

    game_id: str
    cache_path: str
    status: CacheStatus
    cached_at: datetime
    last_accessed: datetime
    size_bytes: int
    is_pinned: bool = False

    # ── derived state ─────────────────────────────────────────────────────────

    @property
    def is_complete(self) -> bool:
        return self.status == CacheStatus.COMPLETE

    @property
    def is_evictable(self) -> bool:
        """True when the entry may be removed by automatic eviction policy."""
        return self.is_complete and not self.is_pinned

    # ── factory ───────────────────────────────────────────────────────────────

    @classmethod
    def create(cls, game_id: str, cache_path: str) -> CacheEntry:
        now = datetime.now(timezone.utc)
        return cls(
            game_id=game_id,
            cache_path=cache_path,
            status=CacheStatus.TRANSFERRING,
            cached_at=now,
            last_accessed=now,
            size_bytes=0,
            is_pinned=False,
        )


@dataclass(frozen=True)
class CachePolicy:
    """Governs how much the cache may grow and when eviction triggers.

    Eviction runs when *either* condition is violated:
    - ``total_cache_size > max_size_bytes``
    - ``free_disk_space < min_free_bytes``
    """

    max_size_bytes: int
    min_free_bytes: int

    @classmethod
    def from_gb(cls, max_size_gb: float, min_free_gb: float) -> CachePolicy:
        return cls(
            max_size_bytes=int(max_size_gb * 1024**3),
            min_free_bytes=int(min_free_gb * 1024**3),
        )

    def is_within_limits(self, total_cache_bytes: int, free_disk_bytes: int) -> bool:
        return (
            total_cache_bytes <= self.max_size_bytes
            and free_disk_bytes >= self.min_free_bytes
        )
