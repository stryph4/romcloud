from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

from romcloud.core.capabilities import OperatingMode
from romcloud.core.models.troubleshoot import TroubleshootFinding, TroubleshootReport
from romcloud.infrastructure.config import AppConfig, CacheConfig, SourceConfig, write_config
from romcloud.infrastructure.database import Database
from romcloud.infrastructure.library_view import write_operating_mode
from romcloud.troubleshoot import (
    ActivitySnapshot,
    ActivityState,
    TroubleshootPaths,
    _inspect_catalog,
    _inspect_existing_lock,
    collect_diagnostics,
    run_quick_repair,
)


def _installation(tmp_path: Path) -> tuple[Path, AppConfig]:
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
    return config_path, config


def _paths(tmp_path: Path) -> TroubleshootPaths:
    es = tmp_path / "es"
    return TroubleshootPaths(
        auto_savesync_hook=tmp_path / "system" / "hook",
        mount_service=tmp_path / "system" / "romcloud_mount",
        services_config=tmp_path / "system" / "batocera.conf",
        es_stock=es / "es_systems.cfg",
        es_override=es / "es_systems_romcloud.cfg",
        es_user_config_dir=es,
        es_system_config_dir=es,
        es_legacy_config_dir=tmp_path / "legacy-es",
    )


def _inactive() -> ActivitySnapshot:
    state = ActivityState("inactive")
    return ActivitySnapshot(state, state, state, state, state, state)


def _tree(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in root.rglob("*")
        # SQLite's read-only WAL reader updates lock/read marks in an existing
        # shared-memory sidecar. Database/WAL contents and all durable ROMCloud
        # state remain byte-for-byte stable.
        if path.is_file() and not path.name.endswith("-shm")
    }


def test_diagnostics_are_zero_write_repeatable_and_do_not_install_mode_fallback(
    tmp_path: Path, monkeypatch
) -> None:
    config_path, config = _installation(tmp_path)
    Database(str(Path(config.data_path) / "catalog.db")).initialize()
    monkeypatch.setattr("romcloud.troubleshoot._inspect_browser", lambda *_: None)
    before = _tree(tmp_path)

    first, _ = collect_diagnostics(config_path, paths=_paths(tmp_path), activity=_inactive())
    middle = _tree(tmp_path)
    second, _ = collect_diagnostics(config_path, paths=_paths(tmp_path), activity=_inactive())

    assert middle == before == _tree(tmp_path)
    assert first.as_dict() == second.as_dict()
    assert not (Path(config.data_path) / "library-view.json").exists()


def test_read_only_catalog_sees_committed_rows_still_in_wal(tmp_path: Path) -> None:
    _, config = _installation(tmp_path)
    path = Path(config.data_path) / "catalog.db"
    Database(str(path)).initialize()
    writer = sqlite3.connect(path)
    try:
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("PRAGMA wal_autocheckpoint=0")
        writer.execute(
            "INSERT INTO games "
            "(id, system, title, source_provider, source_root, added_at, is_eligible) "
            "VALUES ('wal-game', 'switch', 'WAL Game', 'local', '/source', 'now', 1)"
        )
        writer.commit()
        assert path.with_name(path.name + "-wal").is_file()
        db_before = path.read_bytes()
        wal_before = path.with_name(path.name + "-wal").read_bytes()

        inspected = _inspect_catalog(path)

        assert inspected.trusted
        assert inspected.managed_systems == ("switch",)
        assert path.read_bytes() == db_before
        assert path.with_name(path.name + "-wal").read_bytes() == wal_before
    finally:
        writer.close()


def test_incompatible_catalog_is_not_treated_as_trusted_empty(tmp_path: Path) -> None:
    _, config = _installation(tmp_path)
    path = Path(config.data_path) / "catalog.db"
    database = Database(str(path))
    database.initialize()
    with database.connect() as conn:
        conn.execute("UPDATE schema_version SET version = 0")

    incompatible = _inspect_catalog(path)

    assert incompatible.state == "incompatible"
    assert not incompatible.trusted


def test_incompatible_catalog_blocks_catalog_derived_quick_repairs(
    tmp_path: Path, monkeypatch
) -> None:
    config_path, config = _installation(tmp_path)
    path = Path(config.data_path) / "catalog.db"
    database = Database(str(path))
    database.initialize()
    write_operating_mode(config, OperatingMode.CACHE)
    with database.connect() as conn:
        conn.execute("UPDATE schema_version SET version = 0")
    before = path.read_bytes()
    monkeypatch.setattr("romcloud.troubleshoot._inspect_browser", lambda *_: None)

    report = run_quick_repair(
        config_path, paths=_paths(tmp_path), activity=_inactive()
    )

    assert any(item.id == "presentation.catalog_gate" for item in report.findings)
    assert not any(item.id == "presentation.proxy_missing" for item in report.findings)
    assert path.read_bytes() == before


def test_initialized_empty_catalog_is_explicitly_trusted(tmp_path: Path) -> None:
    _, config = _installation(tmp_path)
    path = Path(config.data_path) / "catalog.db"
    Database(str(path)).initialize()

    inspected = _inspect_catalog(path)

    assert inspected.state == "trusted"
    assert inspected.managed_systems == ()


def test_savesync_legacy_state_and_existing_lock_are_not_rewritten_or_cleaned(
    tmp_path: Path, monkeypatch
) -> None:
    config_path, config = _installation(tmp_path)
    Database(str(Path(config.data_path) / "catalog.db")).initialize()
    write_operating_mode(config, OperatingMode.CACHE)
    state_path = Path(config.data_path) / "savesync-state.json"
    state_path.write_text(
        json.dumps(
            {
                "version": 1,
                "device_id": "legacy-device",
                "last_upload": None,
                "last_download": None,
                "shared_manifest": [],
                "last_reconcile": None,
            }
        ),
        encoding="utf-8",
    )
    lock = Path(config.data_path) / ".savesync-auto.lock"
    lock.write_bytes(b"stale-marker")
    before = _tree(tmp_path)
    monkeypatch.setattr("romcloud.troubleshoot._inspect_browser", lambda *_: None)

    collect_diagnostics(config_path, paths=_paths(tmp_path), activity=_inactive())
    state = _inspect_existing_lock(lock)

    assert state.state == "inactive"
    assert _tree(tmp_path) == before
    assert json.loads(state_path.read_text(encoding="utf-8"))["version"] == 1


def test_quick_repair_cancellation_occurs_between_atomic_fixes(monkeypatch) -> None:
    changed: set[str] = set()
    cancellation = {"requested": False}

    def report() -> TroubleshootReport:
        return TroubleshootReport(
            tuple(
                TroubleshootFinding(
                    finding_id,
                    "test",
                    "healthy" if finding_id in changed else "warning",
                    "info" if finding_id in changed else "warning",
                    finding_id,
                    fixability="automatic",
                )
                for finding_id in ("first", "second")
            )
        )

    monkeypatch.setattr(
        "romcloud.troubleshoot.collect_diagnostics",
        lambda *_args, **_kwargs: (report(), SimpleNamespace(secrets=())),
    )

    def first() -> bool:
        changed.add("first")
        cancellation["requested"] = True
        return True

    def second() -> bool:
        raise AssertionError("cancelled fix started")

    monkeypatch.setattr(
        "romcloud.troubleshoot._fix_handlers",
        lambda _context: {"first": first, "second": second},
    )

    result = run_quick_repair(
        "unused.toml", cancelled=lambda: cancellation["requested"]
    )

    statuses = {item.id: item.status for item in result.findings}
    assert statuses == {"first": "fixed", "second": "skipped"}
    assert result.cancelled is True
