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
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from romcloud.core.cache_paths import resolve_cache_path
from romcloud.core.cancellation import TransferCancellationToken
from romcloud.core.dependency_resolvers import DESCRIPTOR_EXTENSIONS
from romcloud.core.exceptions import (
    CacheError,
    GameNotFoundError,
    GamePinnedError,
    InsufficientSpaceError,
    TransferCancelledError,
)
from romcloud.core.capabilities import Capability, CapabilityPolicy
from romcloud.core.models.cache import CacheEntry, CacheMember, CachePolicy, CacheStatus
from romcloud.core.models.game import Game, GameAsset
from romcloud.services.transfer import TransferService
from romcloud.infrastructure.logging import get_logger
from romcloud.infrastructure.repositories.cache import CacheRepository
from romcloud.infrastructure.repositories.game import GameRepository
from romcloud.services.dependencies import DependencyResolverRegistry
from romcloud.infrastructure.cache_coordination import (
    CacheStorageCoordinator,
    FileLockLease,
    LockUnavailable,
)

log = get_logger("cache")


@dataclass(frozen=True)
class PinnedDownloadPreflight:
    """Authoritative physical-storage plan for the current pinned set."""

    pinned_games: int
    games_needing_data: int
    additional_bytes: int
    current_cache_bytes: int
    max_cache_bytes: int
    free_bytes: int
    min_free_bytes: int
    game_ids: tuple[str, ...]
    allowed: bool
    reasons: tuple[str, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "pinned_games": self.pinned_games,
            "games_needing_data": self.games_needing_data,
            "additional_bytes": self.additional_bytes,
            "current_cache_bytes": self.current_cache_bytes,
            "resulting_cache_bytes": self.current_cache_bytes + self.additional_bytes,
            "max_cache_bytes": self.max_cache_bytes,
            "free_bytes": self.free_bytes,
            "resulting_free_bytes": self.free_bytes - self.additional_bytes,
            "min_free_bytes": self.min_free_bytes,
            "game_ids": list(self.game_ids),
            "allowed": self.allowed,
            "reasons": list(self.reasons),
        }


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
        dependency_resolver: Optional[DependencyResolverRegistry] = None,
        storage_coordinator: Optional[CacheStorageCoordinator] = None,
    ) -> None:
        self._cache_repo = cache_repo
        self._game_repo = game_repo
        self._transfer = transfer_service
        self._cache_root = Path(cache_root)
        self._policy = policy
        self._capabilities = capability_policy or CapabilityPolicy("smart_cache")
        self._dependencies = dependency_resolver
        self._storage = storage_coordinator
        if self._dependencies is None and hasattr(transfer_service, "provider"):
            self._dependencies = DependencyResolverRegistry(
                transfer_service.provider,
                source_root=transfer_service.source_root,
            )
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
        game = self._game_repo.get(game_id)
        return self.is_valid_cached_entry(entry, game)

    def has_valid_cached_assets(self, game_id: str) -> bool:
        """Read-only validity check for cached-library presentation.

        Unlike :meth:`is_cached`, this never repairs or deletes stale cache
        records. Presentation changes must not mutate cache state.
        """
        entry = self._cache_repo.get(game_id)
        game = self._game_repo.get(game_id)
        return self.is_valid_cached_entry(entry, game)

    def is_valid_cached_entry(
        self,
        entry: Optional[CacheEntry],
        game: Optional[Game],
        *,
        members: Optional[list[CacheMember]] = None,
        membership_resolved: Optional[bool] = None,
    ) -> bool:
        """Same canonical validity rule as :meth:`has_valid_cached_assets`,
        but taking an already-loaded entry/game. A complete cache entry whose
        persisted membership closure actually exists on disk is playable.

        Bulk presentation callers may supply the membership state to avoid
        opening SQLite connections once per cached game.
        """
        if entry is None or not entry.is_complete or game is None:
            return False
        if membership_resolved is None:
            membership_resolved = self._cache_repo.membership_resolved(entry.game_id)
        if not membership_resolved:
            return False
        if members is None:
            members = self._cache_repo.list_members(entry.game_id)
        return self._all_members_present(entry, game, members)

    def _all_members_present(
        self, entry: CacheEntry, game: Game, members: list[CacheMember]
    ) -> bool:
        """Validate the persisted closure without consulting the source."""
        if not members or not any(member.is_primary for member in members):
            return False
        for member in members:
            path = self._cached_member_path(entry, game, member)
            if not path.exists() or path.is_symlink():
                return False
            if member.expected_size is not None:
                actual = _dir_size(path)
                if actual != member.expected_size:
                    return False
        return True

    def get_entry(self, game_id: str) -> Optional[CacheEntry]:
        return self._cache_repo.get(game_id)

    def effective_status(self, entry: CacheEntry) -> CacheStatus:
        """Return dependency-aware status without changing durable history."""
        if entry.status is CacheStatus.COMPLETE and not self.is_cached(entry.game_id):
            return CacheStatus.INCOMPLETE
        return entry.status

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
        if game is None:
            return None
        primary = next(
            (
                member
                for member in self._cache_repo.list_members(entry.game_id)
                if member.is_primary
            ),
            None,
        )
        return self._cached_member_path(entry, game, primary) if primary else None

    def _cached_member_path(
        self, entry: CacheEntry, game: Game, member: CacheMember
    ) -> Path:
        return self._cached_asset_path(
            entry,
            game,
            GameAsset(
                filename=Path(member.relative_path).name,
                relative_path=member.relative_path,
                size_bytes=member.expected_size,
                is_primary=member.is_primary,
            ),
        )

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
            "complete": sum(
                1
                for entry in entries
                if self.is_valid_cached_entry(
                    entry, self._game_repo.get(entry.game_id)
                )
            ),
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
        cancellation: Optional[TransferCancellationToken] = None,
        *,
        owner_kind: str = "cli",
        owner_instance_id: Optional[str] = None,
        download_item_id: Optional[str] = None,
        _asset_lock: Optional[FileLockLease] = None,
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
        if cancellation is not None:
            cancellation.raise_if_cancelled()

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

        existing = self._cache_repo.get(game_id)
        resolved_game = self._resolved_game(game, existing)

        # Physical destination ownership is process-wide and keyed by the
        # resolved asset closure, so shared playlist dependencies collide.
        if self._storage is not None and _asset_lock is None:
            paths = [
                resolve_cache_path(self._cache_root, resolved_game.system, asset.relative_path)
                for asset in resolved_game.assets
            ]
            with self._storage.assets.acquire(paths, blocking=True) as lease:
                # Re-check after waiting: a background owner may have completed
                # the exact same physical transfer while this process waited.
                return self.cache_game(
                    game_id,
                    on_progress,
                    cancellation,
                    owner_kind=owner_kind,
                    owner_instance_id=owner_instance_id,
                    download_item_id=download_item_id,
                    _asset_lock=lease,
                )

        actual_before = self._existing_member_sizes(existing, resolved_game)
        needed = sum(
            asset.size_bytes or 0
            for asset in resolved_game.assets
            if asset.relative_path not in actual_before
        )
        if any(
            asset.size_bytes is None
            and asset.relative_path not in actual_before
            for asset in resolved_game.assets
        ):
            needed = max(
                needed,
                self._transfer.estimate_size(resolved_game)
                - sum(actual_before.values()),
            )
        staged_before = self._transfer.staging_size(resolved_game)
        needed = max(0, needed - staged_before)
        reservation = None
        if self._storage is not None:
            reservation = self._storage.admit(
                requested_growth=needed,
                game_id=game_id,
                owner_kind=owner_kind,
                owner_instance_id=owner_instance_id or uuid.uuid4().hex,
                download_item_id=download_item_id,
                evict=lambda amount: self.evict(
                    amount, protected_game_ids={game_id}
                ),
            )
        else:
            self._ensure_space(needed, protected_game_id=game_id)
        if cancellation is not None:
            cancellation.raise_if_cancelled()

        # cache_path is fully determined by (system, primary asset's relative
        # path) — see romcloud.core.cache_paths — so it is already correct
        # even before the transfer completes.
        cache_path = str(
            resolve_cache_path(
                self._cache_root, resolved_game.system, primary.relative_path
            )
        )

        # Create or update the entry to TRANSFERRING.
        if existing is None:
            entry = CacheEntry.create(game_id=game_id, cache_path=cache_path)
            self._cache_repo.save(entry)
        else:
            self._cache_repo.update_status(game_id, CacheStatus.TRANSFERRING)
        self._cache_repo.replace_membership(
            game_id, resolved_game.assets, actual_before
        )

        try:
            initial_present = staged_before + sum(actual_before.values())

            def coordinated_progress(done: int, total: int) -> None:
                if reservation is not None:
                    reservation.shrink_to(max(0, needed - max(0, done - initial_present)))
                if on_progress is not None:
                    on_progress(done, total)

            progress = coordinated_progress if reservation is not None else on_progress
            if cancellation is None:
                final_path = self._transfer.transfer(resolved_game, progress)
            else:
                final_path = self._transfer.transfer(
                    resolved_game, progress, cancellation=cancellation
                )
                cancellation.raise_if_cancelled()
            # Size recorded against the quota must cover *every* asset of
            # the logical game (e.g. .cue + all .bin tracks), never just
            # the primary/launch asset. Entry size remains a logical-game
            # figure; quota uses distinct persisted membership paths.
            actual_sizes = {
                asset.relative_path: _dir_size(
                    resolve_cache_path(
                        self._cache_root,
                        resolved_game.system,
                        asset.relative_path,
                    )
                )
                for asset in resolved_game.assets
            }
            actual_size = sum(actual_sizes.values())
            if cancellation is not None:
                cancellation.raise_if_cancelled()
            launch_asset = resolved_game.primary_asset
            launch_path = (
                resolve_cache_path(
                    self._cache_root, resolved_game.system, launch_asset.relative_path
                )
                if launch_asset is not None else None
            )
            if launch_path is None or not launch_path.exists():
                raise CacheError(
                    f"Cache completed but the primary launch asset could not be resolved for {game_id}"
                )
            if cancellation is not None:
                cancellation.raise_if_cancelled()
            self._cache_repo.finalize_transfer(
                game_id=game_id,
                cache_path=final_path,
                sizes=actual_sizes,
                staging_assets=[asset.relative_path for asset in resolved_game.assets],
                download_item_id=download_item_id,
            )
            return str(launch_path)

        except TransferCancelledError:
            # Staging remains isolated under .partial for a safe subsequent
            # retry. It is neither a valid cache hit nor a transfer failure.
            self._cache_repo.update_status(game_id, CacheStatus.INCOMPLETE)
            raise
        except Exception:
            self._cache_repo.update_status(game_id, CacheStatus.FAILED)
            raise
        finally:
            if reservation is not None:
                reservation.release()

    def resolved_asset_paths(self, game_id: str) -> list[Path]:
        """Return canonical final physical destinations for worker claiming."""
        game = self._game_repo.get(game_id)
        if game is None:
            raise GameNotFoundError(f"Game not found in catalog: {game_id}")
        resolved = self._resolved_game(game, self._cache_repo.get(game_id))
        return [
            resolve_cache_path(self._cache_root, resolved.system, asset.relative_path)
            for asset in resolved.assets
        ]

    def try_asset_locks(self, game_id: str) -> Optional[FileLockLease]:
        if self._storage is None:
            return FileLockLease([])
        try:
            return self._storage.assets.acquire(
                self.resolved_asset_paths(game_id), blocking=False
            )
        except LockUnavailable:
            return None

    def retained_staging_size(self, game_id: str) -> int:
        game = self._game_repo.get(game_id)
        if game is None:
            return 0
        resolved = self._resolved_game(game, self._cache_repo.get(game_id))
        return self._transfer.staging_size(resolved)

    def prepare_download_membership(self, game_id: str) -> None:
        """Persist the physical closure before queueing for shared ownership."""
        game = self._game_repo.get(game_id)
        if game is None:
            raise GameNotFoundError(f"Game not found in catalog: {game_id}")
        existing = self._cache_repo.get(game_id)
        resolved = self._resolved_game(game, existing)
        actual = self._existing_member_sizes(existing, resolved)
        if existing is None:
            primary = resolved.primary_asset
            if primary is None:
                raise CacheError(f"Game {game_id!r} has no cacheable assets")
            entry = CacheEntry.create(
                game_id,
                str(resolve_cache_path(
                    self._cache_root, resolved.system, primary.relative_path
                )),
            )
            entry.status = CacheStatus.INCOMPLETE
            self._cache_repo.save(entry)
        self._cache_repo.replace_membership(game_id, resolved.assets, actual)

    def discard_staging(self, game_id: str) -> None:
        """Discard resumable bytes while protecting shared physical assets."""
        game = self._game_repo.get(game_id)
        if game is None:
            return
        resolved = self._resolved_game(game, self._cache_repo.get(game_id))
        paths = [
            resolve_cache_path(self._cache_root, resolved.system, asset.relative_path)
            for asset in resolved.assets
        ]
        lease = (
            self._storage.assets.acquire(paths, blocking=False)
            if self._storage is not None
            else FileLockLease([])
        )
        with lease:
            preserve = {
                asset.relative_path
                for asset in resolved.assets
                if self._cache_repo.owner_count(asset.relative_path) > 1
            }
            self._transfer.discard_staging(
                resolved, preserve_relative_paths=preserve
            )

    def remove(
        self,
        game_id: str,
        force: bool = False,
        *,
        _asset_lock: Optional[FileLockLease] = None,
    ) -> None:
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
        resolved = self._resolved_game(game, entry) if game is not None else None
        if self._storage is not None and _asset_lock is None and resolved is not None:
            paths = [
                resolve_cache_path(self._cache_root, resolved.system, asset.relative_path)
                for asset in resolved.assets
            ]
            with self._storage.assets.acquire(paths, blocking=True) as lease:
                return self.remove(game_id, force=force, _asset_lock=lease)
        if resolved is not None and entry.status is not CacheStatus.COMPLETE:
            preserve = {
                asset.relative_path
                for asset in resolved.assets
                if self._cache_repo.owner_count(asset.relative_path) > 1
            }
            self._transfer.discard_staging(
                resolved, preserve_relative_paths=preserve
            )
        self._remove_files(entry, game)
        self._cache_repo.delete(game_id)
        log.info("Removed cache entry %s", game_id)

    def pin(self, game_id: str) -> None:
        entry = self._cache_repo.get(game_id)
        if entry is None:
            game = self._game_repo.get(game_id)
            if game is None or not game.is_eligible:
                raise GameNotFoundError(f"Game not found in eligible catalog: {game_id}")
            primary = game.primary_asset
            if primary is None:
                raise CacheError(f"Game {game_id!r} has no cacheable assets")
            now = datetime.now(timezone.utc)
            entry = CacheEntry(
                game_id=game_id,
                cache_path=str(
                    resolve_cache_path(
                        self._cache_root, game.system, primary.relative_path
                    )
                ),
                status=CacheStatus.INCOMPLETE,
                cached_at=now,
                last_accessed=now,
                size_bytes=0,
                is_pinned=True,
            )
            self._cache_repo.save(entry)
            log.info("Pinned remote catalog game %s for later download", game_id)
            return
        self._cache_repo.set_pinned(game_id, True)
        log.info("Pinned %s", game_id)

    def unpin(self, game_id: str) -> None:
        """Unpin a game.  Does NOT remove the cached file."""
        entry = self._cache_repo.get(game_id)
        if entry is None:
            return
        if (
            entry.status is CacheStatus.INCOMPLETE
            and entry.size_bytes == 0
            and not self._cache_repo.list_members(game_id)
        ):
            self._cache_repo.delete(game_id)
            log.info("Removed undownloaded pin %s", game_id)
            return
        self._cache_repo.set_pinned(game_id, False)
        log.info("Unpinned %s", game_id)

    def preflight_pinned(self, *, free_bytes: Optional[int] = None) -> PinnedDownloadPreflight:
        """Resolve pinned misses and calculate their deduplicated closure.

        Ordinary browsing never calls this method. Descriptor reads and
        recursive directory sizing are limited to the explicit preflight.
        """
        entries = self._cache_repo.list_pinned()
        needed_games: list[str] = []
        missing_paths: dict[str, int] = {}
        for entry in entries:
            game = self._game_repo.get(entry.game_id)
            if game is None or not game.is_eligible:
                continue
            if self.is_valid_cached_entry(entry, game):
                continue
            resolved = self._resolved_game(game, entry)
            existing = self._existing_member_sizes(entry, resolved)
            for asset in resolved.assets:
                if asset.relative_path in existing:
                    continue
                size = self._transfer.estimate_asset_size(resolved, asset)
                missing_paths[asset.relative_path] = max(
                    missing_paths.get(asset.relative_path, 0), int(size or 0)
                )
            needed_games.append(entry.game_id)

        additional = sum(missing_paths.values())
        current = self._cache_repo.total_size()
        free = _free_bytes(str(self._cache_root)) if free_bytes is None else free_bytes
        reasons: list[str] = []
        if current + additional > self._policy.max_size_bytes:
            reasons.append(
                "Pinned downloads would exceed the configured cache-size limit. "
                "Remove local copies or unpin games first."
            )
        if free - additional < self._policy.min_free_bytes:
            reasons.append(
                "Pinned downloads would reduce filesystem free space below the "
                "configured minimum reserve. Remove local copies or unpin games first."
            )
        return PinnedDownloadPreflight(
            pinned_games=len(entries),
            games_needing_data=len(needed_games),
            additional_bytes=additional,
            current_cache_bytes=current,
            max_cache_bytes=self._policy.max_size_bytes,
            free_bytes=free,
            min_free_bytes=self._policy.min_free_bytes,
            game_ids=tuple(needed_games),
            allowed=not reasons,
            reasons=tuple(reasons),
        )

    def download_pinned(
        self,
        *,
        on_game: Optional[Callable[[int, int, str], None]] = None,
        on_progress: Optional[Callable[[int, int], None]] = None,
    ) -> list[str]:
        """Download/repair the pinned misses after a fresh preflight."""
        plan = self.preflight_pinned()
        if not plan.allowed:
            raise InsufficientSpaceError(" ".join(plan.reasons))
        completed: list[str] = []
        total = len(plan.game_ids)
        for index, game_id in enumerate(plan.game_ids, 1):
            if on_game is not None:
                on_game(index, total, game_id)
            self.cache_game(game_id, on_progress=on_progress)
            completed.append(game_id)
        return completed

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
            lock = None
            if self._storage is not None and game is not None:
                try:
                    resolved = self._resolved_game(game, entry)
                    lock = self._storage.assets.acquire(
                        [
                            resolve_cache_path(
                                self._cache_root, resolved.system, asset.relative_path
                            )
                            for asset in resolved.assets
                        ],
                        blocking=False,
                    )
                except LockUnavailable:
                    continue
            try:
                self._remove_files(entry, game)
                self._cache_repo.delete(entry.game_id)
                evicted.append(entry.game_id)
                log.info("Evicted %s (LRU)", entry.game_id)
            finally:
                if lock is not None:
                    lock.release()

        return evicted

    # ── helpers ───────────────────────────────────────────────────────────────

    def _existing_member_sizes(
        self, entry: Optional[CacheEntry], game: Game
    ) -> dict[str, int]:
        """Return valid existing bytes that can be adopted by this snapshot."""
        sizes: dict[str, int] = {}
        for asset in game.assets:
            direct = resolve_cache_path(
                self._cache_root, game.system, asset.relative_path
            )
            path = (
                self._cached_asset_path(entry, game, asset)
                if entry is not None
                else direct
            )
            if not path.exists() or path.is_symlink():
                continue
            actual = _dir_size(path)
            if asset.size_bytes is None or actual == asset.size_bytes:
                sizes[asset.relative_path] = actual
        return sizes

    def _resolved_game(self, game: Game, entry: Optional[CacheEntry]) -> Game:
        """Use a persisted closure when present, otherwise resolve lazily."""
        members = self._cache_repo.list_members(game.id)
        if entry is not None and self._cache_repo.membership_resolved(game.id) and members:
            return self._game_from_membership(game, members)
        if self._dependencies is not None:
            return self._dependencies.resolve(game)
        primary = game.primary_asset
        if primary and Path(primary.filename).suffix.lower() in DESCRIPTOR_EXTENSIONS:
            raise CacheError(
                "Dependency resolution is unavailable for descriptor game "
                f"{game.id!r}"
            )
        return game

    @staticmethod
    def _game_from_membership(game: Game, members: list[CacheMember]) -> Game:
        """Rebuild a transfer view from the persisted, source-independent snapshot."""
        from dataclasses import replace

        return replace(
            game,
            assets=[
                GameAsset(
                    filename=Path(member.relative_path).name,
                    relative_path=member.relative_path,
                    size_bytes=member.expected_size,
                    is_primary=member.is_primary,
                )
                for member in members
            ],
        )

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
        """Remove persisted members only when this is their last owner."""
        members = self._cache_repo.list_members(entry.game_id)
        if game is not None and members:
            for member in members:
                if self._cache_repo.owner_count(member.relative_path) > 1:
                    continue
                p = self._cached_member_path(entry, game, member)
                if (
                    p.exists()
                    and not p.is_symlink()
                    and _is_within(p, self._cache_root)
                ):
                    if p.is_dir():
                        shutil.rmtree(p)
                    else:
                        p.unlink()
            return

        p = Path(entry.cache_path)
        if p.exists() and not p.is_symlink() and _is_within(p, self._cache_root):
            if p.is_dir():
                shutil.rmtree(p)
            else:
                p.unlink()


def _free_bytes(path: str) -> int:
    try:
        stat = os.statvfs(path)
        return stat.f_bavail * stat.f_frsize
    except AttributeError:
        return shutil.disk_usage(path).free
    except OSError:
        return 0


def _dir_size(path: Path) -> int:
    if path.is_file():
        return path.stat().st_size
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file())


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except (OSError, ValueError):
        return False
    return True
