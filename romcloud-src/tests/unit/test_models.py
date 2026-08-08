"""Unit tests for domain models."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from romcloud.core.models.game import Game, GameAsset, derive_title
from romcloud.core.models.cache import CacheEntry, CachePolicy, CacheStatus
from romcloud.core.models.proxy import ProxyRecord


class TestGameAsset:
    def test_frozen(self):
        asset = GameAsset(filename="game.iso", relative_path="ps2/game.iso", is_primary=True)
        with pytest.raises((AttributeError, TypeError)):
            asset.filename = "other.iso"  # type: ignore[misc]


class TestGame:
    def test_create_generates_uuid(self):
        g1 = Game.create("ps2", "Test", "local", "/roms", [])
        g2 = Game.create("ps2", "Test", "local", "/roms", [])
        assert g1.id != g2.id

    def test_primary_asset_returns_flagged(self):
        assets = [
            GameAsset("disc1.bin", "ps2/disc1.bin", is_primary=False),
            GameAsset("game.cue", "ps2/game.cue", is_primary=True),
        ]
        game = Game.create("ps2", "Test", "local", "/roms", assets)
        assert game.primary_asset is not None
        assert game.primary_asset.filename == "game.cue"

    def test_primary_asset_fallback(self):
        assets = [GameAsset("game.iso", "ps2/game.iso")]
        game = Game.create("ps2", "Test", "local", "/roms", assets)
        assert game.primary_asset is not None
        assert game.primary_asset.filename == "game.iso"

    def test_primary_asset_none_when_empty(self):
        game = Game.create("ps2", "Test", "local", "/roms", [])
        assert game.primary_asset is None

    def test_total_size_bytes_sums(self):
        assets = [
            GameAsset("a.bin", "ps2/a.bin", size_bytes=100),
            GameAsset("b.bin", "ps2/b.bin", size_bytes=200),
        ]
        game = Game.create("ps2", "Test", "local", "/roms", assets)
        assert game.total_size_bytes == 300

    def test_total_size_bytes_none_if_unknown(self):
        assets = [
            GameAsset("a.bin", "ps2/a.bin", size_bytes=100),
            GameAsset("b.bin", "ps2/b.bin", size_bytes=None),
        ]
        game = Game.create("ps2", "Test", "local", "/roms", assets)
        assert game.total_size_bytes is None


class TestCacheEntry:
    def test_is_complete(self):
        entry = CacheEntry(
            game_id="x",
            cache_path="/cache/x",
            status=CacheStatus.COMPLETE,
            cached_at=datetime.now(timezone.utc),
            last_accessed=datetime.now(timezone.utc),
            size_bytes=100,
        )
        assert entry.is_complete
        assert entry.is_evictable

    def test_transferring_not_evictable(self):
        entry = CacheEntry(
            game_id="x",
            cache_path="/cache/x",
            status=CacheStatus.TRANSFERRING,
            cached_at=datetime.now(timezone.utc),
            last_accessed=datetime.now(timezone.utc),
            size_bytes=0,
        )
        assert not entry.is_complete
        assert not entry.is_evictable

    def test_pinned_not_evictable(self):
        entry = CacheEntry(
            game_id="x",
            cache_path="/cache/x",
            status=CacheStatus.COMPLETE,
            cached_at=datetime.now(timezone.utc),
            last_accessed=datetime.now(timezone.utc),
            size_bytes=100,
            is_pinned=True,
        )
        assert entry.is_complete
        assert not entry.is_evictable

    def test_create_defaults(self):
        entry = CacheEntry.create("abc", "/cache/abc")
        assert entry.status == CacheStatus.TRANSFERRING
        assert entry.is_pinned is False
        assert entry.size_bytes == 0


class TestCachePolicy:
    def test_from_gb(self):
        policy = CachePolicy.from_gb(10.0, 2.0)
        assert policy.max_size_bytes == int(10 * 1024**3)
        assert policy.min_free_bytes == int(2 * 1024**3)

    def test_within_limits(self):
        policy = CachePolicy.from_gb(10.0, 2.0)
        assert policy.is_within_limits(int(5 * 1024**3), int(5 * 1024**3))

    def test_outside_limits_size(self):
        policy = CachePolicy.from_gb(10.0, 2.0)
        assert not policy.is_within_limits(int(11 * 1024**3), int(5 * 1024**3))

    def test_outside_limits_free(self):
        policy = CachePolicy.from_gb(10.0, 2.0)
        assert not policy.is_within_limits(int(5 * 1024**3), int(1 * 1024**3))


class TestDeriveTitle:
    def test_strips_extension(self):
        assert derive_title("Final Fantasy X.iso") == "Final Fantasy X"

    def test_no_extension(self):
        assert derive_title("BCES00000") == "BCES00000"

    def test_multiple_dots(self):
        # pathlib.Path.stem only strips the last extension
        assert derive_title("game.rev01.iso") == "game.rev01"
