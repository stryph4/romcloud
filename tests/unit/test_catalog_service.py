"""Unit tests for CatalogService."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from romcloud.integrations.batocera.catalog import CatalogService
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
        (cue_root / "psx" / "Game.cue").write_text('FILE "Game.bin" BINARY\n')
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
        games = game_repo.find_by_system("psx")
        titles = [g.title for g in games]
        assert "Game" in titles
        assert "other" in titles
        assert "Game" not in [t for t in titles if t == "Game" and ".bin" in t]

        cue_game = next(g for g in games if g.title == "Game")
        assert cue_game.primary_asset.relative_path == "psx/Game.cue"
        companion_paths = [a.relative_path for a in cue_game.assets if not a.is_primary]
        assert companion_paths == ["psx/Game.bin"]


def _make_catalog_service(provider, game_repo, proxy_repo, local_roms_dir, source_root):
    return CatalogService(
        provider=provider,
        game_repo=game_repo,
        proxy_repo=proxy_repo,
        local_roms_root=str(local_roms_dir),
        source_root=str(source_root),
    )


class TestCueBinCatalogDiscovery:
    """Comprehensive BIN/CUE multi-file disc set discovery coverage."""

    def test_cue_with_three_bins_is_one_logical_game(
        self, provider, game_repo, proxy_repo, local_roms_dir, tmp_path
    ):
        root = tmp_path / "roms"
        (root / "psx").mkdir(parents=True)
        (root / "psx" / "Game.cue").write_text(
            'FILE "Game (Track 1).bin" BINARY\n'
            'FILE "Game (Track 2).bin" BINARY\n'
            'FILE "Game (Track 3).bin" BINARY\n'
        )
        for i in (1, 2, 3):
            (root / "psx" / f"Game (Track {i}).bin").write_bytes(bytes([i]) * 100)

        svc = _make_catalog_service(provider, game_repo, proxy_repo, local_roms_dir, root)
        result = svc.refresh()

        assert result.added == 1
        games = game_repo.find_by_system("psx")
        assert len(games) == 1
        game = games[0]
        assert game.primary_asset.relative_path == "psx/Game.cue"
        companion_paths = sorted(a.relative_path for a in game.assets if not a.is_primary)
        assert companion_paths == [
            "psx/Game (Track 1).bin",
            "psx/Game (Track 2).bin",
            "psx/Game (Track 3).bin",
        ]

    def test_referenced_bins_excluded_as_independent_entries(
        self, provider, game_repo, proxy_repo, local_roms_dir, tmp_path
    ):
        root = tmp_path / "roms"
        (root / "psx").mkdir(parents=True)
        (root / "psx" / "Game.cue").write_text('FILE "Game.bin" BINARY\n')
        (root / "psx" / "Game.bin").write_bytes(b"x" * 50)

        svc = _make_catalog_service(provider, game_repo, proxy_repo, local_roms_dir, root)
        svc.refresh()

        games = game_repo.find_by_system("psx")
        # Only the cue's logical game — no independent "Game.bin" entry.
        assert len(games) == 1
        assert all(g.primary_asset.filename != "Game.bin" for g in games)

    def test_unrelated_standalone_bin_remains_discoverable(
        self, provider, game_repo, proxy_repo, local_roms_dir, tmp_path
    ):
        root = tmp_path / "roms"
        (root / "psx").mkdir(parents=True)
        (root / "psx" / "Game.cue").write_text('FILE "Game.bin" BINARY\n')
        (root / "psx" / "Game.bin").write_bytes(b"x" * 50)
        (root / "psx" / "Unrelated.bin").write_bytes(b"y" * 50)

        svc = _make_catalog_service(provider, game_repo, proxy_repo, local_roms_dir, root)
        result = svc.refresh()

        assert result.added == 2  # Game.cue (with companion) + Unrelated.bin
        titles = [g.title for g in game_repo.find_by_system("psx")]
        assert "Unrelated" in titles

    def test_multiple_cue_sets_in_same_directory(
        self, provider, game_repo, proxy_repo, local_roms_dir, tmp_path
    ):
        root = tmp_path / "roms"
        (root / "psx").mkdir(parents=True)
        (root / "psx" / "Game A.cue").write_text('FILE "Game A.bin" BINARY\n')
        (root / "psx" / "Game A.bin").write_bytes(b"a" * 50)
        (root / "psx" / "Game B.cue").write_text('FILE "Game B.bin" BINARY\n')
        (root / "psx" / "Game B.bin").write_bytes(b"b" * 50)

        svc = _make_catalog_service(provider, game_repo, proxy_repo, local_roms_dir, root)
        result = svc.refresh()

        assert result.added == 2
        games = game_repo.find_by_system("psx")
        titles = sorted(g.title for g in games)
        assert titles == ["Game A", "Game B"]
        for g in games:
            companions = [a.relative_path for a in g.assets if not a.is_primary]
            assert len(companions) == 1

    def test_shared_track_file_referenced_by_two_cue_sets(
        self, provider, game_repo, proxy_repo, local_roms_dir, tmp_path
    ):
        """Two cue sets may legally reference the same shared companion file."""
        root = tmp_path / "roms"
        (root / "psx").mkdir(parents=True)
        (root / "psx" / "Shared.bin").write_bytes(b"s" * 50)
        (root / "psx" / "Disc 1.cue").write_text('FILE "Shared.bin" BINARY\n')
        (root / "psx" / "Disc 2.cue").write_text('FILE "Shared.bin" BINARY\n')

        svc = _make_catalog_service(provider, game_repo, proxy_repo, local_roms_dir, root)
        result = svc.refresh()

        assert result.added == 2
        games = {g.title: g for g in game_repo.find_by_system("psx")}
        assert set(games) == {"Disc 1", "Disc 2"}
        for g in games.values():
            companions = [a.relative_path for a in g.assets if not a.is_primary]
            assert companions == ["psx/Shared.bin"]

    def test_cue_bin_no_matching_stem(
        self, provider, game_repo, proxy_repo, local_roms_dir, tmp_path
    ):
        """Cue and bin filenames need not share the same stem — dependency
        comes purely from the FILE reference, never filename-guessing."""
        root = tmp_path / "roms"
        (root / "psx").mkdir(parents=True)
        (root / "psx" / "MyGame.cue").write_text('FILE "actual_track_data.bin" BINARY\n')
        (root / "psx" / "actual_track_data.bin").write_bytes(b"z" * 50)

        svc = _make_catalog_service(provider, game_repo, proxy_repo, local_roms_dir, root)
        result = svc.refresh()

        assert result.added == 1
        games = game_repo.find_by_system("psx")
        assert len(games) == 1
        companions = [a.relative_path for a in games[0].assets if not a.is_primary]
        assert companions == ["psx/actual_track_data.bin"]

    def test_uppercase_cue_and_bin_extensions(
        self, provider, game_repo, proxy_repo, local_roms_dir, tmp_path
    ):
        root = tmp_path / "roms"
        (root / "psx").mkdir(parents=True)
        (root / "psx" / "GAME.CUE").write_text('FILE "GAME.BIN" BINARY\n')
        (root / "psx" / "GAME.BIN").write_bytes(b"x" * 50)

        svc = _make_catalog_service(provider, game_repo, proxy_repo, local_roms_dir, root)
        result = svc.refresh()

        assert result.added == 1
        games = game_repo.find_by_system("psx")
        assert len(games) == 1
        assert games[0].primary_asset.relative_path == "psx/GAME.CUE"
        companions = [a.relative_path for a in games[0].assets if not a.is_primary]
        assert companions == ["psx/GAME.BIN"]

    def test_malformed_cue_falls_back_to_plain_file(
        self, provider, game_repo, proxy_repo, local_roms_dir, tmp_path
    ):
        root = tmp_path / "roms"
        (root / "psx").mkdir(parents=True)
        (root / "psx" / "Broken.cue").write_bytes(b"\x00\x01totally not a cue")

        svc = _make_catalog_service(provider, game_repo, proxy_repo, local_roms_dir, root)
        result = svc.refresh()

        # Still catalogued (as a plain single-file game) — never dropped.
        assert result.added == 1
        games = game_repo.find_by_system("psx")
        assert len(games) == 1
        assert games[0].primary_asset.filename == "Broken.cue"

    def test_missing_referenced_file_still_catalogued_with_warning(
        self, provider, game_repo, proxy_repo, local_roms_dir, tmp_path
    ):
        root = tmp_path / "roms"
        (root / "psx").mkdir(parents=True)
        (root / "psx" / "Game.cue").write_text('FILE "Missing.bin" BINARY\n')
        # Missing.bin intentionally not created.

        svc = _make_catalog_service(provider, game_repo, proxy_repo, local_roms_dir, root)
        result = svc.refresh()

        assert result.added == 1
        games = game_repo.find_by_system("psx")
        companions = [a for a in games[0].assets if not a.is_primary]
        assert len(companions) == 1
        assert companions[0].relative_path == "psx/Missing.bin"
        assert companions[0].size_bytes is None
        assert any("missing" in w for w in result.warnings)

    def test_traversal_attempt_rejected_with_warning(
        self, provider, game_repo, proxy_repo, local_roms_dir, tmp_path
    ):
        root = tmp_path / "roms"
        (root / "psx").mkdir(parents=True)
        (root / "psx" / "Game.cue").write_text('FILE "../../../etc/passwd" BINARY\n')

        svc = _make_catalog_service(provider, game_repo, proxy_repo, local_roms_dir, root)
        result = svc.refresh()

        assert result.added == 1
        games = game_repo.find_by_system("psx")
        companions = [a for a in games[0].assets if not a.is_primary]
        assert companions == []  # traversal reference never became an asset
        assert any("rejected" in w or "traversal" in w for w in result.warnings)

    def test_directory_scoped_cue_set(
        self, provider, game_repo, proxy_repo, local_roms_dir, tmp_path
    ):
        """psx/Game A/Game A.cue + tracks in its own subdirectory."""
        root = tmp_path / "roms"
        (root / "psx" / "Game A").mkdir(parents=True)
        (root / "psx" / "Game A" / "Game A.cue").write_text(
            'FILE "Track 01.bin" BINARY\nFILE "Track 02.bin" BINARY\n'
        )
        (root / "psx" / "Game A" / "Track 01.bin").write_bytes(b"1" * 50)
        (root / "psx" / "Game A" / "Track 02.bin").write_bytes(b"2" * 50)

        svc = _make_catalog_service(provider, game_repo, proxy_repo, local_roms_dir, root)
        result = svc.refresh()

        assert result.added == 1
        games = game_repo.find_by_system("psx")
        assert len(games) == 1
        game = games[0]
        assert game.primary_asset.relative_path == "psx/Game A/Game A.cue"
        companion_paths = sorted(a.relative_path for a in game.assets if not a.is_primary)
        assert companion_paths == [
            "psx/Game A/Track 01.bin",
            "psx/Game A/Track 02.bin",
        ]

    def test_two_directory_scoped_games_do_not_collide_on_identical_track_names(
        self, provider, game_repo, proxy_repo, local_roms_dir, tmp_path
    ):
        """Two directory-scoped cue sets with identically-named tracks must
        be kept fully separate — asset identity is by full relative path,
        never bare filename."""
        root = tmp_path / "roms"
        for name in ("Game A", "Game B"):
            (root / "psx" / name).mkdir(parents=True)
            (root / "psx" / name / f"{name}.cue").write_text('FILE "Track 01.bin" BINARY\n')
            (root / "psx" / name / "Track 01.bin").write_bytes(name.encode() * 10)

        svc = _make_catalog_service(provider, game_repo, proxy_repo, local_roms_dir, root)
        result = svc.refresh()

        assert result.added == 2
        games = {g.title: g for g in game_repo.find_by_system("psx")}
        assert set(games) == {"Game A", "Game B"}

        companions_a = [a.relative_path for a in games["Game A"].assets if not a.is_primary]
        companions_b = [a.relative_path for a in games["Game B"].assets if not a.is_primary]
        assert companions_a == ["psx/Game A/Track 01.bin"]
        assert companions_b == ["psx/Game B/Track 01.bin"]

    def test_directory_without_cue_still_uses_opaque_directory_game(
        self, provider, game_repo, proxy_repo, local_roms_dir, tmp_path
    ):
        """Non-cue directories (e.g. PS3-style folder games) are unaffected."""
        root = tmp_path / "roms"
        (root / "ps3" / "BCES00000").mkdir(parents=True)
        (root / "ps3" / "BCES00000" / "EBOOT.BIN").write_bytes(b"x" * 50)

        svc = _make_catalog_service(provider, game_repo, proxy_repo, local_roms_dir, root)
        result = svc.refresh()

        assert result.added == 1
        games = game_repo.find_by_system("ps3")
        assert len(games) == 1
        assert games[0].primary_asset.relative_path == "ps3/BCES00000"

    def test_nested_directories_do_not_regress_top_level_scan(
        self, provider, game_repo, proxy_repo, local_roms_dir, tmp_path
    ):
        root = tmp_path / "roms"
        (root / "psx").mkdir(parents=True)
        (root / "psx" / "Standalone.iso").write_bytes(b"x" * 20)
        (root / "psx" / "Game A").mkdir()
        (root / "psx" / "Game A" / "Game A.cue").write_text('FILE "T.bin" BINARY\n')
        (root / "psx" / "Game A" / "T.bin").write_bytes(b"t" * 20)

        svc = _make_catalog_service(provider, game_repo, proxy_repo, local_roms_dir, root)
        result = svc.refresh()

        assert result.added == 2
        titles = sorted(g.title for g in game_repo.find_by_system("psx"))
        assert titles == ["Game A", "Standalone"]


class TestCueCatalogMigration:
    """Refresh must clean up stale entries from pre-cue-aware catalogs."""

    def test_refresh_prunes_stale_independent_track_after_cue_added(
        self, provider, game_repo, proxy_repo, local_roms_dir, tmp_path
    ):
        root = tmp_path / "roms"
        (root / "psx").mkdir(parents=True)
        (root / "psx" / "Game.bin").write_bytes(b"x" * 50)

        svc = _make_catalog_service(provider, game_repo, proxy_repo, local_roms_dir, root)

        # First refresh: no cue yet — Game.bin is catalogued as its own game.
        result1 = svc.refresh()
        assert result1.added == 1
        games = game_repo.find_by_system("psx")
        assert len(games) == 1
        assert games[0].title == "Game"
        stale_proxy = list(local_roms_dir.rglob("*.romcloud"))
        assert len(stale_proxy) == 1

        # A .cue referencing Game.bin now appears.
        (root / "psx" / "Game.cue").write_text('FILE "Game.bin" BINARY\n')
        result2 = svc.refresh()

        assert result2.added == 1  # the new cue game
        assert result2.removed == 1  # the stale standalone Game.bin entry

        games = game_repo.find_by_system("psx")
        assert len(games) == 1
        assert games[0].primary_asset.relative_path == "psx/Game.cue"

        # The stale entry's id must be gone; no user ROM files touched.
        assert game_repo.get(games[0].id) is not None
        assert (root / "psx" / "Game.bin").exists()
        assert (root / "psx" / "Game.cue").exists()

    def test_refresh_does_not_touch_unrelated_roms_during_prune(
        self, provider, game_repo, proxy_repo, local_roms_dir, tmp_path
    ):
        root = tmp_path / "roms"
        (root / "psx").mkdir(parents=True)
        (root / "psx" / "Game.bin").write_bytes(b"x" * 50)
        (root / "psx" / "Unrelated.iso").write_bytes(b"y" * 20)

        svc = _make_catalog_service(provider, game_repo, proxy_repo, local_roms_dir, root)
        svc.refresh()
        (root / "psx" / "Game.cue").write_text('FILE "Game.bin" BINARY\n')
        svc.refresh()

        # Unrelated game/proxy must survive untouched.
        titles = [g.title for g in game_repo.find_by_system("psx")]
        assert "Unrelated" in titles
        assert (root / "psx" / "Unrelated.iso").exists()

    def test_stable_after_migration_no_further_churn(
        self, provider, game_repo, proxy_repo, local_roms_dir, tmp_path
    ):
        root = tmp_path / "roms"
        (root / "psx").mkdir(parents=True)
        (root / "psx" / "Game.bin").write_bytes(b"x" * 50)

        svc = _make_catalog_service(provider, game_repo, proxy_repo, local_roms_dir, root)
        svc.refresh()
        (root / "psx" / "Game.cue").write_text('FILE "Game.bin" BINARY\n')
        svc.refresh()

        result3 = svc.refresh()
        assert result3.added == 0
        assert result3.removed == 0
        assert result3.skipped == 1


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
