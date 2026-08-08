"""Unit tests for CacheService."""

from __future__ import annotations

from pathlib import Path

import pytest

from romcloud.core.models.game import Game, GameAsset
from romcloud.core.models.cache import CacheEntry, CachePolicy, CacheStatus
from romcloud.core.exceptions import GameNotFoundError, GamePinnedError, CacheError, InsufficientSpaceError


@pytest.fixture
def game_with_file(game_repo, rom_root) -> Game:
    asset = GameAsset(
        filename="Final Fantasy X.iso",
        relative_path="ps2/Final Fantasy X.iso",
        size_bytes=(rom_root / "ps2" / "Final Fantasy X.iso").stat().st_size,
        is_primary=True,
    )
    game = Game.create("ps2", "Final Fantasy X", "local", str(rom_root), [asset])
    game_repo.save(game)
    return game


class TestCacheServiceIsNotCached:
    def test_uncached_game_returns_false(self, cache_service, game_with_file):
        assert cache_service.is_cached(game_with_file.id) is False

    def test_missing_path_invalidates_entry(self, cache_service, cache_repo, game_repo, game_with_file):
        # Manually insert an entry pointing to a nonexistent path
        entry = CacheEntry.create(game_with_file.id, "/nonexistent/path")
        entry.status = CacheStatus.COMPLETE
        cache_repo.save(entry)
        assert cache_service.is_cached(game_with_file.id) is False
        # Entry should be cleaned up
        assert cache_repo.get(game_with_file.id) is None


class TestCacheGameCaching:
    def test_cache_game_succeeds(self, cache_service, game_with_file):
        path = cache_service.cache_game(game_with_file.id)
        assert path is not None
        assert Path(path).exists()

    def test_cached_status_updated(self, cache_service, cache_repo, game_with_file):
        cache_service.cache_game(game_with_file.id)
        entry = cache_repo.get(game_with_file.id)
        assert entry is not None
        assert entry.status == CacheStatus.COMPLETE

    def test_cache_game_returns_launch_path(self, cache_service, game_with_file):
        path = cache_service.cache_game(game_with_file.id)
        assert path.endswith("Final Fantasy X.iso")

    def test_cache_game_already_cached(self, cache_service, game_with_file):
        # Second call should be instant (already cached)
        path1 = cache_service.cache_game(game_with_file.id)
        path2 = cache_service.cache_game(game_with_file.id)
        assert path1 == path2

    def test_cache_game_not_in_catalog_raises(self, cache_service):
        with pytest.raises(GameNotFoundError):
            cache_service.cache_game("does-not-exist")


class TestPinUnpin:
    def test_pin_cached_game(self, cache_service, cache_repo, game_with_file):
        cache_service.cache_game(game_with_file.id)
        cache_service.pin(game_with_file.id)
        assert cache_repo.get(game_with_file.id).is_pinned is True

    def test_unpin_does_not_remove(self, cache_service, cache_repo, game_with_file, cache_dir):
        cache_service.cache_game(game_with_file.id)
        cache_service.pin(game_with_file.id)
        cache_service.unpin(game_with_file.id)
        assert cache_repo.get(game_with_file.id).is_pinned is False
        # File still exists
        assert cache_service.is_cached(game_with_file.id)

    def test_pin_uncached_raises(self, cache_service, game_with_file):
        with pytest.raises(CacheError):
            cache_service.pin(game_with_file.id)


class TestRemove:
    def test_remove_cached(self, cache_service, cache_repo, game_with_file, cache_dir):
        cache_service.cache_game(game_with_file.id)
        cache_service.remove(game_with_file.id)
        assert cache_repo.get(game_with_file.id) is None
        assert not cache_service.is_cached(game_with_file.id)

    def test_remove_pinned_without_force_raises(self, cache_service, game_with_file):
        cache_service.cache_game(game_with_file.id)
        cache_service.pin(game_with_file.id)
        with pytest.raises(GamePinnedError):
            cache_service.remove(game_with_file.id)

    def test_remove_pinned_with_force(self, cache_service, cache_repo, game_with_file):
        cache_service.cache_game(game_with_file.id)
        cache_service.pin(game_with_file.id)
        cache_service.remove(game_with_file.id, force=True)
        assert cache_repo.get(game_with_file.id) is None

    def test_remove_nonexistent_is_noop(self, cache_service):
        # Should not raise
        cache_service.remove("nonexistent-id")


class TestEviction:
    def test_evict_by_lru(self, game_repo, cache_service, cache_repo, cache_dir, rom_root):
        """LRU eviction removes oldest-accessed unpinned game first."""
        from datetime import datetime, timezone, timedelta

        games = []
        for i, name in enumerate(["Final Fantasy X", "Shadow of the Colossus"]):
            asset = GameAsset(
                filename=f"{name}.iso",
                relative_path=f"ps2/{name}.iso",
                size_bytes=(rom_root / "ps2" / f"{name}.iso").stat().st_size,
                is_primary=True,
            )
            g = Game.create("ps2", name, "local", str(rom_root), [asset])
            game_repo.save(g)
            games.append(g)
            cache_service.cache_game(g.id)

        # Manually set last_accessed so we can predict LRU order
        old_time = datetime.now(timezone.utc) - timedelta(days=30)
        cache_repo.update_last_accessed(games[0].id, old_time)
        # games[1] has a more recent last_accessed

        # Now evict by forcing a very small policy
        import os
        # Patch policy to a tiny quota
        from romcloud.core.models.cache import CachePolicy
        cache_service._policy = CachePolicy(max_size_bytes=1, min_free_bytes=0)
        evicted = cache_service.evict()

        # The oldest game should have been evicted
        assert games[0].id in evicted
        assert not cache_service.is_cached(games[0].id)

    def test_evict_skips_pinned(self, game_repo, cache_service, cache_repo, cache_dir, rom_root):
        asset = GameAsset(
            filename="Final Fantasy X.iso",
            relative_path="ps2/Final Fantasy X.iso",
            size_bytes=(rom_root / "ps2" / "Final Fantasy X.iso").stat().st_size,
            is_primary=True,
        )
        game = Game.create("ps2", "Final Fantasy X", "local", str(rom_root), [asset])
        game_repo.save(game)
        cache_service.cache_game(game.id)
        cache_service.pin(game.id)

        from romcloud.core.models.cache import CachePolicy
        cache_service._policy = CachePolicy(max_size_bytes=1, min_free_bytes=0)
        evicted = cache_service.evict()
        assert game.id not in evicted
        assert cache_service.is_cached(game.id)
