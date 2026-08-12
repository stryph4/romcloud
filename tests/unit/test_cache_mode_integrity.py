"""Release-blocking Cache Mode presentation/launch integrity regressions."""

from __future__ import annotations

import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import pytest

from romcloud.bootstrap.container import Container
from romcloud.core.capabilities import OperatingMode
from romcloud.core.models.cache import CacheEntry, CacheStatus
from romcloud.core.models.game import Game, GameAsset
from romcloud.core.models.proxy import ProxyRecord
from romcloud.infrastructure.config import AppConfig, CacheConfig, SourceConfig
from romcloud.infrastructure.database import Database
from romcloud.infrastructure.providers.local import LocalFilesystemProvider
from romcloud.infrastructure.repositories.cache import CacheRepository
from romcloud.infrastructure.repositories.game import GameRepository
from romcloud.infrastructure.repositories.proxy import ProxyRepository
from romcloud.integrations.batocera.game_access import (
    DirectLinkReport,
    reconcile_library_presentation,
    set_operating_mode,
)
from romcloud.lifecycle.manage import restore_owned_proxies


SYSTEM_COUNTS = {
    "dreamcast": 562,
    "gamecube": 1218,
    "ps2": 4134,
    "psp": 1242,
    "psx": 3122,
    "saturn": 462,
    "wii": 352,
    "xbox": 1758,
    "xbox360": 10,
}
CATALOG_SIZE = 12_860
INITIAL_PROXY_RECORDS = 6_434
INITIAL_MISSING_RECORDS = CATALOG_SIZE - INITIAL_PROXY_RECORDS


@pytest.fixture(autouse=True)
def _isolate_emulationstation(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "romcloud.integrations.batocera.game_access._refresh_emulationstation",
        lambda config, systems, **kwargs: None,
    )
    monkeypatch.setattr(
        "romcloud.integrations.batocera.game_access._reload_emulationstation",
        lambda: True,
    )


def _config(tmp_path: Path, *, source_exists: bool = True) -> AppConfig:
    source = tmp_path / "source"
    local = tmp_path / "roms"
    cache = tmp_path / "cache"
    if source_exists:
        (source / "snes").mkdir(parents=True)
    local.mkdir()
    cache.mkdir()
    return AppConfig(
        source=SourceConfig("local", str(source)),
        cache=CacheConfig(str(cache)),
        local_roms_path=str(local),
        data_path=str(tmp_path / "data"),
    )


def _database(config: AppConfig) -> Database:
    db = Database(str(Path(config.data_path) / "catalog.db"))
    db.initialize()
    return db


def _game(
    config: AppConfig,
    title: str,
    *,
    persisted_source_root: Path | None = None,
    content: bytes = b"data",
) -> Game:
    filename = f"{title}.sfc"
    game = Game.create(
        "snes",
        title,
        "local",
        str(persisted_source_root or config.source.rom_root),
        [
            GameAsset(
                filename=filename,
                relative_path=f"snes/{filename}",
                size_bytes=len(content),
                is_primary=True,
            )
        ],
    )
    GameRepository(_database(config)).save(game)
    return game


def _register(config: AppConfig, game: Game) -> Path:
    path = Path(config.local_roms_path) / game.system / f"{game.title}.romcloud"
    ProxyRepository(_database(config)).save(ProxyRecord.create(game.id, str(path)))
    return path


def _complete_cache(config: AppConfig, game: Game, content: bytes = b"data") -> Path:
    asset = game.primary_asset
    assert asset is not None
    path = Path(config.cache.path) / game.system / asset.filename
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    entry = CacheEntry.create(game.id, str(path))
    entry.status = CacheStatus.COMPLETE
    entry.size_bytes = len(content)
    CacheRepository(_database(config)).save(entry)
    return path


def _run_proxy_launch(
    monkeypatch: pytest.MonkeyPatch, config: AppConfig, proxy_path: Path
) -> str:
    import romcloud.infrastructure.config as config_module
    import romcloud.integrations.batocera.launcher as launcher_module
    import romcloud.ui.graphical_progress as graphical_progress

    monkeypatch.setattr(config_module, "load_config", lambda: config)
    monkeypatch.setattr(graphical_progress, "graphical_progress_binary", lambda cfg: None)
    monkeypatch.setattr(launcher_module.sys.stdout, "isatty", lambda: False)
    return launcher_module._resolve_and_cache(str(proxy_path))


def _seed_production_state(config: AppConfig) -> str:
    """Seed the exact hardware counts using historical/current-root pairs.

    Every source-root pair derives the same presentation path. All historical
    identities have the default registration; four current identities already
    have disambiguated registrations, producing 6,434 existing rows and 6,426
    missing rows across the 12,860-game catalog.
    """
    db = _database(config)
    now = datetime.now(timezone.utc).isoformat()
    historical_root = str(Path(config.source.rom_root).with_name("romcloud-source"))
    game_rows: list[tuple] = []
    asset_rows: list[tuple] = []
    proxy_rows: list[tuple] = []
    global_index = 0
    extra_registered = 0
    cached_game_id = ""

    for system, total in SYSTEM_COUNTS.items():
        per_root = total // 2
        for index in range(per_root):
            title = f"{system} Game {index:05d}"
            filename = f"{title}.rom"
            relative_path = f"{system}/{filename}"
            old_id = f"a{global_index:07d}"
            new_id = f"b{global_index:07d}"
            if not cached_game_id:
                cached_game_id = old_id

            game_rows.extend(
                [
                    (old_id, system, title, "local", historical_root, None, now),
                    (new_id, system, title, "local", config.source.rom_root, None, now),
                ]
            )
            asset_rows.extend(
                [
                    (
                        f"asset-old-{global_index}",
                        old_id,
                        relative_path,
                        filename,
                        4,
                        1,
                    ),
                    (
                        f"asset-new-{global_index}",
                        new_id,
                        relative_path,
                        filename,
                        4,
                        1,
                    ),
                ]
            )
            default_path = Path(config.local_roms_path) / system / f"{title}.romcloud"
            proxy_rows.append((old_id, str(default_path), now))
            if extra_registered < INITIAL_PROXY_RECORDS - CATALOG_SIZE // 2:
                suffix_path = default_path.with_name(
                    f"{default_path.stem}.{new_id[:8]}.romcloud"
                )
                proxy_rows.append((new_id, str(suffix_path), now))
                extra_registered += 1
            global_index += 1

    with db.connect() as conn:
        conn.executemany(
            """
            INSERT INTO games
                (id, system, title, source_provider, source_root, last_played, added_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            game_rows,
        )
        conn.executemany(
            """
            INSERT INTO game_assets
                (id, game_id, relative_path, filename, size_bytes, is_primary)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            asset_rows,
        )
        conn.executemany(
            "INSERT INTO proxy_records (game_id, proxy_path, created_at) VALUES (?, ?, ?)",
            proxy_rows,
        )

    cached_game = GameRepository(db).get(cached_game_id)
    assert cached_game is not None
    _complete_cache(config, cached_game)
    return cached_game_id


def _invariant_counts(config: AppConfig) -> dict[str, int | Counter]:
    container = Container(config)
    games = container.game_repo.list_all()
    games_by_id = {game.id: game for game in games}
    records = container.proxy_repo.list_all()
    records_by_id = {record.game_id: record for record in records}
    proxy_files = list(Path(config.local_roms_path).rglob("*.romcloud"))
    payload_ids: dict[Path, str | None] = {}
    for path in proxy_files:
        payload_ids[path] = json.loads(path.read_text(encoding="utf-8")).get("game_id")

    complete = container.cache_repo.list_complete()
    valid = [
        entry
        for entry in complete
        if container.cache.is_valid_cached_entry(entry, games_by_id.get(entry.game_id))
    ]
    cached_with_record = [entry for entry in valid if entry.game_id in records_by_id]
    cached_with_file = [
        entry
        for entry in cached_with_record
        if payload_ids.get(Path(records_by_id[entry.game_id].proxy_path)) == entry.game_id
    ]
    coherent_files = sum(
        payload_ids.get(Path(record.proxy_path)) == record.game_id for record in records
    )
    return {
        "games": len(games),
        "systems": Counter(game.system for game in games),
        "proxy_records": len(records),
        "proxy_files": len(proxy_files),
        "coherent_proxy_files": coherent_files,
        "missing_proxy_records": len(set(games_by_id) - set(records_by_id)),
        "complete_cache_entries": len(complete),
        "valid_cached_assets": len(valid),
        "cached_games_with_proxy_record": len(cached_with_record),
        "cached_games_without_proxy_record": len(valid) - len(cached_with_record),
        "cached_games_with_proxy_file": len(cached_with_file),
        "cached_games_without_proxy_file": len(valid) - len(cached_with_file),
    }


def test_production_sized_cache_reconcile_converges(tmp_path: Path) -> None:
    config = _config(tmp_path, source_exists=False)
    _seed_production_state(config)

    before = _invariant_counts(config)
    assert before == {
        "games": CATALOG_SIZE,
        "systems": Counter(SYSTEM_COUNTS),
        "proxy_records": INITIAL_PROXY_RECORDS,
        "proxy_files": 0,
        "coherent_proxy_files": 0,
        "missing_proxy_records": INITIAL_MISSING_RECORDS,
        "complete_cache_entries": 1,
        "valid_cached_assets": 1,
        "cached_games_with_proxy_record": 1,
        "cached_games_without_proxy_record": 0,
        "cached_games_with_proxy_file": 0,
        "cached_games_without_proxy_file": 1,
    }

    first = reconcile_library_presentation(config, offline=False)
    after_first = _invariant_counts(config)
    mtimes = {
        path: path.stat().st_mtime_ns
        for path in Path(config.local_roms_path).rglob("*.romcloud")
    }
    second = reconcile_library_presentation(config, offline=False)

    assert first.visible == CATALOG_SIZE
    assert first.restored == CATALOG_SIZE
    assert after_first == {
        "games": CATALOG_SIZE,
        "systems": Counter(SYSTEM_COUNTS),
        "proxy_records": CATALOG_SIZE,
        "proxy_files": CATALOG_SIZE,
        "coherent_proxy_files": CATALOG_SIZE,
        "missing_proxy_records": 0,
        "complete_cache_entries": 1,
        "valid_cached_assets": 1,
        "cached_games_with_proxy_record": 1,
        "cached_games_without_proxy_record": 0,
        "cached_games_with_proxy_file": 1,
        "cached_games_without_proxy_file": 0,
    }
    assert second.visible == CATALOG_SIZE
    assert second.restored == 0
    assert _invariant_counts(config) == after_first
    assert {
        path: path.stat().st_mtime_ns
        for path in Path(config.local_roms_path).rglob("*.romcloud")
    } == mtimes


def test_cached_launch_after_cache_transition_is_source_independent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path, source_exists=False)
    historical_root = tmp_path / "romcloud-source"
    game = _game(config, "Cached Game", persisted_source_root=historical_root)
    cached_path = _complete_cache(config, game)

    set_operating_mode(config, OperatingMode.CACHE)
    record = ProxyRepository(_database(config)).get(game.id)
    assert record is not None

    def fail_reachability(self, root):  # noqa: ANN001
        raise AssertionError("a valid cached launch must not inspect the source")

    monkeypatch.setattr(LocalFilesystemProvider, "is_reachable", fail_reachability)
    result = _run_proxy_launch(monkeypatch, config, Path(record.proxy_path))

    assert result == str(cached_path)
    assert Path(result).is_file()


def test_uncached_launch_uses_current_source_root_and_records_completion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path)
    content = b"downloaded-from-current-root"
    historical_root = tmp_path / "romcloud-source"
    game = _game(
        config,
        "Uncached Game",
        persisted_source_root=historical_root,
        content=content,
    )
    source_file = Path(config.source.rom_root) / "snes" / "Uncached Game.sfc"
    source_file.write_bytes(content)
    set_operating_mode(config, OperatingMode.CACHE)
    record = ProxyRepository(_database(config)).get(game.id)
    assert record is not None

    result = _run_proxy_launch(monkeypatch, config, Path(record.proxy_path))
    entry = CacheRepository(_database(config)).get(game.id)

    assert Path(result).read_bytes() == content
    assert historical_root.exists() is False
    assert entry is not None and entry.status is CacheStatus.COMPLETE
    assert Container(config).cache.get_launch_path(game.id) == result


def test_cache_connected_cache_round_trip_preserves_ownership_and_converges(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path)
    historical = _game(config, "Same Title", persisted_source_root=tmp_path / "old-root")
    current = _game(config, "Same Title")
    _register(config, historical)
    monkeypatch.setattr(
        "romcloud.integrations.batocera.game_access.reconcile_direct_links",
        lambda *args, **kwargs: DirectLinkReport(),
    )
    monkeypatch.setattr(
        "romcloud.integrations.batocera.game_access.remove_direct_links",
        lambda *args, **kwargs: DirectLinkReport(),
    )

    set_operating_mode(config, OperatingMode.CACHE)
    assert len(ProxyRepository(_database(config)).list_all()) == 2
    assert len(list(Path(config.local_roms_path).rglob("*.romcloud"))) == 2

    set_operating_mode(config, OperatingMode.CONNECTED)
    assert len(ProxyRepository(_database(config)).list_all()) == 2
    assert list(Path(config.local_roms_path).rglob("*.romcloud")) == []

    returned = set_operating_mode(config, OperatingMode.CACHE)
    repeated = set_operating_mode(config, OperatingMode.CACHE)
    records = ProxyRepository(_database(config)).list_all()
    assert returned.restored == 2
    assert repeated.restored == 0
    assert {record.game_id for record in records} == {historical.id, current.id}
    assert len(list(Path(config.local_roms_path).rglob("*.romcloud"))) == 2


def test_cache_offline_cache_round_trip_and_offline_launch_are_source_independent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path)
    cached = _game(config, "Cached", persisted_source_root=tmp_path / "old-root")
    uncached = _game(config, "Uncached")
    cached_path = _complete_cache(config, cached)

    set_operating_mode(config, OperatingMode.CACHE)
    set_operating_mode(config, OperatingMode.OFFLINE)
    repo = ProxyRepository(_database(config))
    cached_record = repo.get(cached.id)
    uncached_record = repo.get(uncached.id)
    assert cached_record is not None and Path(cached_record.proxy_path).is_file()
    assert uncached_record is not None and not Path(uncached_record.proxy_path).exists()
    assert len(repo.list_all()) == 2

    def fail_reachability(self, root):  # noqa: ANN001
        raise AssertionError("Offline cached launch must not inspect the source")

    monkeypatch.setattr(LocalFilesystemProvider, "is_reachable", fail_reachability)
    assert _run_proxy_launch(monkeypatch, config, Path(cached_record.proxy_path)) == str(
        cached_path
    )

    returned = set_operating_mode(config, OperatingMode.CACHE)
    repeated = set_operating_mode(config, OperatingMode.CACHE)
    assert returned.restored == 1
    assert repeated.restored == 0
    assert len(ProxyRepository(_database(config)).list_all()) == 2
    assert len(list(Path(config.local_roms_path).rglob("*.romcloud"))) == 2


def test_restore_owned_proxies_registers_and_materializes_colliding_titles(
    tmp_path: Path,
) -> None:
    """Directly pin the shared ownership invariant below mode orchestration."""
    config = _config(tmp_path)
    first = _game(config, "Collision", persisted_source_root=tmp_path / "old-root")
    second = _game(config, "Collision")
    first_path = _register(config, first)

    assert restore_owned_proxies(config) == 2
    repo = ProxyRepository(_database(config))
    first_record = repo.get(first.id)
    second_record = repo.get(second.id)
    assert first_record is not None and Path(first_record.proxy_path) == first_path
    assert second_record is not None and second_record.proxy_path != first_record.proxy_path
    assert json.loads(first_path.read_text(encoding="utf-8"))["game_id"] == first.id
    assert json.loads(Path(second_record.proxy_path).read_text(encoding="utf-8"))[
        "game_id"
    ] == second.id
    assert restore_owned_proxies(config) == 0
