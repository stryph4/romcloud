"""Cache service — manage the local game cache.

Responsibilities
----------------
- Check/report cache state for a game.
- Initiate transfers (delegating to :class:`~.transfer.TransferService`).
- Pin / unpin games.
- Remove cached games.
- Run eviction according to :class:`~romcloud.core.models.cache.CachePolicy`.

This service is the single authority on what is cached.  It never touches
files it did not create; eviction only removes paths recorded in the database.
"""

from __future__ import annotations

import os
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from romcloud.core.exceptions import (
    CacheError,
    GameNotFoundError,
    GamePinnedError,
    InsufficientSpaceError,
)
from romcloud.core.models.cache import CacheEntry, CachePolicy, CacheStatus
from romcloud.core.models.game import Game
from romcloud.core.services.transfer import TransferService
from romcloud.infrastructure.logging import get_logger
from romcloud.infrastructure.repositories.cache import CacheRepository
from romcloud.infrastructure.repositories.game import GameRepository

log = get_logger("cache")


class CacheService:
    """Manages the local ROM cache."""

    def __init__(
        self,
        cache_repo: CacheRepository,
        game_repo: GameRepository,
        transfer_service: TransferService,
        cache_root: str,
        policy: CachePolicy,
    ) -> None:
        self._cache_repo = cache_repo
        self._game_repo = game_repo
        self._transfer = transfer_service
        self._cache_root = Path(cache_root)
        self._policy = policy
        # In-memory set of game_ids currently being launched.
        # Eviction must not remove these.  Does not persist across restarts.
        self._active_launches: set[str] = set()

    # ── query ─────────────────────────────────────────────────────────────────

    def is_cached(self, game_id: str) -> bool:
        """True if the game has a complete, valid cache entry."""
        entry = self._cache_repo.get(game_id)
        if entry is None:
            return False
        # Also verify the cached path still exists on disk.
        if not Path(entry.cache_path).exists():
            log.warning(
                "Cache entry for %s points to missing path %s — invalidating",
                game_id,
                entry.cache_path,
            )
            self._cache_repo.delete(game_id)
            return False
        return entry.is_complete

    def get_entry(self, game_id: str) -> Optional[CacheEntry]:
        return self._cache_repo.get(game_id)

    def get_launch_path(self, game_id: str) -> Optional[str]:
        """Return the local path of the primary ROM asset for launching.

        Returns None if the game is not completely cached.
        """
        if not self.is_cached(game_id):
            return None
        entry = self._cache_repo.get(game_id)
        assert entry is not None  # guaranteed by is_cached
        game = self._game_repo.get(game_id)
        if game is None:
            return None
        primary = game.primary_asset
        if primary is None:
            return None
        return str(Path(entry.cache_path) / primary.filename)

    def status_summary(self) -> dict:
        """Return a summary dict suitable for CLI display."""
        entries = self._cache_repo.list_all()
        total_bytes = self._cache_repo.total_size()
        free = _free_bytes(str(self._cache_root))
        return {
            "total_entries": len(entries),
            "complete": sum(1 for e in entries if e.is_complete),
            "pinned": sum(1 for e in entries if e.is_pinned),
            "total_bytes": total_bytes,
            "free_bytes": free,
            "max_bytes": self._policy.max_size_bytes,
            "min_free_bytes": self._policy.min_free_bytes,
        }

    # ── mutations ─────────────────────────────────────────────────────────────

    def cache_game(
        self,
        game_id: str,
        on_progress: Optional[Callable[[int, int], None]] = None,
    ) -> str:
        """Ensure *game_id* is cached and return its launch path.

        If a complete entry already exists, returns immediately.
        Otherwise, runs the transfer (which may resume an interrupted one).

        Raises
        ------
        GameNotFoundError
            The game is not in the catalog.
        InsufficientSpaceError
            Not enough space even after eviction.
        TransferError
            The transfer failed.
        """
        if self.is_cached(game_id):
            entry = self._cache_repo.get(game_id)
            assert entry is not None
            self._touch_accessed(game_id)
            return self._launch_path(entry, self._game_repo.get(game_id))

        game = self._game_repo.get(game_id)
        if game is None:
            raise GameNotFoundError(f"Game not found in catalog: {game_id}")

        needed = game.total_size_bytes or 0
        self._ensure_space(needed)

        # Create or update the entry to TRANSFERRING.
        existing = self._cache_repo.get(game_id)
        if existing is None:
            entry = CacheEntry.create(
                game_id=game_id,
                cache_path=str(self._cache_root / game.system / game_id),
            )
            self._cache_repo.save(entry)
        else:
            self._cache_repo.update_status(game_id, CacheStatus.TRANSFERRING)

        try:
            final_path = self._transfer.transfer(game, on_progress)
            actual_size = _dir_size(Path(final_path))
            self._cache_repo.update_status(game_id, CacheStatus.COMPLETE)
            self._cache_repo.update_size(game_id, actual_size)
            self._touch_accessed(game_id)

            # Re-read entry with updated cache_path.
            updated_entry = self._cache_repo.get(game_id)
            assert updated_entry is not None
            return self._launch_path(updated_entry, game)

        except Exception:
            self._cache_repo.update_status(game_id, CacheStatus.FAILED)
            raise

    def remove(self, game_id: str, force: bool = False) -> None:
        """Remove the cached copy of a game.

        Raises :class:`~romcloud.core.exceptions.GamePinnedError` if the
        game is pinned and *force* is False.
        """
        entry = self._cache_repo.get(game_id)
        if entry is None:
            return  # nothing to do

        if entry.is_pinned and not force:
            game = self._game_repo.get(game_id)
            title = game.title if game else game_id
            raise GamePinnedError(
                f"{title!r} is pinned. Use `romcloud cache unpin` first, "
                f"or pass --force."
            )

        self._remove_files(entry)
        self._cache_repo.delete(game_id)
        log.info("Removed cache entry %s", game_id)

    def pin(self, game_id: str) -> None:
        entry = self._cache_repo.get(game_id)
        if entry is None:
            raise CacheError(f"Game {game_id!r} is not cached — nothing to pin.")
        self._cache_repo.set_pinned(game_id, True)
        log.info("Pinned %s", game_id)

    def unpin(self, game_id: str) -> None:
        """Unpin a game.  Does NOT remove the cached file."""
        entry = self._cache_repo.get(game_id)
        if entry is None:
            return
        self._cache_repo.set_pinned(game_id, False)
        log.info("Unpinned %s", game_id)

    def mark_launched(self, game_id: str) -> None:
        """Record that a game is currently launching (protects it from eviction)."""
        self._active_launches.add(game_id)
        self._touch_accessed(game_id)

    def mark_launch_done(self, game_id: str) -> None:
        self._active_launches.discard(game_id)

    # ── eviction ──────────────────────────────────────────────────────────────

    def evict(self, bytes_needed: int = 0) -> list[str]:
        """Free space by evicting LRU-eligible entries.

        Eviction never removes:
        - Pinned games
        - Games currently transferring
        - Games currently launching (tracked in-memory this session)

        Returns a list of evicted game_ids.
        """
        evicted: list[str] = []
        candidates = self._cache_repo.list_evictable_lru()

        for entry in candidates:
            total = self._cache_repo.total_size()
            free = _free_bytes(str(self._cache_root))

            if self._policy.is_within_limits(total, free) and total + bytes_needed <= self._policy.max_size_bytes:
                break

            if entry.game_id in self._active_launches:
                continue

            self._remove_files(entry)
            self._cache_repo.delete(entry.game_id)
            evicted.append(entry.game_id)
            log.info("Evicted %s (LRU)", entry.game_id)

        return evicted

    # ── helpers ───────────────────────────────────────────────────────────────

    def _ensure_space(self, bytes_needed: int) -> None:
        total = self._cache_repo.total_size()
        free = _free_bytes(str(self._cache_root))

        if not self._policy.is_within_limits(total + bytes_needed, free - bytes_needed):
            self.evict(bytes_needed)

        # Re-check after eviction.
        total = self._cache_repo.total_size()
        free = _free_bytes(str(self._cache_root))
        if (
            total + bytes_needed > self._policy.max_size_bytes
            or free - bytes_needed < self._policy.min_free_bytes
        ):
            raise InsufficientSpaceError(
                f"Not enough space to cache game: need {bytes_needed / 1024**3:.1f} GB, "
                f"have {free / 1024**3:.1f} GB free / "
                f"{(self._policy.max_size_bytes - total) / 1024**3:.1f} GB of quota remaining"
            )

    def _touch_accessed(self, game_id: str) -> None:
        self._cache_repo.update_last_accessed(game_id, datetime.now(timezone.utc))

    @staticmethod
    def _launch_path(entry: CacheEntry, game: Optional[Game]) -> str:
        if game is None or game.primary_asset is None:
            return entry.cache_path
        return str(Path(entry.cache_path) / game.primary_asset.filename)

    @staticmethod
    def _remove_files(entry: CacheEntry) -> None:
        p = Path(entry.cache_path)
        if p.exists():
            if p.is_dir():
                shutil.rmtree(p)
            else:
                p.unlink()


def _free_bytes(path: str) -> int:
    try:
        stat = os.statvfs(path)
        return stat.f_bavail * stat.f_frsize
    except OSError:
        return 0


def _dir_size(path: Path) -> int:
    if path.is_file():
        return path.stat().st_size
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file())
