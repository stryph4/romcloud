from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from threading import Event, Thread

import pytest
from click.testing import CliRunner

from romcloud.bootstrap.container import Container
from romcloud.cli.main import cli
from romcloud.core.capabilities import OperatingMode
from romcloud.core.exceptions import ModeTransitionError
from romcloud.core.models.cache import CacheEntry, CacheStatus
from romcloud.core.models.game import Game, GameAsset
from romcloud.core.models.proxy import ProxyRecord
from romcloud.core.progress import ProgressEvent
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
    operating_mode,
    state_path,
    write_operating_mode,
)
from romcloud.infrastructure.repositories.cache import CacheRepository
from romcloud.infrastructure.repositories.game import GameRepository
from romcloud.infrastructure.repositories.proxy import ProxyRepository
from romcloud.integrations.batocera.game_access import (
    LINK_NAME,
    _operating_mode_lock,
    reconcile_game_access,
    set_operating_mode,
)
from romcloud.lifecycle.manage import restore_owned_proxies


@pytest.fixture(autouse=True)
def _stub_es(monkeypatch):
    refreshes: list[tuple[tuple[str, ...], str]] = []
    reloads: list[bool] = []
    monkeypatch.setattr(
        "romcloud.integrations.batocera.game_access._refresh_emulationstation",
        lambda config, systems, **kwargs: refreshes.append(
            (tuple(systems), OperatingMode(kwargs["mode"]).value)
        ),
    )
    monkeypatch.setattr(
        "romcloud.integrations.batocera.game_access._reload_emulationstation",
        lambda: reloads.append(True) or True,
    )
    return refreshes, reloads


def _config(tmp_path: Path, strategy: str = SMART_CACHE_MODE) -> AppConfig:
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
        game_access_mode=strategy,
    )


def _add_game(
    config: AppConfig,
    title: str,
    *,
    cached: bool = False,
    pinned: bool = False,
) -> tuple[Game, Path, Path | None]:
    db = Database(str(Path(config.data_path) / "catalog.db"))
    db.initialize()
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
    source = Path(config.source.rom_root) / asset.relative_path
    source.write_bytes(title.encode())

    cached_path = None
    if cached:
        cached_path = Path(config.cache.path) / "snes" / filename
        cached_path.parent.mkdir(parents=True, exist_ok=True)
        cached_path.write_bytes(title.encode())
        entry = CacheEntry.create(game.id, str(cached_path))
        entry.status = CacheStatus.COMPLETE
        entry.size_bytes = len(title.encode())
        entry.is_pinned = pinned
        CacheRepository(db).save(entry)
    return game, proxy, cached_path


def _library(config: AppConfig):
    cached = _add_game(config, "Cached", cached=True, pinned=True)
    uncached = _add_game(config, "Uncached")
    restore_owned_proxies(config)
    return cached, uncached


@pytest.mark.parametrize(
    ("strategy", "payload", "expected"),
    [
        (SMART_CACHE_MODE, None, OperatingMode.CACHE),
        (DIRECT_NAS_MODE, None, OperatingMode.CONNECTED),
        (SMART_CACHE_MODE, {"version": 1, "offline_library": True}, OperatingMode.OFFLINE),
        (SMART_CACHE_MODE, {"version": 2, "mode": "offline"}, OperatingMode.OFFLINE),
        (SMART_CACHE_MODE, {"version": 2, "mode": "nas"}, OperatingMode.CONNECTED),
        (DIRECT_NAS_MODE, {"version": 2, "mode": "nas"}, OperatingMode.CONNECTED),
    ],
)
def test_authoritative_three_state_persistence_and_migration(
    tmp_path: Path, strategy: str, payload: dict | None, expected: OperatingMode
) -> None:
    config = _config(tmp_path, strategy)
    if payload is not None:
        state_path(config).parent.mkdir(parents=True, exist_ok=True)
        state_path(config).write_text(json.dumps(payload), encoding="utf-8")

    assert operating_mode(config) is expected
    assert json.loads(state_path(config).read_text()) == {
        "version": 3,
        "mode": expected.value,
    }


@pytest.mark.parametrize("selected", list(OperatingMode))
def test_only_three_authoritative_values_can_be_persisted(
    tmp_path: Path, selected: OperatingMode
) -> None:
    config = _config(tmp_path)
    write_operating_mode(config, selected)
    assert operating_mode(config) is selected
    with pytest.raises(ValueError):
        write_operating_mode(config, "nas")


def test_connected_to_cache_is_database_backed_and_preserves_cache(
    tmp_path: Path, monkeypatch
) -> None:
    config = _config(tmp_path, DIRECT_NAS_MODE)
    (cached, cached_proxy, cached_path), (_uncached, uncached_proxy, _) = _library(config)
    reconcile_game_access(config, refresh_es=False)
    assert (Path(config.local_roms_path) / "snes" / LINK_NAME).is_symlink()
    monkeypatch.setattr(
        "romcloud.integrations.batocera.catalog.CatalogService.refresh",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("source scan")),
    )

    set_operating_mode(config, OperatingMode.CACHE)

    assert operating_mode(config) is OperatingMode.CACHE
    assert cached_proxy.is_file() and uncached_proxy.is_file()
    assert not (Path(config.local_roms_path) / "snes" / LINK_NAME).exists()
    assert cached_path is not None and cached_path.read_bytes() == b"Cached"
    assert Container(config).cache_repo.get(cached.id).is_pinned


def test_cache_to_connected_uses_catalog_systems_without_refresh(
    tmp_path: Path, monkeypatch
) -> None:
    config = _config(tmp_path)
    (cached, cached_proxy, cached_path), (_uncached, uncached_proxy, _) = _library(config)
    monkeypatch.setattr(
        "romcloud.integrations.batocera.catalog.CatalogService.refresh",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("source scan")),
    )

    set_operating_mode(config, OperatingMode.CONNECTED)

    assert operating_mode(config) is OperatingMode.CONNECTED
    assert not cached_proxy.exists() and not uncached_proxy.exists()
    assert (Path(config.local_roms_path) / "snes" / LINK_NAME).is_symlink()
    assert cached_path is not None and cached_path.read_bytes() == b"Cached"
    assert Container(config).cache_repo.get(cached.id).is_pinned


def test_cache_to_offline_hides_only_source_only_managed_games(tmp_path: Path) -> None:
    config = _config(tmp_path)
    (_cached, cached_proxy, _), (_uncached, uncached_proxy, _) = _library(config)
    local_rom = Path(config.local_roms_path) / "snes" / "Local Game.sfc"
    local_rom.write_bytes(b"local")
    foreign_proxy = Path(config.local_roms_path) / "snes" / "Foreign.romcloud"
    foreign_proxy.write_text('{"owner":"other"}', encoding="utf-8")

    set_operating_mode(config, OperatingMode.OFFLINE)

    assert cached_proxy.is_file() and not uncached_proxy.exists()
    assert local_rom.read_bytes() == b"local" and foreign_proxy.is_file()


def test_offline_to_cache_restores_catalog_while_source_is_unavailable(tmp_path: Path) -> None:
    config = _config(tmp_path)
    (_cached, cached_proxy, _), (_uncached, uncached_proxy, _) = _library(config)
    set_operating_mode(config, OperatingMode.OFFLINE)
    Path(config.source.rom_root).rename(tmp_path / "disconnected")

    set_operating_mode(config, OperatingMode.CACHE)

    assert operating_mode(config) is OperatingMode.CACHE
    assert cached_proxy.is_file() and uncached_proxy.is_file()


def test_connected_to_offline_does_not_require_source(tmp_path: Path) -> None:
    config = _config(tmp_path, DIRECT_NAS_MODE)
    (_cached, cached_proxy, _), (_uncached, uncached_proxy, _) = _library(config)
    local_rom = Path(config.local_roms_path) / "snes" / "Local Game.sfc"
    local_rom.write_bytes(b"local")
    reconcile_game_access(config, refresh_es=False)
    Path(config.source.rom_root).rename(tmp_path / "disconnected")

    set_operating_mode(config, OperatingMode.OFFLINE)

    assert operating_mode(config) is OperatingMode.OFFLINE
    assert cached_proxy.is_file() and not uncached_proxy.exists()
    assert local_rom.read_bytes() == b"local"


def test_failed_connected_to_cache_restores_dangling_direct_links(
    tmp_path: Path, monkeypatch
) -> None:
    config = _config(tmp_path, DIRECT_NAS_MODE)
    (_cached, cached_proxy, _), (_uncached, uncached_proxy, _) = _library(config)
    reconcile_game_access(config, refresh_es=False)
    link = Path(config.local_roms_path) / "snes" / LINK_NAME
    assert link.is_symlink()
    Path(config.source.rom_root).rename(tmp_path / "disconnected")

    def refresh(config, systems, **kwargs):
        if OperatingMode(kwargs["mode"]) is OperatingMode.CACHE:
            raise RuntimeError("ES update failed")

    monkeypatch.setattr(
        "romcloud.integrations.batocera.game_access._refresh_emulationstation",
        refresh,
    )

    with pytest.raises(ModeTransitionError, match="remains in Connected Mode"):
        set_operating_mode(config, OperatingMode.CACHE)

    assert operating_mode(config) is OperatingMode.CONNECTED
    assert link.is_symlink()
    assert not cached_proxy.exists() and not uncached_proxy.exists()


def test_offline_to_connected_validates_source_without_catalog_refresh(
    tmp_path: Path, monkeypatch
) -> None:
    config = _config(tmp_path)
    _library(config)
    set_operating_mode(config, OperatingMode.OFFLINE)
    monkeypatch.setattr(
        "romcloud.integrations.batocera.catalog.CatalogService.refresh",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("source scan")),
    )

    set_operating_mode(config, OperatingMode.CONNECTED)

    assert operating_mode(config) is OperatingMode.CONNECTED
    assert (Path(config.local_roms_path) / "snes" / LINK_NAME).is_symlink()


@pytest.mark.parametrize("start", [OperatingMode.CACHE, OperatingMode.CONNECTED])
def test_source_loss_never_changes_selected_mode(tmp_path: Path, start: OperatingMode) -> None:
    config = _config(tmp_path)
    write_operating_mode(config, start)
    Path(config.source.rom_root).rename(tmp_path / "disconnected")
    assert operating_mode(config) is start


def test_failed_connected_transition_rolls_back_presentation_and_mode(tmp_path: Path) -> None:
    config = _config(tmp_path)
    (_cached, cached_proxy, _), (_uncached, uncached_proxy, _) = _library(config)
    conflict = Path(config.local_roms_path) / "snes" / LINK_NAME
    conflict.mkdir()

    with pytest.raises(ModeTransitionError, match="remains in Cache Mode"):
        set_operating_mode(config, OperatingMode.CONNECTED)

    assert operating_mode(config) is OperatingMode.CACHE
    assert cached_proxy.is_file() and uncached_proxy.is_file() and conflict.is_dir()


def test_connected_transition_rejects_source_overlapping_local_roms(tmp_path: Path) -> None:
    config = _config(tmp_path)
    _library(config)
    overlapping = replace(
        config,
        source=SourceConfig("local", config.local_roms_path),
    )

    with pytest.raises(ModeTransitionError, match="remains in Cache Mode"):
        set_operating_mode(overlapping, OperatingMode.CONNECTED)

    assert operating_mode(overlapping) is OperatingMode.CACHE


def test_state_write_failure_rolls_back_to_previous_presentation(
    tmp_path: Path, monkeypatch
) -> None:
    config = _config(tmp_path)
    (_cached, cached_proxy, _), (_uncached, uncached_proxy, _) = _library(config)
    assert operating_mode(config) is OperatingMode.CACHE
    monkeypatch.setattr(
        "romcloud.infrastructure.library_view.write_operating_mode",
        lambda config, mode: (_ for _ in ()).throw(OSError("disk full")),
    )

    with pytest.raises(ModeTransitionError):
        set_operating_mode(config, OperatingMode.OFFLINE)

    assert cached_proxy.is_file() and uncached_proxy.is_file()


def test_progress_has_truthful_counts_and_indeterminate_phases(tmp_path: Path) -> None:
    config = _config(tmp_path)
    _library(config)
    events: list[ProgressEvent] = []

    set_operating_mode(config, OperatingMode.OFFLINE, progress=events.append)

    counted = [event for event in events if event.total is not None]
    assert counted and all(event.current is not None for event in counted)
    assert any(
        event.stage == "refresh_notice" and event.status == "running"
        and event.message
        == "Mode changed successfully. Refreshing EmulationStation game list…"
        and event.current is None and event.total is None
        for event in events
    )
    assert events[-1].stage == "complete" and events[-1].status == "success"
    assert events[-1].current is None and events[-1].total is None


@pytest.mark.parametrize("mode", list(OperatingMode))
def test_reentering_the_active_mode_is_a_full_noop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: OperatingMode
) -> None:
    """Same-mode selection performs no source checks or presentation work."""
    config = _config(tmp_path)
    _library(config)
    write_operating_mode(config, mode)
    for name in (
        "_verified_direct_link_snapshot",
        "_prepare_connected_source",
        "_apply_mode_presentation",
        "_update_emulationstation",
    ):
        monkeypatch.setattr(
            f"romcloud.integrations.batocera.game_access.{name}",
            lambda *_args, _name=name, **_kwargs: (_ for _ in ()).throw(
                AssertionError(f"same-mode request called {_name}")
            ),
        )

    report = set_operating_mode(config, mode)

    assert report.mode_changed is False
    assert report.es_restarted is False
    assert operating_mode(config) is mode


def test_reconnect_readiness_recovery_never_restarts_es(tmp_path: Path, _stub_es) -> None:
    """`reconcile_game_access` is the reconnect/readiness/repair path run
    after mount success, catalog refresh, or startup recovery — it must
    never restart ES merely because it ran again while the source was
    temporarily unavailable and the authoritative mode stayed Cache."""
    refreshes, reloads = _stub_es
    config = _config(tmp_path)
    _library(config)
    set_operating_mode(config, OperatingMode.CACHE)
    reloads.clear()
    Path(config.source.rom_root).rename(tmp_path / "disconnected")

    reconcile_game_access(config, render_library_metadata=False)

    assert reloads == []
    assert operating_mode(config) is OperatingMode.CACHE


@pytest.mark.parametrize(
    "start,requested",
    [
        (OperatingMode.CACHE, OperatingMode.CONNECTED),
        (OperatingMode.CONNECTED, OperatingMode.CACHE),
        (OperatingMode.CACHE, OperatingMode.OFFLINE),
        (OperatingMode.OFFLINE, OperatingMode.CACHE),
        (OperatingMode.CONNECTED, OperatingMode.OFFLINE),
        (OperatingMode.OFFLINE, OperatingMode.CONNECTED),
    ],
)
def test_genuine_transition_reports_es_restarted(
    tmp_path: Path, _stub_es, start: OperatingMode, requested: OperatingMode
) -> None:
    """Real-hardware regression: `batocera-es-swissknife --restart` is
    fire-and-forget, so a genuine mode transition must surface a
    deterministic signal that ES was actually asked to restart — never
    silently claim the new presentation is already launch-ready."""
    config = _config(tmp_path)
    _library(config)
    write_operating_mode(config, start)

    report = set_operating_mode(config, requested)

    assert report.mode_changed is True
    assert report.es_restarted is True
    assert operating_mode(config) is requested
    refreshes, reloads = _stub_es
    assert refreshes and refreshes[-1][1] == requested.value
    assert reloads == [True]


def test_mode_state_and_refresh_notice_precede_es_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path)
    _library(config)
    write_operating_mode(config, OperatingMode.CACHE)
    timeline: list[str] = []

    def progress(event: ProgressEvent) -> None:
        timeline.append(f"progress:{event.stage}")

    def refresh(_config, _systems, **kwargs) -> None:  # noqa: ANN001
        assert operating_mode(config) is OperatingMode.OFFLINE
        assert OperatingMode(kwargs["mode"]) is OperatingMode.OFFLINE
        timeline.append("es:refresh")

    monkeypatch.setattr(
        "romcloud.integrations.batocera.game_access._refresh_emulationstation",
        refresh,
    )
    monkeypatch.setattr(
        "romcloud.integrations.batocera.game_access._reload_emulationstation",
        lambda: timeline.append("es:restart") or True,
    )

    set_operating_mode(config, OperatingMode.OFFLINE, progress=progress)

    notice = timeline.index("progress:refresh_notice")
    refresh_call = timeline.index("es:refresh")
    restart_call = timeline.index("es:restart")
    assert notice < refresh_call < restart_call


def test_failed_es_refresh_restores_pre_transition_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path)
    (_cached, cached_proxy, _), (_uncached, uncached_proxy, _) = _library(config)
    write_operating_mode(config, OperatingMode.CACHE)

    def refresh(_config, _systems, **kwargs) -> None:  # noqa: ANN001
        if OperatingMode(kwargs["mode"]) is OperatingMode.OFFLINE:
            raise RuntimeError("ES refresh failed")

    monkeypatch.setattr(
        "romcloud.integrations.batocera.game_access._refresh_emulationstation",
        refresh,
    )

    with pytest.raises(ModeTransitionError, match="remains in Cache Mode"):
        set_operating_mode(config, OperatingMode.OFFLINE)

    assert operating_mode(config) is OperatingMode.CACHE
    assert cached_proxy.is_file() and uncached_proxy.is_file()


@pytest.mark.parametrize(
    "mode", [OperatingMode.CACHE, OperatingMode.CONNECTED, OperatingMode.OFFLINE]
)
def test_same_mode_reentry_reports_es_not_restarted(
    tmp_path: Path, _stub_es, mode: OperatingMode
) -> None:
    """Same-mode invariant: re-entering the already-active mode never
    restarts ES, so the report must say so explicitly (no manual-refresh
    reminder is warranted when nothing about the presentation changed)."""
    config = _config(tmp_path)
    _library(config)
    write_operating_mode(config, mode)

    report = set_operating_mode(config, mode)

    assert report.es_restarted is False
    assert report.mode_changed is False


def test_operating_mode_lock_still_serializes_backend_transitions(tmp_path: Path) -> None:
    config = _config(tmp_path)
    contender_started = Event()
    contender_entered = Event()

    def contend() -> None:
        contender_started.set()
        with _operating_mode_lock(config):
            contender_entered.set()

    with _operating_mode_lock(config):
        thread = Thread(target=contend)
        thread.start()
        assert contender_started.wait(1)
        assert not contender_entered.wait(0.1)

    thread.join(timeout=2)
    assert not thread.is_alive()
    assert contender_entered.is_set()


@pytest.mark.parametrize("selected", list(OperatingMode))
def test_restart_recovery_reconciles_each_persisted_mode(
    tmp_path: Path, selected: OperatingMode
) -> None:
    config = _config(tmp_path)
    (_cached, cached_proxy, _), (_uncached, uncached_proxy, _) = _library(config)
    write_operating_mode(config, selected)

    reconcile_game_access(config, refresh_es=False, render_library_metadata=False)

    link = Path(config.local_roms_path) / "snes" / LINK_NAME
    if selected is OperatingMode.CONNECTED:
        assert link.is_symlink() and not cached_proxy.exists() and not uncached_proxy.exists()
    elif selected is OperatingMode.CACHE:
        assert not link.exists() and cached_proxy.is_file() and uncached_proxy.is_file()
    else:
        assert not link.exists() and cached_proxy.is_file() and not uncached_proxy.exists()


def test_cli_exposes_three_modes(tmp_path: Path) -> None:
    config = _config(tmp_path)
    _library(config)
    config_path = tmp_path / "config" / "romcloud.toml"
    write_config(config, str(config_path))
    runner = CliRunner()

    for command, expected in (
        ("offline", "Offline Mode"),
        ("cache", "Cache Mode"),
        ("connected", "Connected Mode"),
    ):
        result = runner.invoke(cli, ["--config", str(config_path), "library", command])
        assert result.exit_code == 0, result.output
        assert expected in result.output


def _game_with_cache_entry(
    config: AppConfig,
    title: str,
    *,
    status: CacheStatus = CacheStatus.COMPLETE,
    write_cached_file: bool = True,
    container_dir: bool = False,
) -> tuple[Game, Path]:
    """Catalog + cache a game without registering a `.romcloud` proxy.

    Simulates a cache-complete game whose proxy registration was never
    created (e.g. an interrupted catalog refresh) — the scenario that
    exposed the Offline Mode presentation bug on real hardware.
    """
    db = Database(str(Path(config.data_path) / "catalog.db"))
    db.initialize()
    filename = f"{title}.sfc"
    asset = GameAsset(
        filename=filename,
        relative_path=f"snes/{filename}",
        size_bytes=4,
        is_primary=True,
    )
    game = Game.create("snes", title, "local", config.source.rom_root, [asset])
    GameRepository(db).save(game)

    if container_dir:
        cache_path = Path(config.cache.path) / "snes" / title
        cached_file = cache_path / filename
    else:
        cache_path = Path(config.cache.path) / "snes" / filename
        cached_file = cache_path

    if write_cached_file:
        cached_file.parent.mkdir(parents=True, exist_ok=True)
        cached_file.write_bytes(b"data")

    entry = CacheEntry.create(game.id, str(cache_path))
    entry.status = status
    entry.size_bytes = 4
    CacheRepository(db).save(entry)
    return game, cached_file


def test_offline_exposes_cache_complete_game_with_no_proxy_record(tmp_path: Path) -> None:
    """Required invariant: a complete cache entry whose resolved launch
    asset exists is locally playable and visible Offline, even when no
    `.romcloud` proxy was ever registered for it."""
    config = _config(tmp_path)
    game, _cached_file = _game_with_cache_entry(config, "Aggressive Inline (USA)")
    db = Database(str(Path(config.data_path) / "catalog.db"))
    assert ProxyRepository(db).get(game.id) is None  # no registration yet

    set_operating_mode(config, OperatingMode.OFFLINE)

    proxy = Path(config.local_roms_path) / "snes" / "Aggressive Inline (USA).romcloud"
    assert proxy.is_file()
    assert ProxyRepository(db).get(game.id) is not None


def test_offline_exposes_directory_container_cached_game_with_no_proxy_record(
    tmp_path: Path,
) -> None:
    """Directory/container-backed cache layouts (e.g. Xbox) must resolve
    through the same canonical launch-asset lookup as single-file games."""
    config = _config(tmp_path)
    game, cached_file = _game_with_cache_entry(
        config, "Container Game", container_dir=True
    )
    assert cached_file.is_file()

    set_operating_mode(config, OperatingMode.OFFLINE)

    proxy = Path(config.local_roms_path) / "snes" / "Container Game.romcloud"
    assert proxy.is_file()
    db = Database(str(Path(config.data_path) / "catalog.db"))
    assert ProxyRepository(db).get(game.id) is not None


@pytest.mark.parametrize(
    ("status", "write_cached_file"),
    [
        (CacheStatus.TRANSFERRING, True),
        (CacheStatus.FAILED, True),
        (CacheStatus.COMPLETE, False),
    ],
)
def test_offline_hides_invalid_or_missing_cache_entries(
    tmp_path: Path, status: CacheStatus, write_cached_file: bool
) -> None:
    config = _config(tmp_path)
    game, _ = _game_with_cache_entry(
        config, "Broken", status=status, write_cached_file=write_cached_file
    )

    set_operating_mode(config, OperatingMode.OFFLINE)

    proxy = Path(config.local_roms_path) / "snes" / "Broken.romcloud"
    assert not proxy.exists()
    db = Database(str(Path(config.data_path) / "catalog.db"))
    assert Container(config).cache_repo.get(game.id) is not None
