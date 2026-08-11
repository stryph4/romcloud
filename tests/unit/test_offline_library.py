from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest
from click.testing import CliRunner

from romcloud.bootstrap.container import Container
from romcloud.cli.main import cli
from romcloud.core.capabilities import OperatingMode
from romcloud.core.exceptions import CapabilityUnavailableError, ModeTransitionError
from romcloud.core.models.cache import CacheEntry, CacheStatus
from romcloud.core.models.game import Game, GameAsset
from romcloud.core.models.proxy import ProxyRecord
from romcloud.infrastructure.config import (
    AppConfig,
    CacheConfig,
    DIRECT_NAS_MODE,
    LibrarySyncConfig,
    RemoteDataConfig,
    SMBConfig,
    SMART_CACHE_MODE,
    SourceConfig,
    write_config,
)
from romcloud.infrastructure.database import Database
from romcloud.infrastructure.library_view import (
    offline_library_enabled,
    operating_mode,
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


@pytest.fixture(autouse=True)
def _stub_es_refresh(monkeypatch):
    calls: list[tuple[str, ...]] = []
    monkeypatch.setattr(
        "romcloud.integrations.batocera.game_access._refresh_emulationstation",
        lambda config, systems: calls.append(tuple(systems)),
    )
    return calls


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
        "version": 2,
        "mode": "offline",
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
    assert json.loads(state_path(config).read_text()) == {
        "version": 2,
        "mode": "nas",
    }
    assert foreign.exists() and local_rom.exists()
    assert [game.id for game in GameRepository(db).list_all()] == catalog_before
    assert CacheRepository(db).list_all() == cache_before
    assert {path: path.read_bytes() for path in cache_bytes} == cache_bytes
    assert uncached.id in catalog_before


def test_offline_refresh_is_blocked_without_catalog_or_proxy_changes(tmp_path: Path) -> None:
    config = _config(tmp_path)
    db = Database(str(Path(config.data_path) / "catalog.db"))
    db.initialize()
    _add_game(config, db, "Existing Cached", cached=True)
    restore_owned_proxies(config)
    set_offline_library_mode(config, True)

    new_source = Path(config.source.rom_root) / "snes" / "New Uncached.sfc"
    new_source.write_bytes(b"new")
    container = Container(config)
    with pytest.raises(Exception, match="Offline Mode"):
        container.catalog.refresh()

    assert all(game.title != "New Uncached" for game in container.game_repo.list_all())
    assert offline_library_enabled(config)


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
    with pytest.raises(CapabilityUnavailableError, match="Smart Cache"):
        set_offline_library_mode(direct, True)

    reconcile_game_access(smart)

    assert proxy.is_file()
    assert not (Path(smart.local_roms_path) / "snes" / LINK_NAME).exists()
    assert not offline_library_enabled(smart)


def test_library_cli_toggles_and_reports_state(tmp_path: Path, monkeypatch) -> None:
    config = _config(tmp_path)
    config_path = tmp_path / "config" / "romcloud.toml"
    write_config(config, str(config_path))
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
    nas = runner.invoke(
        cli, ["--config", str(config_path), "library", "nas"]
    )

    assert offline.exit_code == 0, offline.output
    assert "Offline Mode" in offline.output
    assert status.exit_code == 0 and "Offline Mode" in status.output
    assert json.loads(gui_status.output)["offline_library_mode"] is True
    assert json.loads(catalog_status.output)["offline_library_mode"] is True
    assert nas.exit_code == 0 and "NAS Mode" in nas.output
    assert not offline_library_enabled(config)


def test_library_cli_is_unavailable_in_direct_mode(tmp_path: Path) -> None:
    config = _config(tmp_path, DIRECT_NAS_MODE)
    config_path = tmp_path / "config" / "romcloud.toml"
    write_config(config, str(config_path))

    result = CliRunner().invoke(
        cli, ["--config", str(config_path), "library", "offline"]
    )

    assert result.exit_code == 1
    assert "available only in Smart Cache mode" in result.output


def test_state_write_failure_rolls_back_to_previous_full_presentation(
    tmp_path: Path, monkeypatch
) -> None:
    config = _config(tmp_path)
    db = Database(str(Path(config.data_path) / "catalog.db"))
    db.initialize()
    _cached, cached_proxy, _ = _add_game(config, db, "Cached", cached=True)
    _uncached, uncached_proxy, _ = _add_game(config, db, "Uncached")
    restore_owned_proxies(config)
    assert operating_mode(config) is OperatingMode.NAS

    monkeypatch.setattr(
        "romcloud.infrastructure.library_view.write_operating_mode",
        lambda config, mode: (_ for _ in ()).throw(OSError("disk full")),
    )

    with pytest.raises(OSError, match="disk full"):
        set_offline_library_mode(config, True)

    assert cached_proxy.is_file() and uncached_proxy.is_file()
    assert not offline_library_enabled(config)


def test_es_refresh_runs_only_after_successful_transition(
    tmp_path: Path, monkeypatch, _stub_es_refresh
) -> None:
    config = _config(tmp_path)
    db = Database(str(Path(config.data_path) / "catalog.db"))
    db.initialize()
    _add_game(config, db, "Cached", cached=True)
    restore_owned_proxies(config)

    set_offline_library_mode(config, True)
    assert _stub_es_refresh == [("snes",)]

    monkeypatch.setattr(
        "romcloud.integrations.batocera.game_access.reconcile_library_presentation",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("reconcile failed")),
    )
    with pytest.raises(ModeTransitionError, match="remains in Offline Mode"):
        set_offline_library_mode(config, False)
    assert _stub_es_refresh == [("snes",)]


def test_returning_to_nas_refreshes_provider_before_restoring_full_library(
    tmp_path: Path
) -> None:
    config = _config(tmp_path)
    db = Database(str(Path(config.data_path) / "catalog.db"))
    db.initialize()
    _cached, cached_proxy, _ = _add_game(config, db, "Cached", cached=True)
    _uncached, uncached_proxy, _ = _add_game(config, db, "Uncached")
    restore_owned_proxies(config)
    set_offline_library_mode(config, True)
    assert cached_proxy.exists() and not uncached_proxy.exists()

    set_offline_library_mode(config, False)

    assert cached_proxy.exists() and uncached_proxy.exists()
    assert operating_mode(config) is OperatingMode.NAS


def test_failed_offline_to_nas_reconnect_keeps_cached_library_and_mode(
    tmp_path: Path, _stub_es_refresh
) -> None:
    config = _config(tmp_path)
    db = Database(str(Path(config.data_path) / "catalog.db"))
    db.initialize()
    _cached, cached_proxy, _ = _add_game(config, db, "Cached", cached=True)
    _uncached, uncached_proxy, _ = _add_game(config, db, "Uncached")
    restore_owned_proxies(config)
    set_offline_library_mode(config, True)
    Path(config.source.rom_root).rename(tmp_path / "disconnected-source")

    with pytest.raises(ModeTransitionError, match="try NAS Mode again"):
        set_offline_library_mode(config, False)

    assert operating_mode(config) is OperatingMode.OFFLINE
    assert cached_proxy.is_file()
    assert not uncached_proxy.exists()
    assert _stub_es_refresh == [("snes",)]


def test_nas_connectivity_loss_does_not_change_authoritative_mode(tmp_path: Path) -> None:
    config = _config(tmp_path)
    assert operating_mode(config) is OperatingMode.NAS
    Path(config.source.rom_root).rename(tmp_path / "disconnected-source")

    assert operating_mode(config) is OperatingMode.NAS
    assert json.loads(state_path(config).read_text()) == {
        "version": 2,
        "mode": "nas",
    }


def test_legacy_offline_state_migrates_to_explicit_operating_mode(tmp_path: Path) -> None:
    config = _config(tmp_path)
    path = state_path(config)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"version": 1, "offline_library": true}\n', encoding="utf-8")

    assert operating_mode(config) is OperatingMode.OFFLINE
    assert json.loads(path.read_text()) == {"version": 2, "mode": "offline"}


def test_offline_to_nas_uses_configured_mount_lifecycle(
    tmp_path: Path, monkeypatch
) -> None:
    config = replace(
        _config(tmp_path),
        smb=SMBConfig("nas.local", "roms", "player"),
    )
    set_offline_library_mode(config, True)
    calls: list[AppConfig] = []
    monkeypatch.setattr(
        "romcloud.services.connections.mount_connections",
        lambda mounted_config, progress=None: calls.append(mounted_config) or {
            "changed": True
        },
    )

    set_offline_library_mode(config, False)

    assert calls == [config]
    assert operating_mode(config) is OperatingMode.NAS


def test_offline_to_nas_validates_remote_data_and_runs_enabled_library_sync(
    tmp_path: Path
) -> None:
    remote_data = tmp_path / "remote-data"
    remote_data.mkdir()
    config = replace(
        _config(tmp_path),
        remote_data=RemoteDataConfig("local", str(remote_data)),
        library_sync=LibrarySyncConfig(enabled=True),
    )
    db = Database(str(Path(config.data_path) / "catalog.db"))
    db.initialize()
    _add_game(config, db, "Cached", cached=True)
    restore_owned_proxies(config)
    set_offline_library_mode(config, True)

    set_offline_library_mode(config, False)

    assert operating_mode(config) is OperatingMode.NAS
    assert (remote_data / "library" / "library.json").is_file()


def test_missing_required_remote_data_aborts_nas_transition(tmp_path: Path) -> None:
    config = replace(
        _config(tmp_path),
        remote_data=RemoteDataConfig("local", str(tmp_path / "missing-remote-data")),
    )
    db = Database(str(Path(config.data_path) / "catalog.db"))
    db.initialize()
    _cached, cached_proxy, _ = _add_game(config, db, "Cached", cached=True)
    _uncached, uncached_proxy, _ = _add_game(config, db, "Uncached")
    restore_owned_proxies(config)
    set_offline_library_mode(config, True)

    with pytest.raises(ModeTransitionError, match="remains in Offline Mode"):
        set_offline_library_mode(config, False)

    assert operating_mode(config) is OperatingMode.OFFLINE
    assert cached_proxy.is_file()
    assert not uncached_proxy.exists()
