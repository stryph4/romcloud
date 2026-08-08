"""Unit tests for the database and repositories."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from romcloud.infrastructure.database import Database
from romcloud.infrastructure.repositories.game import GameRepository
from romcloud.infrastructure.repositories.cache import CacheRepository
from romcloud.infrastructure.repositories.proxy import ProxyRepository
from romcloud.core.models.game import Game, GameAsset
from romcloud.core.models.cache import CacheEntry, CacheStatus
from romcloud.core.models.proxy import ProxyRecord


class TestDatabase:
    def test_initialize_idempotent(self, tmp_path):
        db = Database(str(tmp_path / "test.db"))
        db.initialize()
        db.initialize()  # second call must not raise

    def test_creates_parent_dirs(self, tmp_path):
        db = Database(str(tmp_path / "deep" / "dir" / "test.db"))
        db.initialize()
        assert db.path.exists()


class TestGameRepository:
    def test_save_and_get(self, game_repo, db):
        game = Game.create(
            "ps2", "Test Game", "local", "/roms",
            [GameAsset("game.iso", "ps2/game.iso", size_bytes=100, is_primary=True)],
        )
        game_repo.save(game)
        fetched = game_repo.get(game.id)
        assert fetched is not None
        assert fetched.title == "Test Game"
        assert fetched.system == "ps2"
        assert len(fetched.assets) == 1
        assert fetched.assets[0].filename == "game.iso"
        assert fetched.assets[0].is_primary is True

    def test_save_replaces(self, game_repo):
        game = Game.create("ps2", "Original", "local", "/roms", [])
        game_repo.save(game)
        updated = Game(
            id=game.id,
            system=game.system,
            title="Updated",
            source_provider=game.source_provider,
            source_root=game.source_root,
            assets=[],
            added_at=game.added_at,
        )
        game_repo.save(updated)
        fetched = game_repo.get(game.id)
        assert fetched is not None
        assert fetched.title == "Updated"

    def test_get_nonexistent_returns_none(self, game_repo):
        assert game_repo.get("nonexistent-id") is None

    def test_find_by_system(self, game_repo):
        for title in ("Game A", "Game B"):
            game_repo.save(Game.create("ps2", title, "local", "/roms", []))
        game_repo.save(Game.create("nes", "NES Game", "local", "/roms", []))

        ps2_games = game_repo.find_by_system("ps2")
        assert len(ps2_games) == 2
        assert all(g.system == "ps2" for g in ps2_games)

    def test_find_by_source_path(self, game_repo):
        asset = GameAsset("game.iso", "ps2/game.iso", is_primary=True)
        game = Game.create("ps2", "Test", "local", "/roms", [asset])
        game_repo.save(game)

        found = game_repo.find_by_source_path("local", "/roms", "ps2/game.iso")
        assert found is not None
        assert found.id == game.id

    def test_find_by_source_path_no_match(self, game_repo):
        found = game_repo.find_by_source_path("local", "/roms", "ps2/missing.iso")
        assert found is None

    def test_delete(self, game_repo):
        game = Game.create("ps2", "To Delete", "local", "/roms", [])
        game_repo.save(game)
        game_repo.delete(game.id)
        assert game_repo.get(game.id) is None

    def test_list_all(self, game_repo):
        for title in ("B", "A", "C"):
            game_repo.save(Game.create("ps2", title, "local", "/roms", []))
        games = game_repo.list_all()
        assert len(games) == 3

    def test_count(self, game_repo):
        assert game_repo.count() == 0
        game_repo.save(Game.create("nes", "x", "local", "/roms", []))
        assert game_repo.count() == 1


class TestCacheRepository:
    def _add_game(self, game_repo, system="ps2", title="Test"):
        game = Game.create(system, title, "local", "/roms", [])
        game_repo.save(game)
        return game

    def test_save_and_get(self, game_repo, cache_repo):
        game = self._add_game(game_repo)
        entry = CacheEntry.create(game.id, "/cache/test")
        entry.status = CacheStatus.COMPLETE
        cache_repo.save(entry)
        fetched = cache_repo.get(game.id)
        assert fetched is not None
        assert fetched.cache_path == "/cache/test"

    def test_update_status(self, game_repo, cache_repo):
        game = self._add_game(game_repo)
        entry = CacheEntry.create(game.id, "/cache/test")
        cache_repo.save(entry)
        cache_repo.update_status(game.id, CacheStatus.COMPLETE)
        assert cache_repo.get(game.id).status == CacheStatus.COMPLETE

    def test_update_cache_path(self, game_repo, cache_repo):
        game = self._add_game(game_repo)
        entry = CacheEntry.create(game.id, "/cache/placeholder")
        cache_repo.save(entry)
        cache_repo.update_cache_path(game.id, "/cache/ps2/Game.iso")
        assert cache_repo.get(game.id).cache_path == "/cache/ps2/Game.iso"

    def test_set_pinned(self, game_repo, cache_repo):
        game = self._add_game(game_repo)
        entry = CacheEntry.create(game.id, "/cache/test")
        cache_repo.save(entry)
        cache_repo.set_pinned(game.id, True)
        assert cache_repo.get(game.id).is_pinned is True
        cache_repo.set_pinned(game.id, False)
        assert cache_repo.get(game.id).is_pinned is False

    def test_total_size(self, game_repo, cache_repo):
        for i, size in enumerate([1000, 2000, 500]):
            game = self._add_game(game_repo, title=f"Game {i}")
            e = CacheEntry.create(game.id, f"/cache/{i}")
            e.status = CacheStatus.COMPLETE
            e.size_bytes = size
            cache_repo.save(e)
        assert cache_repo.total_size() == 3500

    def test_list_evictable_lru_excludes_pinned(self, game_repo, cache_repo):
        game = self._add_game(game_repo)
        entry = CacheEntry.create(game.id, "/cache/x")
        entry.status = CacheStatus.COMPLETE
        entry.is_pinned = True
        cache_repo.save(entry)
        assert cache_repo.list_evictable_lru() == []

    def test_delete_removes_entry(self, game_repo, cache_repo):
        game = self._add_game(game_repo)
        entry = CacheEntry.create(game.id, "/cache/x")
        cache_repo.save(entry)
        cache_repo.delete(game.id)
        assert cache_repo.get(game.id) is None


class TestProxyRepository:
    def _add_game(self, game_repo):
        game = Game.create("ps2", "Test", "local", "/roms", [])
        game_repo.save(game)
        return game

    def test_save_and_get(self, game_repo, proxy_repo):
        game = self._add_game(game_repo)
        record = ProxyRecord.create(game.id, "/userdata/roms/ps2/Test.romcloud")
        proxy_repo.save(record)
        fetched = proxy_repo.get(game.id)
        assert fetched is not None
        assert fetched.proxy_path == "/userdata/roms/ps2/Test.romcloud"

    def test_owns_path_true(self, game_repo, proxy_repo):
        game = self._add_game(game_repo)
        record = ProxyRecord.create(game.id, "/roms/ps2/Test.romcloud")
        proxy_repo.save(record)
        assert proxy_repo.owns_path("/roms/ps2/Test.romcloud") is True

    def test_owns_path_false(self, proxy_repo):
        assert proxy_repo.owns_path("/roms/ps2/NotCreatedByUs.romcloud") is False

    def test_get_by_path(self, game_repo, proxy_repo):
        game = self._add_game(game_repo)
        record = ProxyRecord.create(game.id, "/roms/ps2/Test.romcloud")
        proxy_repo.save(record)
        fetched = proxy_repo.get_by_path("/roms/ps2/Test.romcloud")
        assert fetched is not None
        assert fetched.game_id == game.id

    def test_delete(self, game_repo, proxy_repo):
        game = self._add_game(game_repo)
        record = ProxyRecord.create(game.id, "/roms/ps2/Test.romcloud")
        proxy_repo.save(record)
        proxy_repo.delete(game.id)
        assert proxy_repo.get(game.id) is None
