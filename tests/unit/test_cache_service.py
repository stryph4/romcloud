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


class TestCacheCollisionSafety:
    """The cache path invariant: cache_root/system/<relative path> must never collide."""

    def test_identical_filenames_across_two_systems_do_not_collide(
        self, cache_service, game_repo, tmp_path
    ):
        source_root = tmp_path / "source"
        (source_root / "ps2").mkdir(parents=True)
        (source_root / "snes").mkdir(parents=True)
        (source_root / "ps2" / "Game.rom").write_bytes(b"ps2_content" * 20)
        (source_root / "snes" / "Game.rom").write_bytes(b"snes_content" * 20)

        ps2_asset = GameAsset(
            "Game.rom",
            "ps2/Game.rom",
            size_bytes=(source_root / "ps2" / "Game.rom").stat().st_size,
            is_primary=True,
        )
        snes_asset = GameAsset(
            "Game.rom",
            "snes/Game.rom",
            size_bytes=(source_root / "snes" / "Game.rom").stat().st_size,
            is_primary=True,
        )
        ps2_game = Game.create("ps2", "Game", "local", str(source_root), [ps2_asset])
        snes_game = Game.create("snes", "Game", "local", str(source_root), [snes_asset])
        game_repo.save(ps2_game)
        game_repo.save(snes_game)

        ps2_path = cache_service.cache_game(ps2_game.id)
        snes_path = cache_service.cache_game(snes_game.id)

        assert ps2_path != snes_path
        assert Path(ps2_path).read_bytes() == b"ps2_content" * 20
        assert Path(snes_path).read_bytes() == b"snes_content" * 20
        assert cache_service.is_cached(ps2_game.id)
        assert cache_service.is_cached(snes_game.id)

    def test_identical_filenames_across_two_subdirectories_do_not_collide(
        self, cache_service, game_repo, tmp_path
    ):
        source_root = tmp_path / "source"
        (source_root / "ps2" / "discs" / "1").mkdir(parents=True)
        (source_root / "ps2" / "discs" / "2").mkdir(parents=True)
        (source_root / "ps2" / "discs" / "1" / "Game.iso").write_bytes(b"disc_one" * 20)
        (source_root / "ps2" / "discs" / "2" / "Game.iso").write_bytes(b"disc_two" * 20)

        disc1_asset = GameAsset(
            "Game.iso",
            "ps2/discs/1/Game.iso",
            size_bytes=(source_root / "ps2" / "discs" / "1" / "Game.iso").stat().st_size,
            is_primary=True,
        )
        disc2_asset = GameAsset(
            "Game.iso",
            "ps2/discs/2/Game.iso",
            size_bytes=(source_root / "ps2" / "discs" / "2" / "Game.iso").stat().st_size,
            is_primary=True,
        )
        disc1_game = Game.create("ps2", "Disc 1", "local", str(source_root), [disc1_asset])
        disc2_game = Game.create("ps2", "Disc 2", "local", str(source_root), [disc2_asset])
        game_repo.save(disc1_game)
        game_repo.save(disc2_game)

        disc1_path = cache_service.cache_game(disc1_game.id)
        disc2_path = cache_service.cache_game(disc2_game.id)

        assert disc1_path != disc2_path
        assert Path(disc1_path).read_bytes() == b"disc_one" * 20
        assert Path(disc2_path).read_bytes() == b"disc_two" * 20


class TestRemoveAndEvictionSiblingSafety:
    """Regression coverage: `CacheEntry.cache_path` is now the exact cached
    asset *file* under ``<cache_root>/<system>/<relative_path>`` — remove and
    eviction must delete only that file, never sibling games in the same
    system directory.
    """

    @staticmethod
    def _make_sibling_games(source_root, game_repo):
        (source_root / "ps2").mkdir(parents=True)
        (source_root / "ps2" / "Game A.iso").write_bytes(b"a" * 500)
        (source_root / "ps2" / "Game B.iso").write_bytes(b"b" * 500)

        asset_a = GameAsset("Game A.iso", "ps2/Game A.iso", size_bytes=500, is_primary=True)
        asset_b = GameAsset("Game B.iso", "ps2/Game B.iso", size_bytes=500, is_primary=True)
        game_a = Game.create("ps2", "Game A", "local", str(source_root), [asset_a])
        game_b = Game.create("ps2", "Game B", "local", str(source_root), [asset_b])
        game_repo.save(game_a)
        game_repo.save(game_b)
        return game_a, game_b

    def test_remove_deletes_only_target_file_and_preserves_siblings(
        self, cache_service, cache_repo, game_repo, tmp_path
    ):
        source_root = tmp_path / "source"
        game_a, game_b = self._make_sibling_games(source_root, game_repo)

        path_a = cache_service.cache_game(game_a.id)
        path_b = cache_service.cache_game(game_b.id)

        # Sanity: both live directly under the same system directory.
        assert Path(path_a).parent == Path(path_b).parent
        assert Path(path_a).parent.name == "ps2"

        cache_service.remove(game_a.id)

        assert not Path(path_a).exists(), "removed game's file must be gone"
        assert Path(path_b).exists(), "sibling game's file must be untouched"
        assert Path(path_b).read_bytes() == b"b" * 500
        assert cache_repo.get(game_a.id) is None
        assert cache_service.is_cached(game_b.id)
        # The shared system directory itself must not be removed.
        assert Path(path_a).parent.exists()

    def test_evict_removes_only_lru_target_file_and_preserves_siblings(
        self, cache_service, cache_repo, game_repo, tmp_path
    ):
        from datetime import datetime, timedelta, timezone

        from romcloud.core.models.cache import CachePolicy

        source_root = tmp_path / "source"
        game_a, game_b = self._make_sibling_games(source_root, game_repo)

        path_a = cache_service.cache_game(game_a.id)
        path_b = cache_service.cache_game(game_b.id)

        # game_a is the oldest-accessed → the LRU eviction candidate.
        old_time = datetime.now(timezone.utc) - timedelta(days=30)
        cache_repo.update_last_accessed(game_a.id, old_time)

        # Quota only has room for one 500-byte entry, forcing exactly one eviction.
        cache_service._policy = CachePolicy(max_size_bytes=500, min_free_bytes=0)
        evicted = cache_service.evict()

        assert evicted == [game_a.id]
        assert not Path(path_a).exists(), "evicted game's file must be gone"
        assert Path(path_b).exists(), "non-evicted sibling's file must be untouched"
        assert Path(path_b).read_bytes() == b"b" * 500
        assert not cache_service.is_cached(game_a.id)
        assert cache_service.is_cached(game_b.id)
        # The shared system directory itself must not be removed.
        assert Path(path_a).parent.exists()


class TestMultiAssetCueBinCache:
    """BIN/CUE (multi-asset) cache-hit completeness, repair, and eviction
    coherence."""

    @staticmethod
    def _make_cue_game(tmp_path, game_repo, num_tracks=2):
        source_root = tmp_path / "source"
        (source_root / "psx").mkdir(parents=True)
        (source_root / "psx" / "Game.cue").write_bytes(b"cue_data")

        assets = [GameAsset("Game.cue", "psx/Game.cue", size_bytes=8, is_primary=True)]
        for i in range(1, num_tracks + 1):
            name = f"Track {i}.bin"
            content = bytes([i]) * 100
            (source_root / "psx" / name).write_bytes(content)
            assets.append(GameAsset(name, f"psx/{name}", size_bytes=len(content), is_primary=False))

        game = Game.create("psx", "Game", "local", str(source_root), assets)
        game_repo.save(game)
        return game, source_root

    def test_cache_game_caches_every_required_asset(self, cache_service, game_repo, tmp_path):
        game, source_root = self._make_cue_game(tmp_path, game_repo)
        launch_path = cache_service.cache_game(game.id)
        assert Path(launch_path).name == "Game.cue"
        for asset in game.assets:
            cached = Path(cache_service._cache_root) / "psx" / asset.filename
            assert cached.exists()

    def test_full_cache_hit_requires_every_companion_present(
        self, cache_service, game_repo, tmp_path
    ):
        game, source_root = self._make_cue_game(tmp_path, game_repo)
        cache_service.cache_game(game.id)
        assert cache_service.is_cached(game.id) is True

        # Simulate a companion track disappearing (disk issue, manual delete, etc).
        (Path(cache_service._cache_root) / "psx" / "Track 1.bin").unlink()

        assert cache_service.is_cached(game.id) is False

    def test_incomplete_set_not_treated_as_hit_even_if_launch_asset_present(
        self, cache_service, game_repo, tmp_path
    ):
        game, source_root = self._make_cue_game(tmp_path, game_repo, num_tracks=3)
        cache_service.cache_game(game.id)
        (Path(cache_service._cache_root) / "psx" / "Track 3.bin").unlink()

        assert cache_service.is_cached(game.id) is False
        # The .cue (launch asset) is still there, but that alone is not a hit.
        assert (Path(cache_service._cache_root) / "psx" / "Game.cue").exists()

    def test_repair_only_downloads_missing_companion(
        self, cache_service, game_repo, tmp_path
    ):
        game, source_root = self._make_cue_game(tmp_path, game_repo, num_tracks=2)
        cache_service.cache_game(game.id)

        cache_root = Path(cache_service._cache_root)
        (cache_root / "psx" / "Track 2.bin").unlink()
        # Corrupt the source .cue so we could detect an unwanted re-copy.
        (source_root / "psx" / "Game.cue").write_bytes(b"CORRUPTED")

        assert cache_service.is_cached(game.id) is False
        launch_path = cache_service.cache_game(game.id)

        assert Path(launch_path).read_bytes() != b"CORRUPTED"
        assert (cache_root / "psx" / "Track 2.bin").exists()
        assert cache_service.is_cached(game.id) is True

    def test_full_cache_hit_is_fast_no_transfer_call(
        self, cache_service, game_repo, tmp_path, monkeypatch
    ):
        game, source_root = self._make_cue_game(tmp_path, game_repo)
        cache_service.cache_game(game.id)

        called = []
        monkeypatch.setattr(
            cache_service._transfer, "transfer", lambda *a, **k: called.append(1)
        )
        path = cache_service.cache_game(game.id)
        assert not called
        assert Path(path).name == "Game.cue"

    def test_remove_deletes_all_companion_assets(self, cache_service, game_repo, tmp_path):
        game, source_root = self._make_cue_game(tmp_path, game_repo, num_tracks=2)
        cache_service.cache_game(game.id)
        cache_root = Path(cache_service._cache_root)

        cache_service.remove(game.id)

        for asset in game.assets:
            assert not (cache_root / "psx" / asset.filename).exists()

    def test_eviction_removes_all_companion_assets_together(
        self, cache_service, cache_repo, game_repo, tmp_path
    ):
        from datetime import datetime, timedelta, timezone

        game, source_root = self._make_cue_game(tmp_path, game_repo, num_tracks=2)
        cache_service.cache_game(game.id)
        cache_root = Path(cache_service._cache_root)

        old_time = datetime.now(timezone.utc) - timedelta(days=30)
        cache_repo.update_last_accessed(game.id, old_time)
        cache_service._policy = CachePolicy(max_size_bytes=1, min_free_bytes=0)

        evicted = cache_service.evict()

        assert game.id in evicted
        for asset in game.assets:
            assert not (cache_root / "psx" / asset.filename).exists()

    def test_active_launch_protects_whole_asset_set_from_eviction(
        self, cache_service, cache_repo, game_repo, tmp_path
    ):
        game, source_root = self._make_cue_game(tmp_path, game_repo, num_tracks=2)
        cache_service.cache_game(game.id)
        cache_service.mark_launched(game.id)

        cache_service._policy = CachePolicy(max_size_bytes=1, min_free_bytes=0)
        evicted = cache_service.evict()

        assert game.id not in evicted
        assert cache_service.is_cached(game.id)

    def test_recorded_size_covers_every_asset_not_just_launch_asset(
        self, cache_service, cache_repo, game_repo, tmp_path
    ):
        """Cache-entry size accounting must cover the whole logical game
        (.cue + every track), never just the primary/launch asset — an
        undercount would silently break quota/LRU eviction."""
        game, source_root = self._make_cue_game(tmp_path, game_repo, num_tracks=2)
        cache_service.cache_game(game.id)

        entry = cache_repo.get(game.id)
        expected_total = sum(a.size_bytes for a in game.assets)
        assert entry.size_bytes == expected_total
        assert cache_repo.total_size() == expected_total
