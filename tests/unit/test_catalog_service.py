"""Unit tests for CatalogService."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from romcloud.integrations.batocera.catalog import CatalogService
from romcloud.core.exceptions import ProxyError
from romcloud.core.models.game import Game, GameAsset
from romcloud.core.models.proxy import ProxyRecord
from romcloud.core.models.cache import CacheEntry, CacheStatus
from romcloud.integrations.batocera.system_registry import EffectiveSystemRegistry
from tests.system_registry_fixture import TEST_SYSTEM_REGISTRY


class TestCatalogServiceRefresh:
    def test_emits_structured_per_system_progress_with_real_denominators(
        self, catalog_service
    ):
        events = []

        result = catalog_service.refresh(progress=events.append)

        assert result.errors == []
        stages = [event.stage for event in events]
        assert stages[0] == "refresh_started"
        assert stages[-1] == "refresh_completed"
        queued = [event for event in events if event.stage == "system_queued"]
        completed = [event for event in events if event.stage == "system_completed"]
        assert {event.metadata["system"] for event in queued} == {"ps2", "nes", "snes"}
        assert {event.metadata["system"] for event in completed} == {"ps2", "nes", "snes"}
        determinate = [
            event
            for event in events
            if event.stage == "system_progress" and event.total
        ]
        assert determinate
        assert all(0 <= event.current <= event.total for event in determinate)
        final = events[-1]
        assert final.current == final.total == 3
        assert final.metadata == {"succeeded": 3, "failed": 0}

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
            system_registry=TEST_SYSTEM_REGISTRY,
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
            system_registry=TEST_SYSTEM_REGISTRY,
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


class TestPositiveRecursiveDiscovery:
    def test_unsupported_backup_is_not_catalogued(
        self, provider, game_repo, proxy_repo, local_roms_dir, tmp_path
    ):
        root = tmp_path / "roms"
        (root / "nes").mkdir(parents=True)
        (root / "nes" / "Mario.nes").write_bytes(b"rom")
        (root / "nes" / "gamelist.xml.bak").write_text("backup")

        result = _make_catalog_service(
            provider, game_repo, proxy_repo, local_roms_dir, root
        ).refresh()

        assert result.added == 1
        assert [game.title for game in game_repo.find_by_system("nes")] == ["Mario"]

    def test_xbla_and_arbitrary_nested_launchables_are_discovered(
        self, provider, game_repo, proxy_repo, local_roms_dir, tmp_path
    ):
        root = tmp_path / "roms"
        game_path = root / "xbox360" / "XBLA" / "Publisher" / "Game" / "default.xex"
        game_path.parent.mkdir(parents=True)
        game_path.write_bytes(b"xex")

        result = _make_catalog_service(
            provider, game_repo, proxy_repo, local_roms_dir, root
        ).refresh()

        assert result.added == 1
        game = game_repo.find_by_system("xbox360")[0]
        assert game.primary_asset.relative_path == (
            "xbox360/XBLA/Publisher/Game/default.xex"
        )

    def test_known_ineligible_legacy_row_is_hidden_but_retained_and_reactivates(
        self, provider, game_repo, cache_repo, proxy_repo, local_roms_dir, tmp_path
    ):
        root = tmp_path / "roms"
        (root / "nes").mkdir(parents=True)
        backup = root / "nes" / "gamelist.xml.bak"
        backup.write_text("backup")
        permissive = EffectiveSystemRegistry.from_extensions({"nes": {".bak"}})
        first = CatalogService(
            provider, game_repo, proxy_repo, str(local_roms_dir), str(root),
            system_registry=permissive,
        )
        first.refresh()
        legacy = game_repo.find_by_system("nes")[0]
        cache_entry = CacheEntry.create(legacy.id, str(tmp_path / "cache" / "backup"))
        cache_entry.is_pinned = True
        cache_repo.save(cache_entry)
        proxy_path = Path(proxy_repo.get(legacy.id).proxy_path)
        assert proxy_path.is_file()

        strict = CatalogService(
            provider, game_repo, proxy_repo, str(local_roms_dir), str(root),
            system_registry=EffectiveSystemRegistry.from_extensions({"nes": {".nes"}}),
        )
        strict.refresh()

        retained = game_repo.get(legacy.id)
        assert retained is not None and retained.is_eligible is False
        assert game_repo.find_by_system("nes") == []
        assert cache_repo.get(legacy.id).is_pinned is True
        assert proxy_repo.get(legacy.id) is None and not proxy_path.exists()

        reactivated = CatalogService(
            provider, game_repo, proxy_repo, str(local_roms_dir), str(root),
            system_registry=permissive,
        ).refresh()
        assert reactivated.updated == 1
        assert game_repo.find_by_system("nes")[0].id == legacy.id
        assert game_repo.get(legacy.id).is_eligible is True
        assert proxy_repo.get(legacy.id) is not None

    def test_failed_system_scan_never_suppresses_existing_row(
        self, provider, game_repo, proxy_repo, local_roms_dir, tmp_path, monkeypatch
    ):
        root = tmp_path / "roms"
        (root / "nes").mkdir(parents=True)
        (root / "nes" / "invalid.bak").write_text("backup")
        permissive = EffectiveSystemRegistry.from_extensions({"nes": {".bak"}})
        CatalogService(
            provider, game_repo, proxy_repo, str(local_roms_dir), str(root),
            system_registry=permissive,
        ).refresh()
        legacy = game_repo.find_by_system("nes")[0]
        proxy_path = Path(proxy_repo.get(legacy.id).proxy_path)
        assert proxy_path.is_file()
        monkeypatch.setattr(
            provider, "list_entries", lambda *_args: (_ for _ in ()).throw(OSError("gone"))
        )

        result = CatalogService(
            provider, game_repo, proxy_repo, str(local_roms_dir), str(root),
            system_registry=EffectiveSystemRegistry.from_extensions({"nes": {".nes"}}),
        ).refresh()

        assert result.errors == [("nes", "gone")]
        assert game_repo.get(legacy.id).is_eligible is True
        assert game_repo.find_by_system("nes")[0].id == legacy.id
        assert proxy_repo.get(legacy.id) is not None
        assert proxy_path.is_file()

    def test_last_known_good_registry_never_suppresses_existing_row(
        self, provider, game_repo, proxy_repo, local_roms_dir, tmp_path
    ):
        root = tmp_path / "roms"
        (root / "nes").mkdir(parents=True)
        (root / "nes" / "legacy.bak").write_text("backup")
        permissive = EffectiveSystemRegistry.from_extensions({"nes": {".bak"}})
        CatalogService(
            provider, game_repo, proxy_repo, str(local_roms_dir), str(root),
            system_registry=permissive,
        ).refresh()
        legacy = game_repo.find_by_system("nes")[0]
        proxy_path = Path(proxy_repo.get(legacy.id).proxy_path)
        assert proxy_path.is_file()
        strict = EffectiveSystemRegistry.from_extensions({"nes": {".nes"}})
        lkg = EffectiveSystemRegistry(strict.systems, from_last_known_good=True)

        CatalogService(
            provider, game_repo, proxy_repo, str(local_roms_dir), str(root),
            system_registry=lkg,
        ).refresh()

        assert game_repo.get(legacy.id).is_eligible is True
        assert game_repo.find_by_system("nes")[0].id == legacy.id
        assert proxy_repo.get(legacy.id) is not None
        assert proxy_path.is_file()

    def test_unambiguous_legacy_directory_adopts_id(
        self, provider, game_repo, proxy_repo, local_roms_dir, tmp_path
    ):
        root = tmp_path / "roms"
        nested = root / "xbox360" / "XBLA" / "Only Game" / "default.xex"
        nested.parent.mkdir(parents=True)
        nested.write_bytes(b"xex")
        legacy = Game.create(
            "xbox360", "XBLA", "local", str(root),
            [GameAsset("XBLA", "xbox360/XBLA", is_primary=True)],
        )
        game_repo.save(legacy)

        result = _make_catalog_service(
            provider, game_repo, proxy_repo, local_roms_dir, root
        ).refresh()

        assert result.updated == 1 and result.added == 0
        migrated = game_repo.get(legacy.id)
        assert migrated.primary_asset.relative_path == (
            "xbox360/XBLA/Only Game/default.xex"
        )

    def test_authoritative_refresh_removes_stale_legacy_container_presentation(
        self, provider, game_repo, cache_repo, proxy_repo, local_roms_dir, tmp_path
    ):
        root = tmp_path / "roms"
        for name in ("One", "Two"):
            path = root / "xbox360" / "XBLA" / name / "default.xex"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(name.encode())
        legacy = Game.create(
            "xbox360", "XBLA", "local", str(root),
            [GameAsset("XBLA", "xbox360/XBLA", is_primary=True)],
        )
        legacy.last_played = datetime(2025, 2, 3, tzinfo=timezone.utc)
        game_repo.save(legacy)
        cached_path = tmp_path / "cache" / "XBLA"
        cached_path.mkdir(parents=True)
        cached_content = cached_path / "legacy-cache.bin"
        cached_content.write_bytes(b"preserved")
        cached = CacheEntry.create(legacy.id, str(cached_path))
        cached.status = CacheStatus.COMPLETE
        cached.is_pinned = True
        cached.cached_at -= timedelta(days=10)
        cached.last_accessed -= timedelta(days=2)
        cached.size_bytes = 1234
        cache_repo.save(cached)

        service = _make_catalog_service(
            provider, game_repo, proxy_repo, local_roms_dir, root
        )
        stale_proxy = Path(service.ensure_proxy(legacy).proxy_path)
        foreign_file = stale_proxy.with_name("foreign-not-romcloud.romcloud")
        foreign_file.write_text("user-owned content", encoding="utf-8")
        # A signed proxy may outlive its manifest row after a legacy repair or
        # interrupted presentation transition. Its embedded game identity is
        # still sufficient to prove ROMCloud ownership.
        proxy_repo.delete(legacy.id)
        assert stale_proxy.is_file()

        result = service.refresh()

        assert result.added == 2 and result.updated == 0
        retained = game_repo.get(legacy.id)
        assert retained is not None and retained.is_eligible is False
        assert retained.id == legacy.id
        assert retained.last_played == legacy.last_played
        assert cache_repo.get(legacy.id) == cached
        assert cached_content.read_bytes() == b"preserved"
        assert not stale_proxy.exists()
        assert foreign_file.read_text(encoding="utf-8") == "user-owned content"
        visible = game_repo.find_by_system("xbox360")
        assert len(visible) == 2 and legacy.id not in {game.id for game in visible}
        child_records = {
            record.game_id: Path(record.proxy_path)
            for record in proxy_repo.list_all()
        }
        assert set(child_records) == {game.id for game in visible}
        assert all(path.is_file() for path in child_records.values())

        second = service.refresh()

        assert second.added == 0
        assert not stale_proxy.exists()
        assert foreign_file.is_file()


def _make_catalog_service(provider, game_repo, proxy_repo, local_roms_dir, source_root):
    return CatalogService(
        provider=provider,
        game_repo=game_repo,
        proxy_repo=proxy_repo,
        local_roms_root=str(local_roms_dir),
        source_root=str(source_root),
        system_registry=TEST_SYSTEM_REGISTRY,
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

    def test_launchable_extension_directory_is_an_opaque_package_game(
        self, provider, game_repo, proxy_repo, local_roms_dir, tmp_path
    ):
        """Batocera extension-matched directories are games and stop recursion."""
        root = tmp_path / "roms"
        (root / "ps3" / "BCES00000.ps3").mkdir(parents=True)
        (root / "ps3" / "BCES00000.ps3" / "EBOOT.BIN").write_bytes(b"x" * 50)

        svc = _make_catalog_service(provider, game_repo, proxy_repo, local_roms_dir, root)
        result = svc.refresh()

        assert result.added == 1
        games = game_repo.find_by_system("ps3")
        assert len(games) == 1
        assert games[0].primary_asset.relative_path == "ps3/BCES00000.ps3"

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
    def test_top_level_xbox_file_preserves_requested_exact_metadata(
        self, provider, game_repo, proxy_repo, local_roms_dir, tmp_path
    ):
        source = tmp_path / "source"
        (source / "xbox").mkdir(parents=True)
        (source / "xbox" / "Aggressive Inline.iso").write_bytes(b"xbox-iso")
        svc = _make_catalog_service(
            provider, game_repo, proxy_repo, local_roms_dir, source
        )

        svc.refresh()

        game = game_repo.find_by_system("xbox")[0]
        assert game.primary_asset is not None
        assert game.primary_asset.filename == "Aggressive Inline.iso"
        assert game.primary_asset.relative_path == "xbox/Aggressive Inline.iso"
        payload = json.loads(
            (local_roms_dir / "xbox" / "Aggressive Inline.romcloud").read_text()
        )
        assert payload["assets"][0]["filename"] == "Aggressive Inline.iso"
        assert payload["assets"][0]["relative_path"] == "xbox/Aggressive Inline.iso"

    def test_xbox_single_file_container_preserves_exact_iso_metadata(
        self, provider, game_repo, proxy_repo, local_roms_dir, tmp_path
    ):
        source = tmp_path / "source"
        game_dir = source / "xbox" / "Aggressive Inline"
        game_dir.mkdir(parents=True)
        (game_dir / "Aggressive Inline.iso").write_bytes(b"xbox-iso")
        svc = _make_catalog_service(
            provider, game_repo, proxy_repo, local_roms_dir, source
        )

        result = svc.refresh()

        assert result.added == 1
        game = game_repo.find_by_system("xbox")[0]
        assert game.primary_asset is not None
        assert game.primary_asset.filename == "Aggressive Inline.iso"
        assert game.primary_asset.relative_path == (
            "xbox/Aggressive Inline/Aggressive Inline.iso"
        )
        payload = json.loads(
            (local_roms_dir / "xbox" / "Aggressive Inline.romcloud").read_text()
        )
        assert payload["assets"][0]["filename"] == "Aggressive Inline.iso"
        assert payload["assets"][0]["relative_path"] == (
            "xbox/Aggressive Inline/Aggressive Inline.iso"
        )

    def test_non_xbox_single_file_container_preserves_exact_extension(
        self, provider, game_repo, proxy_repo, local_roms_dir, tmp_path
    ):
        source = tmp_path / "source"
        game_dir = source / "gamecube" / "Metroid Prime"
        game_dir.mkdir(parents=True)
        (game_dir / "Metroid Prime.rvz").write_bytes(b"gamecube-rvz")
        svc = _make_catalog_service(
            provider, game_repo, proxy_repo, local_roms_dir, source
        )

        svc.refresh()

        game = game_repo.find_by_system("gamecube")[0]
        assert game.primary_asset is not None
        assert game.primary_asset.filename == "Metroid Prime.rvz"
        assert game.primary_asset.relative_path == (
            "gamecube/Metroid Prime/Metroid Prime.rvz"
        )
        payload = json.loads(
            (local_roms_dir / "gamecube" / "Metroid Prime.romcloud").read_text()
        )
        assert payload["assets"][0]["filename"] == "Metroid Prime.rvz"
        assert payload["assets"][0]["relative_path"] == (
            "gamecube/Metroid Prime/Metroid Prime.rvz"
        )

    def test_refresh_repairs_legacy_container_asset_and_owned_proxy_in_place(
        self, provider, game_repo, proxy_repo, local_roms_dir, tmp_path
    ):
        source = tmp_path / "source"
        game_dir = source / "xbox" / "Aggressive Inline"
        game_dir.mkdir(parents=True)
        (game_dir / "Aggressive Inline.iso").write_bytes(b"xbox-iso")
        legacy = Game.create(
            system="xbox",
            title="Aggressive Inline",
            source_provider="local",
            source_root=str(source),
            assets=[
                GameAsset(
                    filename="Aggressive Inline",
                    relative_path="xbox/Aggressive Inline",
                    size_bytes=8,
                    is_primary=True,
                )
            ],
        )
        game_repo.save(legacy)
        proxy_path = local_roms_dir / "xbox" / "Aggressive Inline.romcloud"
        proxy_path.parent.mkdir(parents=True)
        proxy_path.write_text(json.dumps({
            "romcloud_version": "1",
            "game_id": legacy.id,
            "title": legacy.title,
            "system": legacy.system,
            "source_provider": legacy.source_provider,
            "source_root": legacy.source_root,
            "assets": [{
                "filename": "Aggressive Inline",
                "relative_path": "xbox/Aggressive Inline",
                "is_primary": True,
            }],
        }))
        proxy_repo.save(ProxyRecord.create(legacy.id, str(proxy_path)))
        svc = _make_catalog_service(
            provider, game_repo, proxy_repo, local_roms_dir, source
        )

        result = svc.refresh()

        assert result.added == 0
        assert result.updated == 1
        repaired = game_repo.get(legacy.id)
        assert repaired is not None
        assert repaired.primary_asset is not None
        assert repaired.primary_asset.filename == "Aggressive Inline.iso"
        assert repaired.primary_asset.relative_path == (
            "xbox/Aggressive Inline/Aggressive Inline.iso"
        )
        payload = json.loads(proxy_path.read_text())
        assert payload["game_id"] == legacy.id
        assert payload["assets"][0]["filename"] == "Aggressive Inline.iso"
        assert payload["assets"][0]["relative_path"] == (
            "xbox/Aggressive Inline/Aggressive Inline.iso"
        )

    def test_xbox_proxy_preserves_primary_iso_metadata(
        self, provider, game_repo, proxy_repo, local_roms_dir, tmp_path
    ):
        source = tmp_path / "source"
        (source / "xbox").mkdir(parents=True)
        (source / "xbox" / "Aeon Flux.iso").write_bytes(b"xbox-iso")
        svc = _make_catalog_service(
            provider, game_repo, proxy_repo, local_roms_dir, source
        )

        svc.refresh()
        proxy = local_roms_dir / "xbox" / "Aeon Flux.romcloud"
        game = svc.resolve_proxy(str(proxy))

        assert game.system == "xbox"
        assert game.primary_asset is not None
        assert game.primary_asset.filename == "Aeon Flux.iso"
        assert game.primary_asset.relative_path == "xbox/Aeon Flux.iso"

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
            system_registry=TEST_SYSTEM_REGISTRY,
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
