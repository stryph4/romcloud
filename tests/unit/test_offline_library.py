from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest
from click.testing import CliRunner

from romcloud.bootstrap.container import Container
from romcloud.cli.main import cli
from romcloud.core.models.cache import CacheEntry, CacheStatus
from romcloud.core.models.game import Game, GameAsset
from romcloud.core.models.proxy import ProxyRecord
from romcloud.infrastructure.config import (
    AppConfig,
    CacheConfig,
    DIRECT_NAS_MODE,
    SMART_CACHE_MODE,
    SourceConfig,
    write_config,
)
from romcloud.infrastructure.database import Database
from romcloud.infrastructure.library_view import (
    offline_library_enabled,
    state_path,
)
from romcloud.infrastructure.repositories.cache import CacheRepository
from romcloud.infrastructure.repositories.game import GameRepository
from romcloud.infrastructure.repositories.proxy import ProxyRepository
from romcloud.integrations.batocera.game_access import (
    LINK_NAME,
    reconcile_game_access,
    set_offline_library_mode,
)
from romcloud.lifecycle.manage import restore_owned_proxies


def _config(tmp_path: Path, mode: str = SMART_CACHE_MODE) -> AppConfig:
    source = tmp_path / "source"
    local = tmp_path / "roms"
    cache = tmp_path / "cache"
    for root in (source / "snes", local / "snes", cache):
        root.mkdir(parents=True, exist_ok=True)
    return AppConfig(
        source=SourceConfig("local", str(source)),
        cache=CacheConfig(str(cache)),
        local_roms_path=str(local),
        data_path=str(tmp_path / "data"),
        game_access_mode=mode,
    )


def _add_game(
    config: AppConfig,
    db: Database,
    title: str,
    *,
    cached: bool = False,
    pinned: bool = False,
    stale: bool = False,
) -> tuple[Game, Path, Path | None]:
    filename = f"{title}.sfc"
    asset = GameAsset(
        filename=filename,
        relative_path=f"snes/{filename}",
        size_bytes=len(title.encode()),
        is_primary=True,
    )
    game = Game.create("snes", title, "local", config.source.rom_root, [asset])
    GameRepository(db).save(game)
    proxy = Path(config.local_roms_path) / "snes" / f"{title}.romcloud"
    ProxyRepository(db).save(ProxyRecord.create(game.id, str(proxy)))

    cached_path = None
    if cached or stale:
        cached_path = Path(config.cache.path) / "snes" / filename
        if cached and not stale:
            cached_path.parent.mkdir(parents=True, exist_ok=True)
            cached_path.write_bytes(title.encode())
        entry = CacheEntry.create(game.id, str(cached_path))
        entry.status = CacheStatus.COMPLETE
        entry.size_bytes = len(title.encode())
        entry.is_pinned = pinned
        CacheRepository(db).save(entry)
    return game, proxy, cached_path


def test_cached_only_presentation_is_reversible_safe_and_idempotent(tmp_path: Path) -> None:
    config = _config(tmp_path)
    db = Database(str(Path(config.data_path) / "catalog.db"))
    db.initialize()
    pinned, pinned_proxy, pinned_cache = _add_game(
        config, db, "Pinned Cached", cached=True, pinned=True
    )
    unpinned, unpinned_proxy, unpinned_cache = _add_game(
        config, db, "Unpinned Cached", cached=True
    )
    stale, stale_proxy, _ = _add_game(config, db, "Stale Cache", stale=True)
    uncached, uncached_proxy, _ = _add_game(config, db, "Uncached")
    restore_owned_proxies(config)

    local_rom = Path(config.local_roms_path) / "snes" / "Local Game.sfc"
    local_rom.write_bytes(b"local")
    foreign = Path(config.local_roms_path) / "snes" / "Foreign.romcloud"
    foreign.write_text('{"not": "romcloud-owned"}', encoding="utf-8")
    catalog_before = [game.id for game in GameRepository(db).list_all()]
    cache_before = CacheRepository(db).list_all()
    cache_bytes = {
        pinned_cache: pinned_cache.read_bytes(),
        unpinned_cache: unpinned_cache.read_bytes(),
    }

    first = set_offline_library_mode(config, True)
    second = set_offline_library_mode(config, True)

    assert first.offline and second.offline
    assert first.visible == second.visible == 2
    assert pinned_proxy.is_file() and unpinned_proxy.is_file()
    assert not stale_proxy.exists() and not uncached_proxy.exists()
    assert foreign.exists() and local_rom.exists()
    assert offline_library_enabled(config)
    assert json.loads(state_path(config).read_text()) == {
        "version": 1,
        "offline_library": True,
    }
    assert [game.id for game in GameRepository(db).list_all()] == catalog_before
    assert CacheRepository(db).list_all() == cache_before
    assert {path: path.read_bytes() for path in cache_bytes} == cache_bytes
    assert CacheRepository(db).get(stale.id) is not None
    assert CacheRepository(db).get(pinned.id).is_pinned
    assert not CacheRepository(db).get(unpinned.id).is_pinned

    restored = set_offline_library_mode(config, False)
    repeated = set_offline_library_mode(config, False)

    assert not restored.offline and not repeated.offline
    assert all(
        path.is_file()
        for path in (pinned_proxy, unpinned_proxy, stale_proxy, uncached_proxy)
    )
    assert not state_path(config).exists()
    assert foreign.exists() and local_rom.exists()
    assert [game.id for game in GameRepository(db).list_all()] == catalog_before
    assert CacheRepository(db).list_all() == cache_before
    assert {path: path.read_bytes() for path in cache_bytes} == cache_bytes
    assert uncached.id in catalog_before


def test_normal_refresh_retains_cached_only_view_and_records_new_games(tmp_path: Path) -> None:
    config = _config(tmp_path)
    db = Database(str(Path(config.data_path) / "catalog.db"))
    db.initialize()
    _add_game(config, db, "Existing Cached", cached=True)
    restore_owned_proxies(config)
    set_offline_library_mode(config, True)

    new_source = Path(config.source.rom_root) / "snes" / "New Uncached.sfc"
    new_source.write_bytes(b"new")
    container = Container(config)
    result = container.catalog.refresh()
    reconcile_game_access(config)

    assert not result.errors
    new_game = next(game for game in container.game_repo.list_all() if game.title == "New Uncached")
    record = container.proxy_repo.get(new_game.id)
    assert record is not None
    assert not Path(record.proxy_path).exists()
    assert offline_library_enabled(config)

    cached_path = Path(config.cache.path) / "snes" / "New Uncached.sfc"
    cached_path.parent.mkdir(parents=True, exist_ok=True)
    cached_path.write_bytes(b"new")
    entry = CacheEntry.create(new_game.id, str(cached_path))
    entry.status = CacheStatus.COMPLETE
    entry.size_bytes = 3
    container.cache_repo.save(entry)
    reconcile_game_access(config)

    assert Path(record.proxy_path).is_file()


def test_direct_mode_clears_offline_state_and_returns_to_full_smart_cache(tmp_path: Path) -> None:
    smart = _config(tmp_path)
    db = Database(str(Path(smart.data_path) / "catalog.db"))
    db.initialize()
    _game, proxy, _cache = _add_game(smart, db, "Cached", cached=True)
    restore_owned_proxies(smart)
    set_offline_library_mode(smart, True)

    direct = replace(smart, game_access_mode=DIRECT_NAS_MODE)
    reconcile_game_access(direct)

    assert not offline_library_enabled(direct)
    assert not proxy.exists()
    assert (Path(direct.local_roms_path) / "snes" / LINK_NAME).is_symlink()
    with pytest.raises(RuntimeError, match="Smart Cache"):
        set_offline_library_mode(direct, True)

    reconcile_game_access(smart)

    assert proxy.is_file()
    assert not (Path(smart.local_roms_path) / "snes" / LINK_NAME).exists()
    assert not offline_library_enabled(smart)


def test_library_cli_toggles_and_reports_state(tmp_path: Path, monkeypatch) -> None:
    config = _config(tmp_path)
    config_path = tmp_path / "config" / "romcloud.toml"
    write_config(config, str(config_path))
    monkeypatch.setattr(
        "romcloud.cli.commands.library.es_config.refresh", lambda systems: None
    )
    runner = CliRunner()

    offline = runner.invoke(
        cli, ["--config", str(config_path), "library", "offline"]
    )
    status = runner.invoke(
        cli, ["--config", str(config_path), "library", "status"]
    )
    gui_status = runner.invoke(
        cli, ["--config", str(config_path), "uidata", "setup-status"]
    )
    catalog_status = runner.invoke(
        cli, ["--config", str(config_path), "uidata", "status"]
    )
    online = runner.invoke(
        cli, ["--config", str(config_path), "library", "online"]
    )

    assert offline.exit_code == 0, offline.output
    assert "cached games only" in offline.output
    assert status.exit_code == 0 and "cached games only" in status.output
    assert json.loads(gui_status.output)["offline_library_mode"] is True
    assert json.loads(catalog_status.output)["offline_library_mode"] is True
    assert online.exit_code == 0 and "full Smart Cache catalog" in online.output
    assert not offline_library_enabled(config)


def test_library_cli_is_unavailable_in_direct_mode(tmp_path: Path) -> None:
    config = _config(tmp_path, DIRECT_NAS_MODE)
    config_path = tmp_path / "config" / "romcloud.toml"
    write_config(config, str(config_path))

    result = CliRunner().invoke(
        cli, ["--config", str(config_path), "library", "offline"]
    )

    assert result.exit_code == 1
    assert "unavailable in Direct/NAS mode" in result.output


def test_state_write_failure_rolls_back_to_previous_full_presentation(
    tmp_path: Path, monkeypatch
) -> None:
    config = _config(tmp_path)
    db = Database(str(Path(config.data_path) / "catalog.db"))
    db.initialize()
    _cached, cached_proxy, _ = _add_game(config, db, "Cached", cached=True)
    _uncached, uncached_proxy, _ = _add_game(config, db, "Uncached")
    restore_owned_proxies(config)

    monkeypatch.setattr(
        "romcloud.infrastructure.library_view.write_offline_library_state",
        lambda config, enabled: (_ for _ in ()).throw(OSError("disk full")),
    )

    with pytest.raises(OSError, match="disk full"):
        set_offline_library_mode(config, True)

    assert cached_proxy.is_file() and uncached_proxy.is_file()
    assert not offline_library_enabled(config)
