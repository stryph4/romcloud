from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from xml.etree import ElementTree as ET

from romcloud.core.capabilities import OperatingMode
from romcloud.infrastructure.config import (
    AppConfig,
    CacheConfig,
    SavesConfig,
    SourceConfig,
    write_config,
)
from romcloud.infrastructure.database import Database
from romcloud.infrastructure.library_view import write_operating_mode
from romcloud.integrations.batocera import mount_service
from romcloud.troubleshoot import (
    ActivitySnapshot,
    ActivityState,
    TroubleshootPaths,
    collect_diagnostics,
    run_quick_repair,
)


def _setup(
    tmp_path: Path, *, auto_sync: bool = False
) -> tuple[Path, AppConfig, Database, TroubleshootPaths]:
    home = tmp_path / "home"
    source = tmp_path / "source"
    roms = tmp_path / "roms"
    cache = tmp_path / "cache"
    data = home / "data"
    es_user = tmp_path / "es-user"
    es_system = tmp_path / "es-system"
    for path in (source, roms, cache, data, es_user, es_system):
        path.mkdir(parents=True)
    config = AppConfig(
        source=SourceConfig("local", str(source)),
        cache=CacheConfig(str(cache), min_free_gb=0),
        local_roms_path=str(roms),
        data_path=str(data),
        saves=SavesConfig(auto_sync_enabled=auto_sync),
    )
    config_path = home / "config" / "romcloud.toml"
    config_path.parent.mkdir(parents=True)
    write_config(config, str(config_path))
    database = Database(str(data / "catalog.db"))
    database.initialize()
    paths = TroubleshootPaths(
        auto_savesync_hook=tmp_path / "system" / "autosync",
        mount_service=tmp_path / "system" / "romcloud_mount",
        services_config=tmp_path / "system" / "batocera.conf",
        es_stock=es_system / "es_systems.cfg",
        es_override=es_user / "es_systems_romcloud.cfg",
        es_user_config_dir=es_user,
        es_system_config_dir=es_system,
        es_legacy_config_dir=tmp_path / "es-legacy",
    )
    return config_path, config, database, paths


def _inactive() -> ActivitySnapshot:
    value = ActivityState("inactive")
    return ActivitySnapshot(value, value, value, value, value, value)


def _finding(report, finding_id: str):  # noqa: ANN001
    return next(item for item in report.findings if item.id == finding_id)


def test_broken_runtime_blocks_wrapper_repair(tmp_path: Path, monkeypatch) -> None:
    config_path, _, _, paths = _setup(tmp_path)
    monkeypatch.setattr("romcloud.troubleshoot._inspect_browser", lambda *_: None)

    report, _ = collect_diagnostics(config_path, paths=paths, activity=_inactive())

    wrapper = _finding(report, "runtime.cli_wrapper")
    assert wrapper.fixability == "confirmation"
    assert wrapper.blocked_by == ("runtime:broken",)
    assert not wrapper.eligible_for_quick_repair


def test_verified_runtime_allows_owned_wrapper_repair(tmp_path: Path, monkeypatch) -> None:
    config_path, _, _, paths = _setup(tmp_path)
    python = config_path.parent.parent / "venv" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.write_bytes(b"runtime")
    monkeypatch.setattr(
        "romcloud.troubleshoot.subprocess.run",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=0, stderr=""),
    )
    monkeypatch.setattr("romcloud.troubleshoot._inspect_browser", lambda *_: None)

    report = run_quick_repair(config_path, paths=paths, activity=_inactive())

    assert (config_path.parent.parent / "bin" / "romcloud").is_file()
    assert _finding(report, "runtime.cli_wrapper").status == "fixed"


def test_startup_service_is_checked_and_repaired_without_smb(
    tmp_path: Path, monkeypatch
) -> None:
    config_path, _, _, paths = _setup(tmp_path, auto_sync=True)
    paths.mount_service.parent.mkdir(parents=True)
    paths.mount_service.write_text("stale", encoding="utf-8")
    paths.services_config.write_text(
        "system.services=romcloud_mount\n", encoding="utf-8"
    )
    monkeypatch.setattr("romcloud.troubleshoot._inspect_browser", lambda *_: None)

    diagnostic, _ = collect_diagnostics(config_path, paths=paths, activity=_inactive())
    repaired = run_quick_repair(config_path, paths=paths, activity=_inactive())

    assert _finding(diagnostic, "mount.integration").status == "healthy"
    assert _finding(diagnostic, "mount.service").status == "warning"
    assert diagnostic.service_restart_required is False
    assert _finding(repaired, "mount.service").status == "fixed"
    assert repaired.service_restart_required is True
    assert paths.mount_service.read_text(encoding="utf-8") == mount_service.generate_service_script(
        str(config_path.parent.parent / "bin" / "romcloud")
    )


def test_service_enablement_failure_remains_unresolved(
    tmp_path: Path, monkeypatch
) -> None:
    config_path, _, _, paths = _setup(tmp_path, auto_sync=True)
    monkeypatch.setattr("romcloud.troubleshoot._inspect_browser", lambda *_: None)

    def script_only(romcloud_bin: str, **_kwargs) -> Path:
        paths.mount_service.parent.mkdir(parents=True, exist_ok=True)
        paths.mount_service.write_text(
            mount_service.generate_service_script(romcloud_bin), encoding="utf-8"
        )
        return paths.mount_service

    monkeypatch.setattr(mount_service, "install_service", script_only)

    report = run_quick_repair(config_path, paths=paths, activity=_inactive())

    finding = _finding(report, "mount.service")
    assert finding.status == "warning"
    assert finding.fix.succeeded is False
    assert "remains unhealthy" in finding.fix.reason
    assert report.service_restart_required is False


def test_ports_launcher_is_checked_and_malformed_gamelist_is_preserved(
    tmp_path: Path, monkeypatch
) -> None:
    config_path, config, _, paths = _setup(tmp_path)
    ports = Path(config.local_roms_path) / "ports"
    ports.mkdir()
    launcher = ports / "ROMCloud.sh"
    launcher.write_text(
        f'#!/bin/bash\nexec "{config_path.parent.parent / "bin" / "romcloud-ports"}" "$@"\n',
        encoding="utf-8",
    )
    gamelist = ports / "gamelist.xml"
    gamelist.write_bytes(b"<broken third-party xml")
    before = gamelist.read_bytes()
    monkeypatch.setattr("romcloud.troubleshoot._inspect_browser", lambda *_: None)

    report = run_quick_repair(config_path, paths=paths, activity=_inactive())

    assert _finding(report, "ports.launcher").status == "healthy"
    assert _finding(report, "ports.gamelist").status == "error"
    assert gamelist.read_bytes() == before


def test_bua_switch_uses_live_registry_and_preserves_native_fields(
    tmp_path: Path, monkeypatch
) -> None:
    config_path, config, database, paths = _setup(tmp_path)
    write_operating_mode(config, OperatingMode.CACHE)
    with database.connect() as conn:
        conn.execute(
            "INSERT INTO games "
            "(id, system, title, source_provider, source_root, added_at, is_eligible) "
            "VALUES ('switch-game', 'switch', 'Switch Game', 'local', ?, 'now', 1)",
            (config.source.rom_root,),
        )
    paths.es_stock.write_text(
        "<systemList><system><name>switch</name><path>/userdata/roms/switch</path>"
        "<extension>.xci</extension><command>emulatorlauncher -system %SYSTEM% "
        "-rom %ROM%</command><theme>stock</theme></system></systemList>",
        encoding="utf-8",
    )
    native_command = (
        "python /userdata/system/switch/configgen/switchlauncher.py "
        "%CONTROLLERSCONFIG% -system %SYSTEM% -rom %ROM%"
    )
    bua = paths.es_user_config_dir / "es_systems_switch.cfg"
    bua.write_text(
        "<systemList><system><name>switch</name><path>/userdata/roms/switch-bua</path>"
        f"<extension>.xci .nsp</extension><command>{native_command}</command>"
        "<theme>bua-theme</theme></system></systemList>",
        encoding="utf-8",
    )
    wrapper = config_path.parent.parent / "bin" / "romcloud-run"
    wrapper.parent.mkdir(parents=True)
    wrapper.write_text("owned wrapper", encoding="utf-8")
    monkeypatch.setattr("romcloud.troubleshoot._inspect_browser", lambda *_: None)

    diagnostic, _ = collect_diagnostics(config_path, paths=paths, activity=_inactive())
    repaired = run_quick_repair(config_path, paths=paths, activity=_inactive())
    repeated = run_quick_repair(config_path, paths=paths, activity=_inactive())

    assert _finding(diagnostic, "es.integration.refresh").status == "warning"
    assert diagnostic.es_restart_required is False
    assert _finding(repaired, "es.integration.refresh").status == "fixed"
    assert repaired.es_restart_required is True
    assert repeated.es_restart_required is False
    root = ET.fromstring(bua.read_text(encoding="utf-8"))
    system = root.find("system")
    assert system is not None
    assert system.findtext("path") == "/userdata/roms/switch-bua"
    assert system.findtext("theme") == "bua-theme"
    assert "switchlauncher.py" in (system.findtext("command") or "")
    assert "emulatorlauncher" not in (system.findtext("command") or "")
    patch_state = json.loads(
        (paths.es_user_config_dir / "es_systems_romcloud.patches.json").read_text(
            encoding="utf-8"
        )
    )
    native = patch_state["files"][bua.name]["systems"]["switch"]
    assert native_command in native["command"]["originals"][0]["xml"]
