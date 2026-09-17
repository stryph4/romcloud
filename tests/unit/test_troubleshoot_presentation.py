from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from romcloud.core.capabilities import OperatingMode
from romcloud.infrastructure.config import AppConfig, CacheConfig, SourceConfig, write_config
from romcloud.infrastructure.database import Database
from romcloud.infrastructure.library_view import write_operating_mode
from romcloud.troubleshoot import (
    ActivitySnapshot,
    ActivityState,
    TroubleshootPaths,
    collect_diagnostics,
    run_quick_repair,
)


def _setup(tmp_path: Path, mode: OperatingMode) -> tuple[Path, AppConfig, Database]:
    home = tmp_path / "home"
    source = tmp_path / "source"
    roms = tmp_path / "roms"
    cache = tmp_path / "cache"
    data = home / "data"
    for path in (source, roms, cache, data):
        path.mkdir(parents=True)
    config = AppConfig(
        source=SourceConfig("local", str(source)),
        cache=CacheConfig(str(cache), min_free_gb=0),
        local_roms_path=str(roms),
        data_path=str(data),
    )
    config_path = home / "config" / "romcloud.toml"
    config_path.parent.mkdir(parents=True)
    write_config(config, str(config_path))
    database = Database(str(data / "catalog.db"))
    database.initialize()
    write_operating_mode(config, mode)
    return config_path, config, database


def _paths(tmp_path: Path) -> TroubleshootPaths:
    es = tmp_path / "es"
    return TroubleshootPaths(
        auto_savesync_hook=tmp_path / "system" / "hook",
        mount_service=tmp_path / "system" / "service",
        services_config=tmp_path / "system" / "batocera.conf",
        es_stock=es / "es_systems.cfg",
        es_override=es / "es_systems_romcloud.cfg",
        es_user_config_dir=es,
        es_system_config_dir=es,
        es_legacy_config_dir=tmp_path / "legacy",
    )


def _inactive() -> ActivitySnapshot:
    value = ActivityState("inactive")
    return ActivitySnapshot(value, value, value, value, value, value)


def _game(database: Database, config: AppConfig, game_id: str, proxy: Path) -> None:
    with database.connect() as conn:
        conn.execute(
            "INSERT INTO games "
            "(id, system, title, source_provider, source_root, added_at, is_eligible) "
            "VALUES (?, 'snes', ?, 'local', ?, 'now', 1)",
            (game_id, game_id, config.source.rom_root),
        )
        conn.execute(
            "INSERT INTO game_assets "
            "(id, game_id, relative_path, filename, is_primary) "
            "VALUES (?, ?, ?, ?, 1)",
            (f"asset-{game_id}", game_id, f"snes/{game_id}.sfc", f"{game_id}.sfc"),
        )
        conn.execute(
            "INSERT INTO proxy_records (game_id, proxy_path, created_at) "
            "VALUES (?, ?, 'now')",
            (game_id, str(proxy)),
        )


def _managed_system(
    database: Database, config: AppConfig, system: str = "snes"
) -> None:
    with database.connect() as conn:
        conn.execute(
            "INSERT INTO games "
            "(id, system, title, source_provider, source_root, added_at, is_eligible) "
            "VALUES (?, ?, ?, 'local', ?, 'now', 1)",
            (f"managed-{system}", system, system, config.source.rom_root),
        )


def _complete_cache(
    database: Database,
    config: AppConfig,
    game_id: str,
    *,
    create_primary: bool = True,
    missing_required_member: bool = False,
    invalid_required_member: bool = False,
) -> Path:
    primary = Path(config.cache.path) / "snes" / f"{game_id}.sfc"
    if create_primary:
        primary.parent.mkdir(parents=True, exist_ok=True)
        primary.write_bytes(b"data")
    with database.connect() as conn:
        conn.execute(
            "INSERT INTO cache_entries "
            "(game_id, cache_path, status, cached_at, last_accessed, size_bytes, "
            "membership_resolved) VALUES (?, ?, 'complete', 'now', 'now', 4, 1)",
            (game_id, str(primary)),
        )
        conn.execute(
            "INSERT INTO cache_members "
            "(game_id, relative_path, expected_size, size_bytes, is_primary) "
            "VALUES (?, ?, 4, 4, 1)",
            (game_id, f"snes/{game_id}.sfc"),
        )
        if missing_required_member or invalid_required_member:
            required = primary.with_suffix(".bin")
            if invalid_required_member:
                required.write_bytes(b"data")
            conn.execute(
                "INSERT INTO cache_members "
                "(game_id, relative_path, expected_size, size_bytes, is_primary) "
                "VALUES (?, ?, ?, 4, 0)",
                (
                    game_id,
                    f"snes/{game_id}.bin",
                    5 if invalid_required_member else 4,
                ),
            )
    return primary


def _cache_state(database: Database, game_id: str) -> tuple[tuple, tuple]:
    with database.connect() as conn:
        entry = tuple(
            conn.execute(
                "SELECT status, membership_resolved FROM cache_entries "
                "WHERE game_id = ?",
                (game_id,),
            ).fetchone()
        )
        members = tuple(
            tuple(row)
            for row in conn.execute(
                "SELECT relative_path, expected_size, size_bytes, is_primary "
                "FROM cache_members WHERE game_id = ? ORDER BY relative_path",
                (game_id,),
            )
        )
    return entry, members


def _finding(report, finding_id: str):  # noqa: ANN001
    return next(item for item in report.findings if item.id == finding_id)


def test_cache_mode_ignores_direct_artifacts_and_restores_only_registered_proxy(
    tmp_path: Path, monkeypatch
) -> None:
    config_path, config, database = _setup(tmp_path, OperatingMode.CACHE)
    proxy = Path(config.local_roms_path) / "snes" / "Game.romcloud"
    _game(database, config, "game", proxy)
    monkeypatch.setattr("romcloud.troubleshoot._inspect_browser", lambda *_: None)

    report, _ = collect_diagnostics(config_path, paths=_paths(tmp_path), activity=_inactive())

    assert _finding(report, "presentation.proxy_missing").metadata["game_ids"] == ["game"]
    assert not any(item.id.startswith("presentation.direct_") for item in report.findings)

    repaired = run_quick_repair(config_path, paths=_paths(tmp_path), activity=_inactive())
    repeated = run_quick_repair(config_path, paths=_paths(tmp_path), activity=_inactive())

    assert json.loads(proxy.read_text(encoding="utf-8"))["game_id"] == "game"
    assert _finding(repaired, "presentation.proxy_missing").status == "fixed"
    assert not any(item.id == "presentation.proxy_missing" for item in repeated.findings)
    with database.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM proxy_records").fetchone()[0] == 1


def test_cache_mode_preserves_foreign_proxy_destination(tmp_path: Path, monkeypatch) -> None:
    config_path, config, database = _setup(tmp_path, OperatingMode.CACHE)
    proxy = Path(config.local_roms_path) / "snes" / "Game.romcloud"
    proxy.parent.mkdir(parents=True)
    proxy.write_bytes(b"foreign bytes")
    _game(database, config, "game", proxy)
    monkeypatch.setattr("romcloud.troubleshoot._inspect_browser", lambda *_: None)

    report = run_quick_repair(config_path, paths=_paths(tmp_path), activity=_inactive())

    assert _finding(report, "presentation.proxy_foreign").status == "error"
    assert proxy.read_bytes() == b"foreign bytes"


def test_offline_valid_cache_is_eligible_repairs_and_second_run_is_noop(
    tmp_path: Path, monkeypatch
) -> None:
    config_path, config, database = _setup(tmp_path, OperatingMode.OFFLINE)
    cached = Path(config.local_roms_path) / "snes" / "Cached.romcloud"
    remote_only = Path(config.local_roms_path) / "snes" / "Remote.romcloud"
    _game(database, config, "cached", cached)
    _game(database, config, "remote", remote_only)
    _complete_cache(database, config, "cached")
    monkeypatch.setattr("romcloud.troubleshoot._inspect_browser", lambda *_: None)

    report, _ = collect_diagnostics(config_path, paths=_paths(tmp_path), activity=_inactive())
    repaired = run_quick_repair(
        config_path, paths=_paths(tmp_path), activity=_inactive()
    )
    repeated = run_quick_repair(
        config_path, paths=_paths(tmp_path), activity=_inactive()
    )

    assert _finding(report, "presentation.proxy_missing").metadata["game_ids"] == ["cached"]
    assert not any(item.id.startswith("presentation.direct_") for item in report.findings)
    assert json.loads(cached.read_text(encoding="utf-8"))["game_id"] == "cached"
    assert _finding(repaired, "presentation.proxy_missing").status == "fixed"
    assert not any(item.id == "presentation.proxy_missing" for item in repeated.findings)
    assert not remote_only.exists()


@pytest.mark.parametrize(
    ("create_primary", "missing_required_member", "invalid_required_member"),
    ((False, False, False), (True, True, False), (True, False, True)),
    ids=(
        "missing-primary",
        "missing-required-member",
        "invalid-required-member",
    ),
)
def test_offline_invalid_cache_is_not_eligible_or_restored_and_db_is_unchanged(
    tmp_path: Path,
    monkeypatch,
    create_primary: bool,
    missing_required_member: bool,
    invalid_required_member: bool,
) -> None:
    config_path, config, database = _setup(tmp_path, OperatingMode.OFFLINE)
    proxy = Path(config.local_roms_path) / "snes" / "Broken.romcloud"
    _game(database, config, "broken", proxy)
    _complete_cache(
        database,
        config,
        "broken",
        create_primary=create_primary,
        missing_required_member=missing_required_member,
        invalid_required_member=invalid_required_member,
    )
    before = _cache_state(database, "broken")
    monkeypatch.setattr("romcloud.troubleshoot._inspect_browser", lambda *_: None)

    diagnostic, _ = collect_diagnostics(
        config_path, paths=_paths(tmp_path), activity=_inactive()
    )
    assert _cache_state(database, "broken") == before

    repaired = run_quick_repair(
        config_path, paths=_paths(tmp_path), activity=_inactive()
    )

    assert not any(
        item.id == "presentation.proxy_missing" for item in diagnostic.findings
    )
    assert not proxy.exists()
    assert not any(
        item.id == "presentation.proxy_missing" and item.status == "fixed"
        for item in repaired.findings
    )
    assert _cache_state(database, "broken") == before


@pytest.mark.skipif(os.name == "nt", reason="Direct-link repair requires POSIX symlinks")
def test_direct_mode_restores_only_missing_manifest_link_and_second_run_is_noop(
    tmp_path: Path, monkeypatch
) -> None:
    config_path, config, database = _setup(tmp_path, OperatingMode.CONNECTED)
    _managed_system(database, config)
    target = Path(config.source.rom_root) / "snes"
    target.mkdir()
    link = Path(config.local_roms_path) / "snes" / "ROMCloud"
    link.parent.mkdir()
    manifest = Path(config.data_path) / "direct-links.json"
    manifest.write_text(
        json.dumps({"version": 1, "links": [{"path": str(link), "target": str(target)}]}),
        encoding="utf-8",
    )
    monkeypatch.setattr("romcloud.troubleshoot._inspect_browser", lambda *_: None)

    first = run_quick_repair(config_path, paths=_paths(tmp_path), activity=_inactive())
    second = run_quick_repair(config_path, paths=_paths(tmp_path), activity=_inactive())

    assert link.is_symlink() and link.resolve() == target.resolve()
    assert _finding(first, "presentation.direct_link_missing").status == "fixed"
    assert not any(item.id == "presentation.direct_link_missing" for item in second.findings)


def test_direct_mode_preserves_foreign_file(tmp_path: Path, monkeypatch) -> None:
    config_path, config, database = _setup(tmp_path, OperatingMode.CONNECTED)
    _managed_system(database, config)
    target = Path(config.source.rom_root) / "snes"
    target.mkdir()
    link = Path(config.local_roms_path) / "snes" / "ROMCloud"
    link.parent.mkdir(parents=True)
    link.write_bytes(b"foreign")
    (Path(config.data_path) / "direct-links.json").write_text(
        json.dumps({"version": 1, "links": [{"path": str(link), "target": str(target)}]}),
        encoding="utf-8",
    )
    monkeypatch.setattr("romcloud.troubleshoot._inspect_browser", lambda *_: None)

    report = run_quick_repair(config_path, paths=_paths(tmp_path), activity=_inactive())

    assert _finding(report, "presentation.direct_link_foreign").status == "error"
    assert link.read_bytes() == b"foreign"


def test_direct_mode_does_not_create_missing_batocera_system_directory(
    tmp_path: Path, monkeypatch
) -> None:
    config_path, config, database = _setup(tmp_path, OperatingMode.CONNECTED)
    _managed_system(database, config)
    target = Path(config.source.rom_root) / "snes"
    target.mkdir()
    system_dir = Path(config.local_roms_path) / "snes"
    link = system_dir / "ROMCloud"
    (Path(config.data_path) / "direct-links.json").write_text(
        json.dumps({"version": 1, "links": [{"path": str(link), "target": str(target)}]}),
        encoding="utf-8",
    )
    monkeypatch.setattr("romcloud.troubleshoot._inspect_browser", lambda *_: None)

    report = run_quick_repair(
        config_path, paths=_paths(tmp_path), activity=_inactive()
    )

    assert _finding(report, "presentation.direct_link_unavailable").status == "warning"
    assert not system_dir.exists()
    assert not link.exists()


@pytest.mark.skipif(os.name == "nt", reason="Directory symlink test requires POSIX")
def test_direct_mode_preserves_symlinked_batocera_system_directory(
    tmp_path: Path, monkeypatch
) -> None:
    config_path, config, database = _setup(tmp_path, OperatingMode.CONNECTED)
    _managed_system(database, config)
    target = Path(config.source.rom_root) / "snes"
    target.mkdir()
    foreign_system_dir = tmp_path / "foreign-system"
    foreign_system_dir.mkdir()
    system_dir = Path(config.local_roms_path) / "snes"
    system_dir.symlink_to(foreign_system_dir, target_is_directory=True)
    link = system_dir / "ROMCloud"
    (Path(config.data_path) / "direct-links.json").write_text(
        json.dumps({"version": 1, "links": [{"path": str(link), "target": str(target)}]}),
        encoding="utf-8",
    )
    monkeypatch.setattr("romcloud.troubleshoot._inspect_browser", lambda *_: None)

    report = run_quick_repair(
        config_path, paths=_paths(tmp_path), activity=_inactive()
    )

    assert _finding(report, "presentation.direct_link_unavailable").status == "warning"
    assert system_dir.is_symlink()
    assert not (foreign_system_dir / "ROMCloud").exists()


def test_direct_mode_does_not_restore_when_source_target_is_a_file(
    tmp_path: Path, monkeypatch
) -> None:
    config_path, config, database = _setup(tmp_path, OperatingMode.CONNECTED)
    _managed_system(database, config)
    target = Path(config.source.rom_root) / "snes"
    target.write_bytes(b"not a directory")
    link = Path(config.local_roms_path) / "snes" / "ROMCloud"
    link.parent.mkdir()
    (Path(config.data_path) / "direct-links.json").write_text(
        json.dumps({"version": 1, "links": [{"path": str(link), "target": str(target)}]}),
        encoding="utf-8",
    )
    monkeypatch.setattr("romcloud.troubleshoot._inspect_browser", lambda *_: None)

    report = run_quick_repair(
        config_path, paths=_paths(tmp_path), activity=_inactive()
    )

    assert _finding(report, "presentation.direct_link_unavailable").status == "warning"
    assert target.is_file()
    assert not link.exists()


def test_direct_mode_does_not_restore_stale_manifest_for_unmanaged_system(
    tmp_path: Path, monkeypatch
) -> None:
    config_path, config, database = _setup(tmp_path, OperatingMode.CONNECTED)
    _managed_system(database, config, "snes")
    target = Path(config.source.rom_root) / "nes"
    target.mkdir()
    link = Path(config.local_roms_path) / "nes" / "ROMCloud"
    link.parent.mkdir()
    (Path(config.data_path) / "direct-links.json").write_text(
        json.dumps({"version": 1, "links": [{"path": str(link), "target": str(target)}]}),
        encoding="utf-8",
    )
    monkeypatch.setattr("romcloud.troubleshoot._inspect_browser", lambda *_: None)

    report = run_quick_repair(
        config_path, paths=_paths(tmp_path), activity=_inactive()
    )

    assert _finding(report, "presentation.direct_link_unauthorized").status == "warning"
    assert not link.exists()


def test_direct_mode_does_not_restore_unexpected_manifest_path_shape(
    tmp_path: Path, monkeypatch
) -> None:
    config_path, config, database = _setup(tmp_path, OperatingMode.CONNECTED)
    _managed_system(database, config)
    target = Path(config.source.rom_root) / "snes"
    target.mkdir()
    system_dir = Path(config.local_roms_path) / "snes"
    system_dir.mkdir()
    unexpected = system_dir / "NotROMCloud"
    (Path(config.data_path) / "direct-links.json").write_text(
        json.dumps(
            {"version": 1, "links": [{"path": str(unexpected), "target": str(target)}]}
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr("romcloud.troubleshoot._inspect_browser", lambda *_: None)

    report = run_quick_repair(
        config_path, paths=_paths(tmp_path), activity=_inactive()
    )

    assert _finding(report, "presentation.direct_link_unauthorized").status == "warning"
    assert not unexpected.exists()


@pytest.mark.skipif(os.name == "nt", reason="Direct-link repair requires POSIX symlinks")
def test_direct_mode_preserves_wrong_target_symlink(tmp_path: Path, monkeypatch) -> None:
    config_path, config, database = _setup(tmp_path, OperatingMode.CONNECTED)
    _managed_system(database, config)
    target = Path(config.source.rom_root) / "snes"
    target.mkdir()
    foreign_target = tmp_path / "foreign-source"
    foreign_target.mkdir()
    link = Path(config.local_roms_path) / "snes" / "ROMCloud"
    link.parent.mkdir()
    link.symlink_to(foreign_target, target_is_directory=True)
    (Path(config.data_path) / "direct-links.json").write_text(
        json.dumps({"version": 1, "links": [{"path": str(link), "target": str(target)}]}),
        encoding="utf-8",
    )
    monkeypatch.setattr("romcloud.troubleshoot._inspect_browser", lambda *_: None)

    report = run_quick_repair(
        config_path, paths=_paths(tmp_path), activity=_inactive()
    )

    assert _finding(report, "presentation.direct_link_foreign").status == "error"
    assert link.is_symlink()
    assert link.resolve() == foreign_target.resolve()


@pytest.mark.parametrize("game_state", ("active", "unknown"))
def test_active_or_unknown_game_blocks_direct_link_repair(
    tmp_path: Path, monkeypatch, game_state: str
) -> None:
    config_path, config, database = _setup(tmp_path, OperatingMode.CONNECTED)
    _managed_system(database, config)
    target = Path(config.source.rom_root) / "snes"
    target.mkdir()
    link = Path(config.local_roms_path) / "snes" / "ROMCloud"
    link.parent.mkdir()
    (Path(config.data_path) / "direct-links.json").write_text(
        json.dumps({"version": 1, "links": [{"path": str(link), "target": str(target)}]}),
        encoding="utf-8",
    )
    inactive = ActivityState("inactive")
    activity = ActivitySnapshot(
        ActivityState(game_state), inactive, inactive, inactive, inactive, inactive
    )
    monkeypatch.setattr("romcloud.troubleshoot._inspect_browser", lambda *_: None)

    report = run_quick_repair(config_path, paths=_paths(tmp_path), activity=activity)

    finding = _finding(report, "presentation.direct_link_missing")
    assert finding.status == "warning"
    assert finding.blocked_by == (f"game:{game_state}",)
    assert not link.exists()
