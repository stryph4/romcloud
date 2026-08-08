"""Unit tests for CatalogService."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from romcloud.core.services.catalog import CatalogService
from romcloud.core.exceptions import ProxyError


class TestCatalogServiceRefresh:
    def test_adds_new_games(self, catalog_service, game_repo):
        result = catalog_service.refresh()
        # ps2 has 2 games, nes has 1 game; snes .romcloud file should be skipped
        assert result.added == 3
        assert result.skipped == 0
        assert result.errors == []
        assert game_repo.count() == 3

    def test_skips_already_catalogued(self, catalog_service, game_repo):
        catalog_service.refresh()
        result = catalog_service.refresh()
        # All should be skipped on second run
        assert result.added == 0
        assert result.skipped == 3

    def test_creates_proxy_files(self, catalog_service, local_roms_dir):
        catalog_service.refresh()
        proxies = list(local_roms_dir.rglob("*.romcloud"))
        assert len(proxies) == 3

    def test_proxy_files_in_correct_dirs(self, catalog_service, local_roms_dir):
        catalog_service.refresh()
        ps2_proxies = list((local_roms_dir / "ps2").glob("*.romcloud"))
        nes_proxies = list((local_roms_dir / "nes").glob("*.romcloud"))
        assert len(ps2_proxies) == 2
        assert len(nes_proxies) == 1

    def test_proxy_file_valid_json(self, catalog_service, local_roms_dir):
        catalog_service.refresh()
        for proxy in local_roms_dir.rglob("*.romcloud"):
            data = json.loads(proxy.read_text())
            assert "game_id" in data
            assert "title" in data
            assert "system" in data
            assert "assets" in data

    def test_ignores_unknown_systems(self, catalog_service, rom_root):
        # Add a directory that is not a known Batocera system
        (rom_root / "unknown_system_xyz").mkdir()
        (rom_root / "unknown_system_xyz" / "some_game.bin").write_bytes(b"x")
        result = catalog_service.refresh()
        # unknown_system_xyz should not be catalogued
        assert result.added == 3

    def test_handles_unreachable_source_gracefully(
        self, provider, game_repo, proxy_repo, local_roms_dir
    ):
        svc = CatalogService(
            provider=provider,
            game_repo=game_repo,
            proxy_repo=proxy_repo,
            local_roms_root=str(local_roms_dir),
            source_root="/nonexistent/path",
        )
        result = svc.refresh()
        assert len(result.errors) > 0
        assert result.added == 0

    def test_cue_bin_grouping(self, provider, game_repo, proxy_repo, local_roms_dir, tmp_path):
        """A .cue file should be an entry point; its .bin track should be suppressed."""
        cue_root = tmp_path / "cue_roms"
        (cue_root / "psx").mkdir(parents=True)
        (cue_root / "psx" / "Game.cue").write_bytes(b"cue data")
        (cue_root / "psx" / "Game.bin").write_bytes(b"bin data" * 100)
        (cue_root / "psx" / "other.iso").write_bytes(b"other")

        svc = CatalogService(
            provider=provider,
            game_repo=game_repo,
            proxy_repo=proxy_repo,
            local_roms_root=str(local_roms_dir),
            source_root=str(cue_root),
        )
        result = svc.refresh()
        # Game.cue (1) + other.iso (1) = 2; Game.bin should be suppressed
        assert result.added == 2
        titles = [g.title for g in game_repo.find_by_system("psx")]
        assert "Game" in titles
        assert "other" in titles
        assert "Game" not in [t for t in titles if t == "Game" and ".bin" in t]


class TestCatalogServiceResolveProxy:
    def test_resolve_returns_game(self, catalog_service, local_roms_dir, game_repo):
        catalog_service.refresh()
        proxies = list(local_roms_dir.rglob("*.romcloud"))
        assert proxies

        game = catalog_service.resolve_proxy(str(proxies[0]))
        assert game is not None
        assert game.system in ("ps2", "nes")

    def test_resolve_nonexistent_file_raises(self, catalog_service):
        with pytest.raises(ProxyError):
            catalog_service.resolve_proxy("/nonexistent/game.romcloud")

    def test_resolve_wrong_extension_raises(self, catalog_service, tmp_path):
        f = tmp_path / "game.iso"
        f.write_bytes(b"data")
        with pytest.raises(ProxyError):
            catalog_service.resolve_proxy(str(f))

    def test_resolve_bad_json_raises(self, catalog_service, tmp_path):
        f = tmp_path / "bad.romcloud"
        f.write_text("not json at all")
        with pytest.raises(ProxyError):
            catalog_service.resolve_proxy(str(f))

    def test_resolve_missing_game_id_raises(self, catalog_service, tmp_path):
        f = tmp_path / "nogameid.romcloud"
        f.write_text(json.dumps({"title": "Test"}))
        with pytest.raises(ProxyError):
            catalog_service.resolve_proxy(str(f))

    def test_resolve_fallback_from_proxy_payload(
        self, provider, proxy_repo, local_roms_dir, rom_root, tmp_path, db
    ):
        """If game_id is not in DB, falls back to proxy file payload."""
        from romcloud.infrastructure.repositories.game import GameRepository

        # Use an empty game_repo so the DB lookup always fails
        empty_game_repo = GameRepository(db)

        svc = CatalogService(
            provider=provider,
            game_repo=empty_game_repo,
            proxy_repo=proxy_repo,
            local_roms_root=str(local_roms_dir),
            source_root=str(rom_root),
        )

        proxy_path = tmp_path / "fallback.romcloud"
        proxy_path.write_text(json.dumps({
            "romcloud_version": "1",
            "game_id": "00000000-0000-0000-0000-000000000001",
            "title": "Fallback Game",
            "system": "ps2",
            "source_provider": "local",
            "source_root": str(rom_root),
            "assets": [
                {
                    "filename": "Fallback Game.iso",
                    "relative_path": "ps2/Fallback Game.iso",
                    "is_primary": True,
                }
            ],
        }))

        game = svc.resolve_proxy(str(proxy_path))
        assert game.title == "Fallback Game"
        assert game.system == "ps2"
