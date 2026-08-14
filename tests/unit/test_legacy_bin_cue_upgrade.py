"""Regression coverage for the legacy pre-0.8.0 → 0.8.0+ BIN/CUE upgrade bug.

Root cause
----------
Pre-0.8.0, a .cue file was catalogued as a single-asset Game (no companion
tracks at all — cue-dependency parsing didn't exist). If that game was
cached before upgrading, its CacheEntry legitimately reflects a "complete"
single-asset cache.

`CacheService.is_cached()` (added in 0.8.0) does check every asset in
`Game.assets` for completeness — but it can only check what the *catalog*
currently believes the game's assets are. If nothing has re-derived the
cue's companion tracks into that Game's asset list yet, the catalog itself
still only knows about the one legacy asset, so the "multi-asset" check
degrades back to checking a single asset — a false cache-hit — even though
`refresh()` already knows how to fix this once it runs.

The real-world trigger: a user upgrades ROMCloud and launches the game
immediately, without having run `romcloud refresh` first. `resolve_proxy()`
now self-heals this by re-deriving a `.cue`'s companion assets from the
*current* source every time a proxy is resolved (see
`CatalogService._reconcile_cue_assets`), independent of whether/when
`refresh()` last ran.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from romcloud.core.models.game import Game, GameAsset
from romcloud.core.models.proxy import ProxyRecord
from romcloud.integrations.batocera.catalog import CatalogService
from tests.system_registry_fixture import TEST_SYSTEM_REGISTRY


CUE_TEXT = 'FILE "Game (Track 1).bin" BINARY\nFILE "Game (Track 2).bin" BINARY\n'


@pytest.fixture
def legacy_cue_source(tmp_path):
    """Real-world layout: track filenames do NOT share the cue's stem, so
    pre-0.8.0's old same-stem suppression heuristic would NOT have caught
    them — they'd have been catalogued as independent single-file games."""
    root = tmp_path / "roms"
    (root / "psx").mkdir(parents=True)
    (root / "psx" / "Game.cue").write_text(CUE_TEXT)
    (root / "psx" / "Game (Track 1).bin").write_bytes(b"t1" * 500)
    (root / "psx" / "Game (Track 2).bin").write_bytes(b"t2" * 700)
    return root


def _seed_legacy_single_asset_game(game_repo, root) -> Game:
    """Simulate the exact pre-0.8.0 catalog row for the .cue: one asset,
    itself, is_primary=True — no companions known at all."""
    asset = GameAsset(
        "Game.cue", "psx/Game.cue", size_bytes=len(CUE_TEXT.encode()), is_primary=True
    )
    game = Game.create("psx", "Game", "local", str(root), [asset])
    game_repo.save(game)
    return game


def _write_proxy_file(proxy_repo, local_roms_dir, game: Game) -> Path:
    (local_roms_dir / game.system).mkdir(parents=True, exist_ok=True)
    proxy_path = local_roms_dir / game.system / f"{game.title}.romcloud"
    proxy_path.write_text(
        json.dumps(
            {
                "romcloud_version": "1",
                "game_id": game.id,
                "title": game.title,
                "system": game.system,
                "source_provider": game.source_provider,
                "source_root": game.source_root,
                "assets": [
                    {"filename": a.filename, "relative_path": a.relative_path, "is_primary": a.is_primary}
                    for a in game.assets
                ],
            }
        )
    )
    proxy_repo.save(ProxyRecord.create(game_id=game.id, proxy_path=str(proxy_path)))
    return proxy_path


class TestLegacySingleAssetCacheIsResolvedOnDemand:
    """Phase B resolves legacy descriptor-only catalog rows at cache time."""

    def test_cache_request_resolves_dependencies_before_refresh(
        self, provider, game_repo, proxy_repo, local_roms_dir, cache_service, legacy_cue_source
    ):
        root = legacy_cue_source
        legacy_game = _seed_legacy_single_asset_game(game_repo, root)

        # Step 2: pre-0.8.0 behaviour — only the primary (.cue) gets cached.
        cache_service.cache_game(legacy_game.id)
        assert cache_service.is_cached(legacy_game.id) is True
        assert (Path(cache_service._cache_root) / "psx" / "Game (Track 1).bin").exists()
        assert (Path(cache_service._cache_root) / "psx" / "Game (Track 2).bin").exists()

        # Step 3: upgrade + refresh.
        catalog_svc = CatalogService(
            provider=provider,
            game_repo=game_repo,
            proxy_repo=proxy_repo,
            local_roms_root=str(local_roms_dir),
            source_root=str(root),
            system_registry=TEST_SYSTEM_REGISTRY,
        )
        result = catalog_svc.refresh()
        assert result.errors == []

        updated_game = game_repo.get(legacy_game.id)
        assert len(updated_game.assets) == 3  # cue + 2 tracks now known

        # Refresh may enrich catalog presentation, but the cache-time ownership
        # snapshot remains complete and authoritative.
        assert cache_service.is_cached(legacy_game.id) is True


class TestLegacyLaunchWithoutRefreshSelfHeals:
    """The actual real-hardware trigger: the user launches immediately after
    upgrading, without ever running `romcloud refresh`."""

    def test_resolve_proxy_alone_detects_incompleteness(
        self, provider, game_repo, proxy_repo, local_roms_dir, cache_service, legacy_cue_source
    ):
        root = legacy_cue_source
        legacy_game = _seed_legacy_single_asset_game(game_repo, root)
        proxy_path = _write_proxy_file(proxy_repo, local_roms_dir, legacy_game)

        cache_service.cache_game(legacy_game.id)
        assert cache_service.is_cached(legacy_game.id) is True

        catalog_svc = CatalogService(
            provider=provider,
            game_repo=game_repo,
            proxy_repo=proxy_repo,
            local_roms_root=str(local_roms_dir),
            source_root=str(root),
        )

        # No refresh() call anywhere — resolve_proxy is the only thing run,
        # exactly like the romcloud-run launch wrapper does.
        resolved = catalog_svc.resolve_proxy(str(proxy_path))

        assert len(resolved.assets) == 3
        companions = sorted(a.relative_path for a in resolved.assets if not a.is_primary)
        assert companions == ["psx/Game (Track 1).bin", "psx/Game (Track 2).bin"]

        # The DB row itself must be corrected too (same id — history preserved).
        assert resolved.id == legacy_game.id
        assert len(game_repo.get(legacy_game.id).assets) == 3

        # The cache-time dependency closure was already complete.
        assert cache_service.is_cached(resolved.id) is True

    def test_repair_fetches_only_missing_companions_and_launches_cue(
        self, provider, game_repo, proxy_repo, local_roms_dir, cache_service, legacy_cue_source
    ):
        root = legacy_cue_source
        legacy_game = _seed_legacy_single_asset_game(game_repo, root)
        proxy_path = _write_proxy_file(proxy_repo, local_roms_dir, legacy_game)
        cache_service.cache_game(legacy_game.id)

        catalog_svc = CatalogService(
            provider=provider,
            game_repo=game_repo,
            proxy_repo=proxy_repo,
            local_roms_root=str(local_roms_dir),
            source_root=str(root),
        )
        resolved = catalog_svc.resolve_proxy(str(proxy_path))
        cache_root = Path(cache_service._cache_root)
        for track in ("Game (Track 1).bin", "Game (Track 2).bin"):
            (cache_root / "psx" / track).unlink()
        assert cache_service.is_cached(resolved.id) is False

        # The already-cached .cue must be left alone by the repair (prove it
        # by corrupting the source and confirming the cached copy is unchanged).
        original_cue_bytes = (cache_root / "psx" / "Game.cue").read_bytes()
        (root / "psx" / "Game.cue").write_bytes(b"SHOULD NOT BE RE-COPIED")

        progress_events: list[tuple[int, int]] = []
        launch_path = cache_service.cache_game(
            resolved.id, on_progress=lambda d, t: progress_events.append((d, t))
        )

        # Step 6: emulatorlauncher would receive the cached .cue.
        assert Path(launch_path).name == "Game.cue"
        assert Path(launch_path).read_bytes() == original_cue_bytes

        # Step 5: only the missing companions were actually fetched.
        for track, content in (
            ("Game (Track 1).bin", b"t1" * 500),
            ("Game (Track 2).bin", b"t2" * 700),
        ):
            cached = cache_root / "psx" / track
            assert cached.exists()
            assert cached.read_bytes() == content

        # Aggregate progress reported across the whole repair, not per track.
        assert progress_events
        assert progress_events[-1][0] == progress_events[-1][1]

        # Step 6: now a genuine, complete cache hit.
        assert cache_service.is_cached(resolved.id) is True

        # Step 7: recorded size accounting covers every asset, not just the cue.
        entry = cache_service.get_entry(resolved.id)
        expected_total = sum(a.size_bytes for a in game_repo.get(resolved.id).assets)
        assert entry.size_bytes == expected_total

        # Subsequent launch is a true, fast cache hit — no transfer call at all.
        calls = []
        cache_service._transfer.transfer = lambda *a, **k: calls.append(1)
        second_path = cache_service.cache_game(resolved.id)
        assert not calls
        assert second_path == launch_path

    def test_lru_pinning_preserved_across_reconciliation(
        self, provider, game_repo, proxy_repo, local_roms_dir, cache_service, legacy_cue_source
    ):
        root = legacy_cue_source
        legacy_game = _seed_legacy_single_asset_game(game_repo, root)
        proxy_path = _write_proxy_file(proxy_repo, local_roms_dir, legacy_game)
        cache_service.cache_game(legacy_game.id)
        cache_service.pin(legacy_game.id)

        catalog_svc = CatalogService(
            provider=provider,
            game_repo=game_repo,
            proxy_repo=proxy_repo,
            local_roms_root=str(local_roms_dir),
            source_root=str(root),
        )
        resolved = catalog_svc.resolve_proxy(str(proxy_path))

        # Same game id → pin state (and cache history) untouched by reconciliation.
        assert resolved.id == legacy_game.id
        entry = cache_service.get_entry(legacy_game.id)
        assert entry.is_pinned is True

        cache_service.cache_game(resolved.id)  # repair
        assert cache_service.get_entry(legacy_game.id).is_pinned is True


class TestReconciliationDoesNotBlockOfflineCacheHits:
    """"ROMCloud may fail; Batocera must not" — an already-fully-migrated,
    fully-cached cue game must still launch as a fast cache hit even if the
    source becomes unreachable later (reconciliation must degrade
    gracefully, never raise out of `resolve_proxy`)."""

    def test_unreachable_source_does_not_block_an_already_complete_cache_hit(
        self, provider, game_repo, proxy_repo, local_roms_dir, cache_service, legacy_cue_source
    ):
        root = legacy_cue_source
        legacy_game = _seed_legacy_single_asset_game(game_repo, root)
        proxy_path = _write_proxy_file(proxy_repo, local_roms_dir, legacy_game)

        catalog_svc = CatalogService(
            provider=provider,
            game_repo=game_repo,
            proxy_repo=proxy_repo,
            local_roms_root=str(local_roms_dir),
            source_root=str(root),
        )
        # Fully migrate + fully cache first (source still reachable).
        resolved = catalog_svc.resolve_proxy(str(proxy_path))
        cache_service.cache_game(resolved.id)
        assert cache_service.is_cached(resolved.id) is True

        # Now the source disappears entirely (NAS offline, share unmounted, ...).
        import shutil

        shutil.rmtree(root)

        # resolve_proxy must not raise even though the cue can no longer be read.
        resolved_again = catalog_svc.resolve_proxy(str(proxy_path))
        assert len(resolved_again.assets) == 3

        # Cache-hit fast path must still work with zero source access.
        assert cache_service.is_cached(resolved_again.id) is True


class TestStaleIndependentTrackDoesNotInterfere:
    """A .bin that was ALSO independently catalogued (and possibly cached)
    pre-0.8.0 must not collide with, or corrupt, the new cue logical game."""

    def test_stale_independent_track_pruned_without_deleting_its_cached_bytes(
        self, provider, game_repo, proxy_repo, local_roms_dir, cache_service, legacy_cue_source
    ):
        root = legacy_cue_source
        legacy_cue_game = _seed_legacy_single_asset_game(game_repo, root)
        cache_service.cache_game(legacy_cue_game.id)

        # Reproduce an old descriptor-only cache. Track 1 is restored below by
        # the independently owned legacy game; Track 2 remains absent.
        cache_root = Path(cache_service._cache_root)
        (cache_root / "psx" / "Game (Track 1).bin").unlink()
        (cache_root / "psx" / "Game (Track 2).bin").unlink()

        # A second, independently-catalogued (and cached) legacy game for one track.
        track_asset = GameAsset(
            "Game (Track 1).bin", "psx/Game (Track 1).bin", size_bytes=1000, is_primary=True
        )
        track_game = Game.create("psx", "Game (Track 1)", "local", str(root), [track_asset])
        game_repo.save(track_game)
        _write_proxy_file(proxy_repo, local_roms_dir, track_game)
        cache_service.cache_game(track_game.id)
        assert cache_service.is_cached(track_game.id) is True

        catalog_svc = CatalogService(
            provider=provider,
            game_repo=game_repo,
            proxy_repo=proxy_repo,
            local_roms_root=str(local_roms_dir),
            source_root=str(root),
            system_registry=TEST_SYSTEM_REGISTRY,
        )
        result = catalog_svc.refresh()

        # Stale independent track game/proxy is gone from the catalog...
        assert game_repo.get(track_game.id) is None
        assert result.removed == 1

        # ...but its already-downloaded bytes are not deleted from disk —
        # they get picked up as "already satisfied" for the cue's companion.
        track_path = Path(cache_service._cache_root) / "psx" / "Game (Track 1).bin"
        assert track_path.exists()
        assert track_path.read_bytes() == (root / "psx" / "Game (Track 1).bin").read_bytes()

        cue_game = game_repo.get(legacy_cue_game.id)
        assert len(cue_game.assets) == 3

        # Repair should only need to fetch Track 2 — Track 1 already present.
        calls = []
        real_transfer_to = provider.transfer_to

        def spying_transfer_to(source_path, dest_path, on_progress=None):
            calls.append(source_path)
            return real_transfer_to(source_path, dest_path, on_progress)

        provider.transfer_to = spying_transfer_to
        cache_service.cache_game(legacy_cue_game.id)

        assert not any("Track 1" in c for c in calls)
        assert any("Track 2" in c for c in calls)
        assert cache_service.is_cached(legacy_cue_game.id) is True

    def test_no_unrelated_rom_files_deleted_during_migration(
        self, provider, game_repo, proxy_repo, local_roms_dir, cache_service, legacy_cue_source
    ):
        root = legacy_cue_source
        legacy_cue_game = _seed_legacy_single_asset_game(game_repo, root)
        cache_service.cache_game(legacy_cue_game.id)

        (root / "psx" / "Unrelated.iso").write_bytes(b"unrelated" * 10)

        catalog_svc = CatalogService(
            provider=provider,
            game_repo=game_repo,
            proxy_repo=proxy_repo,
            local_roms_root=str(local_roms_dir),
            source_root=str(root),
            system_registry=TEST_SYSTEM_REGISTRY,
        )
        catalog_svc.refresh()

        # Source ROM files must never be touched by migration/reconciliation.
        assert (root / "psx" / "Game.cue").exists()
        assert (root / "psx" / "Game (Track 1).bin").exists()
        assert (root / "psx" / "Game (Track 2).bin").exists()
        assert (root / "psx" / "Unrelated.iso").exists()
        titles = [g.title for g in game_repo.find_by_system("psx")]
        assert "Unrelated" in titles
