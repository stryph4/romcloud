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

from romcloud.core.cache_paths import resolve_cache_path
from romcloud.core.exceptions import (
    CacheError,
    GameNotFoundError,
    GamePinnedError,
    InsufficientSpaceError,
)
from romcloud.core.capabilities import Capability, CapabilityPolicy
from romcloud.core.models.cache import CacheEntry, CachePolicy, CacheStatus
from romcloud.core.models.game import Game, GameAsset
from romcloud.services.transfer import TransferService
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
        capability_policy: Optional[CapabilityPolicy] = None,
    ) -> None:
        self._cache_repo = cache_repo
        self._game_repo = game_repo
        self._transfer = transfer_service
        self._cache_root = Path(cache_root)
        self._policy = policy
        self._capabilities = capability_policy or CapabilityPolicy("smart_cache")
        # In-memory set of game_ids currently being launched.
        # Eviction must not remove these.  Does not persist across restarts.
        self._active_launches: set[str] = set()

    # ── query ─────────────────────────────────────────────────────────────────

    def is_cached(self, game_id: str) -> bool:
        """True if the game has a complete, valid cache entry.

        For a multi-asset logical game (e.g. .cue + .bin tracks), this is
        only true when the launch asset *and every required companion
        asset* are present (and size-valid where known) — a cue with one
        missing track is treated as incomplete, never as a full hit.
        """
        entry = self._cache_repo.get(game_id)
        if entry is None:
            return False
        game = self._game_repo.get(game_id)
        launch_path = self._launch_asset_path(entry, game)
        if launch_path is None or not launch_path.exists():
            log.warning(
                "Cache entry for %s has no cached primary asset at %s — invalidating",
                game_id,
                launch_path,
            )
            self._cache_repo.delete(game_id)
            return False
        if not entry.is_complete:
            return False

        if game is not None and not self._all_assets_present(entry, game):
            log.warning(
                "Cache entry for %s is missing one or more required companion "
                "assets — treating as incomplete",
                game_id,
            )
            return False
        return True

    def has_valid_cached_assets(self, game_id: str) -> bool:
        """Read-only validity check for cached-library presentation.

        Unlike :meth:`is_cached`, this never repairs or deletes stale cache
        records. Presentation changes must not mutate cache state.
        """
        entry = self._cache_repo.get(game_id)
        game = self._game_repo.get(game_id)
        return self.is_valid_cached_entry(entry, game)

    def is_valid_cached_entry(
        self, entry: Optional[CacheEntry], game: Optional[Game]
    ) -> bool:
        """Same canonical validity rule as :meth:`has_valid_cached_assets`,
        but taking an already-loaded entry/game so bulk callers (e.g. mode
        presentation over the whole library) never issue a per-game
        database query. A complete cache entry whose resolved launch asset
        (and every companion asset) actually exists on disk is playable —
        this is the single source of truth for local-playability."""
        if entry is None or not entry.is_complete or game is None:
            return False
        launch_path = self._launch_asset_path(entry, game)
        if launch_path is None or not launch_path.exists():
            return False
        return self._all_assets_present(entry, game)

    def _all_assets_present(self, entry: CacheEntry, game: Game) -> bool:
        """True if every asset of *game* is present at its final cache path
        (and size-correct, where the expected size is known)."""
        for asset in game.assets:
            path = self._cached_asset_path(entry, game, asset)
            if path is None or not path.exists():
                return False
            if asset.size_bytes is not None:
                actual = _dir_size(path)
                if actual != asset.size_bytes:
                    return False
        return True

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
        path = self._launch_asset_path(entry, game)
        return str(path) if path is not None else None

    def _launch_asset_path(
        self, entry: CacheEntry, game: Optional[Game]
    ) -> Optional[Path]:
        if game is None or game.primary_asset is None:
            return None
        return self._cached_asset_path(entry, game, game.primary_asset)

    def _cached_asset_path(
        self, entry: CacheEntry, game: Game, asset: GameAsset
    ) -> Path:
        """Resolve *asset* across both supported on-disk cache layouts.

        Current caches mirror the asset's system-relative path directly
        below ``cache_root``.  Existing production caches may instead record
        a per-game container in ``entry.cache_path`` and store each asset
        below it by its original basename.  In the latter case the container
        itself is never a valid launch target when the nested asset exists.
        """
        direct = resolve_cache_path(
            self._cache_root, game.system, asset.relative_path
        )
        if direct.exists():
            return direct

        container = Path(entry.cache_path)
        if container.is_dir():
            nested = container / Path(asset.relative_path).name
            if nested.exists():
                return nested

        # Return the authoritative direct location for useful diagnostics and
        # for callers checking an incomplete cache.  A recorded directory is
        # deliberately not returned as a file asset.
        return direct

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
            self._touch_accessed(game_id)
            launch_path = self.get_launch_path(game_id)
            assert launch_path is not None  # guaranteed by is_cached
            return launch_path

        self._capabilities.require(Capability.GAME_DOWNLOAD, "Downloading a game")

        game = self._game_repo.get(game_id)
        if game is None:
            raise GameNotFoundError(f"Game not found in catalog: {game_id}")

        primary = game.primary_asset
        if primary is None:
            raise CacheError(f"Game {game_id!r} has no cacheable assets")

        # Directory packages remain unsized during catalog discovery so a
        # refresh stays O(entries), without a second recursive walk per
        # package.  Size them once here, when quota preflight actually needs
        # the number.
        needed = (
            game.total_size_bytes
            if game.total_size_bytes is not None
            else self._transfer.estimate_size(game)
        )
        self._ensure_space(needed, protected_game_id=game_id)

        # cache_path is fully determined by (system, primary asset's relative
        # path) — see romcloud.core.cache_paths — so it is already correct
        # even before the transfer completes.
        cache_path = str(resolve_cache_path(self._cache_root, game.system, primary.relative_path))

        # Create or update the entry to TRANSFERRING.
        existing = self._cache_repo.get(game_id)
        if existing is None:
            entry = CacheEntry.create(game_id=game_id, cache_path=cache_path)
            self._cache_repo.save(entry)
        else:
            self._cache_repo.update_status(game_id, CacheStatus.TRANSFERRING)

        try:
            final_path = self._transfer.transfer(game, on_progress)
            # Size recorded against the quota must cover *every* asset of
            # the logical game (e.g. .cue + all .bin tracks), never just
            # the primary/launch asset — otherwise multi-asset games would
            # silently under-count against the cache quota.
            actual_size = sum(
                _dir_size(resolve_cache_path(self._cache_root, game.system, a.relative_path))
                for a in game.assets
            )
            self._cache_repo.update_cache_path(game_id, final_path)
            self._cache_repo.update_status(game_id, CacheStatus.COMPLETE)
            self._cache_repo.update_size(game_id, actual_size)
            self._touch_accessed(game_id)

            updated_entry = self._cache_repo.get(game_id)
            assert updated_entry is not None
            launch_path = self._launch_asset_path(updated_entry, game)
            if launch_path is None or not launch_path.exists():
                raise CacheError(
                    f"Cache completed but the primary launch asset could not be resolved for {game_id}"
                )
            return str(launch_path)

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

        game = self._game_repo.get(game_id)
        self._remove_files(entry, game)
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

    def evict(
        self,
        bytes_needed: int = 0,
        *,
        protected_game_ids: Optional[set[str]] = None,
    ) -> list[str]:
        """Free space by evicting LRU-eligible entries.

        Eviction never removes:
        - Pinned games
        - Games currently transferring
        - Games currently launching (tracked in-memory this session)

        Returns a list of evicted game_ids.
        """
        evicted: list[str] = []
        protected = set(protected_game_ids or ())
        protected.update(self._active_launches)
        candidates = self._cache_repo.list_evictable_lru()

        for candidate in candidates:
            # Disk free space and repository usage are authoritative. Re-read
            # both after every removal rather than estimating reclaimed bytes.
            total = self._cache_repo.total_size()
            free = _free_bytes(str(self._cache_root))

            if self._has_space_for(total, free, bytes_needed):
                break

            if (
                candidate.game_id in protected
                or candidate.game_id in self._active_launches
            ):
                continue

            # The LRU list is a snapshot. Re-read before deletion so an entry
            # pinned or moved into an active transfer meanwhile is protected.
            entry = self._cache_repo.get(candidate.game_id)
            if entry is None or not entry.is_evictable:
                continue

            game = self._game_repo.get(entry.game_id)
            self._remove_files(entry, game)
            self._cache_repo.delete(entry.game_id)
            evicted.append(entry.game_id)
            log.info("Evicted %s (LRU)", entry.game_id)

        return evicted

    # ── helpers ───────────────────────────────────────────────────────────────

    def _has_space_for(
        self,
        total_cache_bytes: int,
        free_disk_bytes: int,
        bytes_needed: int,
    ) -> bool:
        """Return whether adding *bytes_needed* satisfies both policy limits."""
        return (
            total_cache_bytes + bytes_needed <= self._policy.max_size_bytes
            and free_disk_bytes - bytes_needed >= self._policy.min_free_bytes
        )

    def _ensure_space(
        self,
        bytes_needed: int,
        *,
        protected_game_id: Optional[str] = None,
    ) -> None:
        if bytes_needed > self._policy.max_size_bytes:
            raise InsufficientSpaceError(
                f"Game requires {bytes_needed / 1024**3:.1f} GB, which exceeds "
                f"the configured cache capacity of "
                f"{self._policy.max_size_bytes / 1024**3:.1f} GB"
            )

        total = self._cache_repo.total_size()
        free = _free_bytes(str(self._cache_root))

        if not self._has_space_for(total, free, bytes_needed):
            protected = {protected_game_id} if protected_game_id else set()
            self.evict(bytes_needed, protected_game_ids=protected)

        # Re-read authoritative values after eviction.
        total = self._cache_repo.total_size()
        free = _free_bytes(str(self._cache_root))
        if not self._has_space_for(total, free, bytes_needed):
            quota_remaining = max(0, self._policy.max_size_bytes - total)
            reserve_available = max(0, free - self._policy.min_free_bytes)
            raise InsufficientSpaceError(
                f"Not enough space to cache game after evicting all eligible entries: "
                f"need {bytes_needed / 1024**3:.1f} GB, "
                f"have {free / 1024**3:.1f} GB free / "
                f"{quota_remaining / 1024**3:.1f} GB of quota remaining / "
                f"{reserve_available / 1024**3:.1f} GB available above the "
                f"minimum free-space reserve; remaining cache entries are "
                f"pinned, launching, or transferring"
            )

    def _touch_accessed(self, game_id: str) -> None:
        self._cache_repo.update_last_accessed(game_id, datetime.now(timezone.utc))

    def _remove_files(self, entry: CacheEntry, game: Optional[Game]) -> None:
        """Remove every on-disk asset belonging to this cache entry.

        For a multi-asset logical game, every asset's exact path is removed
        (never just the recorded primary/launch-asset path) so no companion
        track is ever orphaned on disk. Falls back to the recorded
        ``cache_path`` alone if the game record is unavailable.
        """
        if game is not None and game.assets:
            for asset in game.assets:
                p = resolve_cache_path(self._cache_root, game.system, asset.relative_path)
                if p.exists():
                    if p.is_dir():
                        shutil.rmtree(p)
                    else:
                        p.unlink()
            return

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
