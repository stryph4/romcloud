from __future__ import annotations

from pathlib import Path

import pytest

from romcloud.core.capabilities import OperatingMode
from romcloud.infrastructure.config import (
    AppConfig,
    CacheConfig,
    SMBConfig,
    SourceConfig,
    write_config,
)
from romcloud.infrastructure.database import Database
from romcloud.infrastructure.library_view import write_operating_mode
from romcloud.troubleshoot import (
    ActivitySnapshot,
    ActivityState,
    TroubleshootPaths,
    collect_diagnostics,
    run_quick_repair,
)


def _setup(tmp_path: Path) -> tuple[Path, AppConfig, TroubleshootPaths]:
    home = tmp_path / "home"
    source = tmp_path / "source-mount"
    roms = tmp_path / "roms"
    cache = tmp_path / "cache"
    data = home / "data"
    es = tmp_path / "es"
    for path in (source, roms, cache, data, es):
        path.mkdir(parents=True)
    config = AppConfig(
        source=SourceConfig("local", str(source)),
        smb=SMBConfig("server", "share"),
        cache=CacheConfig(str(cache), min_free_gb=0),
        local_roms_path=str(roms),
        data_path=str(data),
    )
    config_path = home / "config" / "romcloud.toml"
    config_path.parent.mkdir(parents=True)
    write_config(config, str(config_path))
    Database(str(data / "catalog.db")).initialize()
    write_operating_mode(config, OperatingMode.CACHE)
    paths = TroubleshootPaths(
        auto_savesync_hook=tmp_path / "system" / "hook",
        mount_service=tmp_path / "system" / "service",
        services_config=tmp_path / "system" / "batocera.conf",
        es_stock=es / "es_systems.cfg",
        es_override=es / "es_systems_romcloud.cfg",
        es_user_config_dir=es,
        es_system_config_dir=es,
        es_legacy_config_dir=tmp_path / "legacy",
    )
    return config_path, config, paths


def _activity(**states: str) -> ActivitySnapshot:
    return ActivitySnapshot(
        *(
            ActivityState(states.get(name, "inactive"))
            for name in (
                "game",
                "download",
                "savesync",
                "library_sync",
                "browser_manager",
                "graphical_ui",
            )
        )
    )


@pytest.mark.parametrize("active_name", ("download", "savesync", "library_sync"))
def test_active_operations_block_mount_mutation(
    tmp_path: Path, monkeypatch, active_name: str
) -> None:
    config_path, _, paths = _setup(tmp_path)
    monkeypatch.setattr(
        "romcloud.infrastructure.mount_worker._configured_mount_is_ready",
        lambda *_: False,
    )
    monkeypatch.setattr("romcloud.troubleshoot._inspect_browser", lambda *_: None)

    report = run_quick_repair(
        config_path, paths=paths, activity=_activity(**{active_name: "active"})
    )

    finding = next(item for item in report.findings if item.id == "mount.integration")
    assert f"{active_name}:active" in finding.blocked_by
    assert finding.status == "warning"


def test_active_browser_blocks_es_reconciliation(tmp_path: Path, monkeypatch) -> None:
    config_path, config, paths = _setup(tmp_path)
    with Database(str(Path(config.data_path) / "catalog.db")).connect() as conn:
        conn.execute(
            "INSERT INTO games "
            "(id, system, title, source_provider, source_root, added_at, is_eligible) "
            "VALUES ('g', 'snes', 'Game', 'local', ?, 'now', 1)",
            (config.source.rom_root,),
        )
    paths.es_stock.write_text(
        "<systemList><system><name>snes</name><extension>.sfc</extension>"
        "<command>launch %ROM%</command></system></systemList>",
        encoding="utf-8",
    )
    wrapper = config_path.parent.parent / "bin" / "romcloud-run"
    wrapper.parent.mkdir(parents=True)
    wrapper.write_text("wrapper", encoding="utf-8")
    monkeypatch.setattr("romcloud.troubleshoot._inspect_browser", lambda *_: None)
    monkeypatch.setattr(
        "romcloud.integrations.batocera.es_config.refresh",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("ES repair ran")),
    )

    report = run_quick_repair(
        config_path, paths=paths, activity=_activity(browser_manager="active")
    )

    finding = next(
        item for item in report.findings if item.id == "es.integration.refresh"
    )
    assert finding.blocked_by == ("browser_manager:active",)
    assert finding.status == "warning"


def test_stale_mount_worker_pid_is_reported_without_cleanup(
    tmp_path: Path, monkeypatch
) -> None:
    from romcloud.infrastructure import mount_worker

    config_path, _, paths = _setup(tmp_path)
    lock = mount_worker.lock_path(config_path.parent.parent)
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text("999999", encoding="ascii")
    monkeypatch.setattr(
        "romcloud.infrastructure.mount_worker._configured_mount_is_ready",
        lambda *_: False,
    )
    monkeypatch.setattr("romcloud.infrastructure.mount_worker._pid_alive", lambda *_: False)
    monkeypatch.setattr("romcloud.troubleshoot._inspect_browser", lambda *_: None)

    report, _ = collect_diagnostics(
        config_path, paths=paths, activity=_activity()
    )

    finding = next(item for item in report.findings if item.id == "mount.worker")
    assert finding.status == "warning"
    assert lock.read_text(encoding="ascii") == "999999"
