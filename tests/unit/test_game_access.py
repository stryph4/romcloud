from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

import romcloud.cli.commands.cache as cache_module
from romcloud.bootstrap.container import Container
from romcloud.cli.commands.cache import cache_group
from romcloud.core.models.game import Game, GameAsset
from romcloud.core.models.proxy import ProxyRecord
from romcloud.infrastructure.config import (
    AppConfig,
    CacheConfig,
    DIRECT_NAS_MODE,
    SMART_CACHE_MODE,
    SourceConfig,
    load_config,
    write_config,
)
from romcloud.core.exceptions import ConfigurationError
from romcloud.infrastructure.database import Database
from romcloud.core.capabilities import OperatingMode
from romcloud.infrastructure.library_view import write_operating_mode
from romcloud.infrastructure.repositories.game import GameRepository
from romcloud.infrastructure.repositories.proxy import ProxyRepository
from romcloud.integrations.batocera.game_access import (
    DirectLinkConflictError,
    LINK_NAME,
    reconcile_direct_links,
    reconcile_game_access,
    remove_direct_links,
)
from romcloud.lifecycle import manage


@pytest.fixture(autouse=True)
def _stub_es_refresh(monkeypatch):
    monkeypatch.setattr(
        "romcloud.integrations.batocera.game_access._refresh_emulationstation",
        lambda config, systems, **kwargs: None,
    )


def _config(tmp_path: Path, mode: str = DIRECT_NAS_MODE) -> AppConfig:
    source = tmp_path / "source"
    local = tmp_path / "roms"
    source.mkdir(parents=True)
    local.mkdir(parents=True)
    return AppConfig(
        source=SourceConfig("local", str(source)),
        cache=CacheConfig(str(tmp_path / "cache")),
        local_roms_path=str(local),
        data_path=str(tmp_path / "data"),
        game_access_mode=mode,
    )


def _system(config: AppConfig, name: str = "snes") -> tuple[Path, Path]:
    source = Path(config.source.rom_root) / name
    local = Path(config.local_roms_path) / name
    source.mkdir()
    local.mkdir()
    return source, local


def test_existing_config_defaults_to_smart_cache_and_direct_round_trips(tmp_path: Path) -> None:
    path = tmp_path / "romcloud.toml"
    path.write_text('[source]\nprovider = "local"\nrom_root = "/roms"\n')
    assert load_config(str(path)).game_access_mode == SMART_CACHE_MODE

    config = _config(tmp_path / "roundtrip")
    path = tmp_path / "roundtrip.toml"
    write_config(config, str(path))
    assert load_config(str(path)).game_access_mode == DIRECT_NAS_MODE


def test_direct_config_rejects_source_overlapping_batocera_rom_root(tmp_path: Path) -> None:
    path = tmp_path / "romcloud.toml"
    path.write_text(
        '[source]\nprovider = "local"\nrom_root = "/userdata/roms"\n\n'
        '[game_access]\nmode = "direct_nas"\n\n'
        '[local_roms]\npath = "/userdata/roms"\n'
    )
    with pytest.raises(ConfigurationError, match="must not overlap"):
        load_config(str(path))


def test_direct_link_coexists_with_local_roms_and_is_idempotent(tmp_path: Path) -> None:
    config = _config(tmp_path)
    source, local = _system(config)
    local_game = local / "Local Game.sfc"
    local_game.write_bytes(b"local")

    first = reconcile_direct_links(config, ["snes"])
    second = reconcile_direct_links(config, ["snes"])

    link = local / LINK_NAME
    assert first.created == 1 and second.created == 0
    assert link.is_symlink()
    assert Path(os.readlink(link)) == source
    assert local_game.read_bytes() == b"local"


def test_direct_catalog_writes_no_proxies_or_remote_metadata(tmp_path: Path) -> None:
    config = _config(tmp_path)
    source, local = _system(config)
    (source / "Cloud Game.sfc").write_bytes(b"rom")
    gamelist = source / "gamelist.xml"
    original = b"<gameList><game><name>NAS metadata</name></game></gameList>"
    gamelist.write_bytes(original)

    result = Container(config).catalog.refresh()
    reconcile_game_access(config)

    assert not result.errors
    assert list(local.glob("*.romcloud")) == []
    assert gamelist.read_bytes() == original
    assert (local / LINK_NAME).is_symlink()


@pytest.mark.parametrize("foreign_kind", ["directory", "symlink"])
def test_direct_link_conflict_is_never_replaced(tmp_path: Path, foreign_kind: str) -> None:
    config = _config(tmp_path)
    _source, local = _system(config)
    reserved = local / LINK_NAME
    if foreign_kind == "directory":
        reserved.mkdir()
    else:
        foreign = tmp_path / "foreign"
        foreign.mkdir()
        reserved.symlink_to(foreign, target_is_directory=True)

    with pytest.raises(DirectLinkConflictError, match="not a verified ROMCloud-owned symlink|not a ROMCloud-owned symlink"):
        reconcile_direct_links(config, ["snes"])

    assert reserved.exists() or reserved.is_symlink()


def test_cleanup_refuses_changed_link_and_never_follows_it(tmp_path: Path) -> None:
    config = _config(tmp_path)
    _source, local = _system(config)
    reconcile_direct_links(config, ["snes"])
    link = local / LINK_NAME
    link.unlink()
    foreign = tmp_path / "foreign"
    foreign.mkdir()
    sentinel = foreign / "keep.rom"
    sentinel.write_bytes(b"keep")
    link.symlink_to(foreign, target_is_directory=True)

    report = remove_direct_links(config)

    assert report.removed == 0
    assert link.is_symlink() and sentinel.exists()


def test_reconfigure_updates_only_the_verified_owned_target(tmp_path: Path) -> None:
    config = _config(tmp_path)
    old_source, local = _system(config)
    reconcile_direct_links(config, ["snes"])
    new_root = tmp_path / "new-source"
    (new_root / "snes").mkdir(parents=True)
    reconfigured = replace(
        config, source=SourceConfig("local", str(new_root))
    )

    report = reconcile_direct_links(reconfigured, ["snes"])

    link = local / LINK_NAME
    assert report.created == 1 and report.removed == 1
    assert Path(os.readlink(link)) == new_root / "snes"
    assert old_source.exists()


def test_mode_switch_removes_only_owned_link_and_restores_proxy(tmp_path: Path) -> None:
    direct = _config(tmp_path)
    _source, local = _system(direct)
    db = Database(str(Path(direct.data_path) / "catalog.db"))
    db.initialize()
    game = Game.create(
        system="snes",
        title="Cloud Game",
        source_provider="local",
        source_root=direct.source.rom_root,
        assets=[GameAsset("Cloud Game.sfc", "snes/Cloud Game.sfc", True)],
    )
    GameRepository(db).save(game)
    proxy = local / "Cloud Game.romcloud"
    ProxyRepository(db).save(ProxyRecord.create(game.id, str(proxy)))
    reconcile_direct_links(direct, ["snes"])
    local_game = local / "Local Game.sfc"
    local_game.write_bytes(b"local")

    smart = replace(direct, game_access_mode=SMART_CACHE_MODE)
    write_operating_mode(smart, OperatingMode.CACHE)
    reconcile_game_access(smart)

    assert not (local / LINK_NAME).exists()
    assert proxy.is_file()
    assert json.loads(proxy.read_text())["game_id"] == game.id
    assert local_game.exists()


def test_large_existing_library_survives_both_mode_transitions(tmp_path: Path) -> None:
    """A thousands-scale cache-backed install switches without data loss."""
    smart = _config(tmp_path, SMART_CACHE_MODE)
    systems = ("snes", "psx")
    local_dirs: dict[str, Path] = {}
    for system in systems:
        _source, local_dirs[system] = _system(smart, system)

    db = Database(str(Path(smart.data_path) / "catalog.db"))
    db.initialize()
    timestamp = datetime.now(timezone.utc).isoformat()
    proxy_count = 2048
    game_rows = []
    asset_rows = []
    proxy_rows = []
    owned_paths: list[Path] = []
    for index in range(proxy_count):
        system = systems[index % len(systems)]
        game_id = f"game-{index:04d}"
        title = f"Cloud Game {index:04d}"
        relative_path = f"{system}/{title}.rom"
        proxy = local_dirs[system] / f"{title}.romcloud"
        proxy.write_text(
            json.dumps(
                {
                    "romcloud_version": "1",
                    "game_id": game_id,
                    "title": title,
                    "system": system,
                    "source_provider": "local",
                    "source_root": smart.source.rom_root,
                    "assets": [
                        {
                            "filename": f"{title}.rom",
                            "relative_path": relative_path,
                            "is_primary": True,
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        owned_paths.append(proxy)
        game_rows.append(
            (
                game_id,
                system,
                title,
                "local",
                smart.source.rom_root,
                None,
                timestamp,
            )
        )
        asset_rows.append(
            (
                f"asset-{index:04d}",
                game_id,
                relative_path,
                f"{title}.rom",
                index + 1,
                1,
            )
        )
        proxy_rows.append((game_id, str(proxy), timestamp))

    cache_file = Path(smart.cache.path) / "snes" / "Cached Game.sfc"
    cache_file.parent.mkdir(parents=True)
    cache_file.write_bytes(b"preserve cached game bytes")
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
            """
            INSERT INTO proxy_records (game_id, proxy_path, created_at)
            VALUES (?, ?, ?)
            """,
            proxy_rows,
        )
        conn.execute(
            """
            INSERT INTO cache_entries
                (game_id, cache_path, status, cached_at, last_accessed,
                 size_bytes, is_pinned)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "game-0000",
                str(cache_file),
                "complete",
                timestamp,
                timestamp,
                cache_file.stat().st_size,
                1,
            ),
        )

    local_rom = local_dirs["snes"] / "My Local Game.sfc"
    local_rom.write_bytes(b"local game")
    foreign_proxy = local_dirs["psx"] / "Unrelated.romcloud"
    foreign_proxy.write_text('{"owner": "someone-else"}', encoding="utf-8")

    direct = replace(smart, game_access_mode=DIRECT_NAS_MODE)
    write_operating_mode(direct, OperatingMode.CONNECTED)
    first_direct = reconcile_game_access(direct)
    second_direct = reconcile_game_access(direct)

    assert first_direct.created == len(systems)
    assert second_direct.created == second_direct.removed == 0
    assert all(not path.exists() for path in owned_paths)
    assert all((local_dirs[system] / LINK_NAME).is_symlink() for system in systems)
    assert foreign_proxy.read_text(encoding="utf-8") == '{"owner": "someone-else"}'
    assert local_rom.read_bytes() == b"local game"
    assert cache_file.read_bytes() == b"preserve cached game bytes"
    with db.connect() as conn:
        cached = conn.execute(
            "SELECT cache_path, status, is_pinned FROM cache_entries WHERE game_id = ?",
            ("game-0000",),
        ).fetchone()
    assert tuple(cached) == (str(cache_file), "complete", 1)

    write_operating_mode(smart, OperatingMode.CACHE)
    first_smart = reconcile_game_access(smart)
    second_smart = reconcile_game_access(smart)

    assert first_smart.removed == len(systems)
    assert second_smart.created == second_smart.removed == 0
    assert all(path.is_file() for path in owned_paths)
    assert all(not (local_dirs[system] / LINK_NAME).exists() for system in systems)
    assert foreign_proxy.exists() and local_rom.exists() and cache_file.exists()
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM cache_entries").fetchone()[0] == 1


def test_uninstall_unlinks_verified_direct_link_and_preserves_roms(tmp_path: Path, monkeypatch) -> None:
    config = _config(tmp_path)
    _source, local = _system(config)
    reconcile_direct_links(config, ["snes"])
    local_game = local / "Mine.sfc"
    local_game.write_bytes(b"mine")
    monkeypatch.setattr(manage.mount_worker, "stop_worker", lambda home: False)
    monkeypatch.setattr(manage.mount_worker, "configured_mounts", lambda cfg: [])
    monkeypatch.setattr(manage.mount_worker, "cleanup_runtime_state", lambda home: None)
    monkeypatch.setattr(manage.mount_service, "remove_service", lambda: False)
    monkeypatch.setattr(manage.es_config, "remove", lambda: False)
    monkeypatch.setattr(manage.ports_gamelist_config, "remove", lambda **kwargs: False)

    report = manage.uninstall(config=config, romcloud_home=tmp_path / "home", ports_dir=tmp_path / "ports")

    assert report.direct_links_removed == 1
    assert not (local / LINK_NAME).exists()
    assert local_game.exists()


def test_cache_cli_requires_per_command_override_in_direct_mode(monkeypatch) -> None:
    container = SimpleNamespace(
        config=SimpleNamespace(game_access_mode=DIRECT_NAS_MODE),
        cache_repo=SimpleNamespace(list_complete=lambda: []),
    )
    monkeypatch.setattr(cache_module, "get_container", lambda ctx: container)
    runner = CliRunner()

    blocked = runner.invoke(cache_group, ["status"], obj={})
    allowed = runner.invoke(cache_group, ["--override", "status"], obj={})

    assert blocked.exit_code == 1
    assert "unavailable in Connected Mode" in blocked.output
    assert allowed.exit_code == 0 and "Cache is empty" in allowed.output
    assert container.config.game_access_mode == DIRECT_NAS_MODE
