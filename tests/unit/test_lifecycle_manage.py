from __future__ import annotations

import json
import stat
from dataclasses import replace
from pathlib import Path

import pytest
from click.testing import CliRunner

from romcloud.cli.main import cli
from romcloud.core.models.game import Game, GameAsset
from romcloud.core.models.proxy import ProxyRecord
from romcloud.infrastructure.config import (
    AppConfig,
    CacheConfig,
    LoggingConfig,
    RemoteDataConfig,
    SavesConfig,
    SMBConfig,
    SourceConfig,
    write_config,
)
from romcloud.infrastructure.credentials import (
    cifs_credentials_path,
    remote_data_cifs_credentials_path,
)
from romcloud.infrastructure.database import Database
from romcloud.infrastructure.ownership import record_owned_roots
from romcloud.infrastructure.repositories.game import GameRepository
from romcloud.infrastructure.repositories.proxy import ProxyRepository
from romcloud.integrations.batocera.proxy_ownership import remove_owned_proxy_files
from romcloud.lifecycle import manage
from romcloud.troubleshoot import ActivitySnapshot, ActivityState


def _config(tmp_path: Path) -> tuple[AppConfig, Path, Path, Path]:
    home = tmp_path / "home"
    local_roms = tmp_path / "roms"
    cache = tmp_path / "cache"
    source = tmp_path / "source"
    local_roms.mkdir()
    cache.mkdir()
    source.mkdir()
    config = AppConfig(
        source=SourceConfig(provider="local", rom_root=str(source)),
        cache=CacheConfig(path=str(cache)),
        local_roms_path=str(local_roms),
        data_path=str(home / "data"),
        logging=LoggingConfig(path=str(home / "logs")),
    )
    write_config(config, str(home / "config" / "romcloud.toml"))
    record_owned_roots(home, {"home": home, "data": home / "data", "cache": cache})
    return config, home, local_roms, cache


def _catalogued_proxy(config: AppConfig, local_roms: Path) -> tuple[Path, str]:
    db = Database(str(Path(config.data_path) / "catalog.db"))
    db.initialize()
    game_repo = GameRepository(db)
    proxy_repo = ProxyRepository(db)
    game = Game.create(
        system="nes",
        title="Cloud Game",
        source_provider="local",
        source_root=config.source.rom_root,
        assets=[GameAsset(filename="Cloud Game.nes", relative_path="nes/Cloud Game.nes", is_primary=True)],
    )
    game_repo.save(game)
    proxy = local_roms / "nes" / "Cloud Game.romcloud"
    proxy.parent.mkdir()
    proxy.write_text(json.dumps({
        "romcloud_version": "1",
        "game_id": game.id,
        "title": game.title,
        "system": game.system,
        "source_provider": game.source_provider,
        "source_root": game.source_root,
        "assets": [],
    }))
    proxy_repo.save(ProxyRecord.create(game.id, str(proxy)))
    return proxy, game.id


def _isolate_integrations(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(manage.auto_savesync, "stop_menu_loop", lambda data_root: False)
    monkeypatch.setattr(manage.mount_worker, "stop_worker", lambda home: False)
    monkeypatch.setattr(manage.mount_worker, "cleanup_runtime_state", lambda home: None)
    monkeypatch.setattr(manage.mount_service, "remove_service", lambda *args, **kwargs: False)
    monkeypatch.setattr(manage.es_config, "remove", lambda *args, **kwargs: False)


def _inactive_activity(**overrides: ActivityState) -> ActivitySnapshot:
    inactive = ActivityState("inactive")
    values = {
        "game": inactive,
        "download": inactive,
        "savesync": inactive,
        "library_sync": inactive,
        "browser_manager": inactive,
        "graphical_ui": inactive,
        "mount_worker": inactive,
    }
    values.update(overrides)
    return ActivitySnapshot(**values)


def test_repair_restores_wrappers_and_proxy_without_changing_user_state(tmp_path: Path) -> None:
    config, home, local_roms, _cache = _config(tmp_path)
    proxy, _game_id = _catalogued_proxy(config, local_roms)
    proxy.unlink()
    venv_python = home / "venv" / "bin" / "python"
    venv_python.parent.mkdir(parents=True)
    venv_python.write_text("")
    project_root = tmp_path / "project"
    project_root.mkdir()
    config_before = (home / "config" / "romcloud.toml").read_bytes()
    db = Database(str(home / "data" / "catalog.db"))
    games_before = [game.id for game in GameRepository(db).list_all()]
    proxies_before = [record.proxy_path for record in ProxyRepository(db).list_all()]

    _report, lifecycle_report = manage.repair(
        config=config,
        romcloud_home=home,
        project_root=project_root,
        ports_dir=tmp_path / "missing-ports",
        system_python="/bin/false",
    )

    assert (home / "bin" / "romcloud").exists()
    assert (home / "bin" / "romcloud-run").exists()
    assert proxy.exists()
    assert lifecycle_report.proxies_restored == 1
    assert (home / "config" / "romcloud.toml").read_bytes() == config_before
    assert [game.id for game in GameRepository(db).list_all()] == games_before
    assert [record.proxy_path for record in ProxyRepository(db).list_all()] == proxies_before


def test_repair_missing_venv_fails_with_bootstrap_instruction(tmp_path: Path) -> None:
    config, home, _local_roms, _cache = _config(tmp_path)
    with pytest.raises(RuntimeError, match="bootstrap installer"):
        manage.repair(config=config, romcloud_home=home, project_root=tmp_path)


def test_uninstall_removes_active_artifacts_and_preserves_recoverable_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, home, local_roms, cache = _config(tmp_path)
    proxy, _game_id = _catalogued_proxy(config, local_roms)
    real_rom = local_roms / "nes" / "Real Game.nes"
    real_rom.write_bytes(b"rom")
    foreign_proxy = local_roms / "nes" / "Foreign.romcloud"
    foreign_proxy.write_text("{}")
    (cache / "cached.nes").write_bytes(b"cache")
    for name in ("bin", "venv", "ports-gfx", "run", "runtime"):
        (home / name).mkdir(parents=True, exist_ok=True)
    (home / "runtime" / "google-oauth-client.json").write_text("release metadata")
    source_icon = home / "ports-gfx" / "ports_gfx" / "assets" / "icon.png"
    source_icon.parent.mkdir(parents=True)
    source_icon.write_bytes(b"owned")
    (home / "version.json").write_text("{}")
    ports_dir = tmp_path / "ports"
    (ports_dir / "images").mkdir(parents=True)
    wrapper = home / "bin" / "romcloud-ports"
    display_log = home / "logs" / "gui-display.log"
    (ports_dir / "ROMCloud.sh").write_text(
        "#!/bin/bash\n"
        + manage.install._display_trace_shell(display_log, "port_entry_start")
        + f'exec "{wrapper}" "$@"\n'
    )
    (ports_dir / "images" / "ROMCloud.png").write_bytes(b"owned")
    (ports_dir / "gamelist.xml").write_text(
        '<?xml version="1.0"?><gameList><game><path>./ROMCloud.sh</path>'
        '<name>ROMCloud</name><image>./images/ROMCloud.png</image></game>'
        '<game><path>./Other.sh</path></game></gameList>'
    )
    calls: list[str] = []
    _isolate_integrations(monkeypatch)
    monkeypatch.setattr(
        manage.auto_savesync,
        "stop_menu_loop",
        lambda data_root: calls.append("autosync-stop"),
    )
    monkeypatch.setattr(manage.mount_worker, "stop_worker", lambda home: calls.append("worker-stop"))
    monkeypatch.setattr(manage.mount_worker, "cleanup_runtime_state", lambda home: calls.append("runtime-cleanup"))
    monkeypatch.setattr(manage.mount_service, "remove_service", lambda *args, **kwargs: calls.append("service"))
    monkeypatch.setattr(manage.es_config, "remove", lambda *args, **kwargs: calls.append("es"))

    first = manage.uninstall(config=config, romcloud_home=home, ports_dir=ports_dir)
    second = manage.uninstall(config=config, romcloud_home=home, ports_dir=ports_dir)

    assert first.proxies_removed == 1
    assert second.proxies_removed == 0
    assert not proxy.exists()
    assert real_rom.exists() and foreign_proxy.exists()
    assert (home / "config" / "romcloud.toml").exists()
    assert (home / "data" / "catalog.db").exists()
    assert (cache / "cached.nes").exists()
    assert not (home / "bin").exists()
    assert not (home / "runtime").exists()
    assert not (ports_dir / "ROMCloud.sh").exists()
    assert not (ports_dir / "images" / "ROMCloud.png").exists()
    assert "Other.sh" in (ports_dir / "gamelist.xml").read_text()
    assert "ROMCloud.sh" not in (ports_dir / "gamelist.xml").read_text()
    assert calls == [
        "autosync-stop", "worker-stop", "service", "es", "runtime-cleanup",
        "autosync-stop", "worker-stop", "service", "es", "runtime-cleanup",
    ]


def test_purge_removes_owned_state_and_signed_orphan_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, home, local_roms, cache = _config(tmp_path)
    proxy, _game_id = _catalogued_proxy(config, local_roms)
    # Candidate enumeration must retain the prior case-insensitive ownership
    # behavior while avoiding one filesystem probe per manifest row.
    signed_orphan = local_roms / "nes" / "Orphan.ROMCLOUD"
    signed_orphan.write_text(json.dumps({
        "romcloud_version": "1", "game_id": "orphan", "assets": []
    }))
    foreign_proxy = local_roms / "nes" / "Foreign.romcloud"
    foreign_proxy.write_text(json.dumps({"game_id": "foreign", "assets": []}))
    real_rom = local_roms / "nes" / "Real.nes"
    real_rom.write_bytes(b"real")
    unrelated = local_roms / "nes" / "gamelist.xml"
    unrelated.write_text("user metadata")
    (cache / "cached.nes").write_bytes(b"cache")
    _isolate_integrations(monkeypatch)

    report = manage.purge(config=config, romcloud_home=home, ports_dir=tmp_path / "ports")
    repeated = manage.purge(config=config, romcloud_home=home, ports_dir=tmp_path / "ports")

    assert report.proxies_removed == 1
    assert repeated.proxies_removed == 0
    assert not home.exists()
    assert not cache.exists()
    assert not proxy.exists() and signed_orphan.exists()
    assert foreign_proxy.exists() and real_rom.exists() and unrelated.exists()


def test_install_boot_integration_then_purge_stops_and_removes_only_owned_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from romcloud.integrations.batocera import auto_savesync, mount_service

    config, home, _local_roms, _cache = _config(tmp_path)
    config = replace(
        config,
        saves=SavesConfig(auto_sync_enabled=True),
    )
    write_config(config, str(home / "config" / "romcloud.toml"))
    service_path = tmp_path / "services" / "romcloud_mount"
    legacy_service = tmp_path / "services" / "romcloud-mount"
    hook_path = tmp_path / "scripts" / "romcloud-autosync"
    unrelated_service = tmp_path / "services" / "unrelated_service"
    unrelated_hook = tmp_path / "scripts" / "unrelated-hook"
    unrelated_service.parent.mkdir(parents=True, exist_ok=True)
    unrelated_hook.parent.mkdir(parents=True, exist_ok=True)
    unrelated_service.write_text("user service", encoding="utf-8")
    unrelated_hook.write_text("user hook", encoding="utf-8")
    monkeypatch.setattr(mount_service, "SERVICE_SCRIPT_PATH", service_path)
    monkeypatch.setattr(mount_service, "LEGACY_SERVICE_PATH", legacy_service)
    monkeypatch.setattr(auto_savesync, "HOOK_PATH", hook_path)
    original_remove_service = mount_service.remove_service
    mount_service.install_service(str(home / "bin" / "romcloud"), service_path=service_path)
    auto_savesync.install_hook(home / "bin" / "romcloud", hook_path=hook_path)
    assert "mount boot-start" in service_path.read_text(encoding="utf-8")
    assert "_autosync menu-loop" in hook_path.read_text(encoding="utf-8")

    stopped = []
    monkeypatch.setattr(
        manage.auto_savesync,
        "stop_menu_loop",
        lambda data_root: stopped.append(data_root) or True,
    )
    monkeypatch.setattr(manage.mount_worker, "stop_worker", lambda home: False)
    monkeypatch.setattr(
        manage.mount_worker, "cleanup_runtime_state", lambda home: None
    )
    monkeypatch.setattr(
        manage.mount_service,
        "remove_service",
        lambda *args, **kwargs: original_remove_service(
            str(home / "bin" / "romcloud"), service_path=service_path
        ),
    )
    monkeypatch.setattr(manage.es_config, "remove", lambda: False)

    manage.purge(config=config, romcloud_home=home, ports_dir=tmp_path / "ports")

    assert stopped == [Path(config.data_path)]
    assert not service_path.exists()
    assert not hook_path.exists()
    assert unrelated_service.read_text(encoding="utf-8") == "user service"
    assert unrelated_hook.read_text(encoding="utf-8") == "user hook"
    assert not home.exists()


def test_proxy_cleanup_scans_candidates_once_without_weakening_ownership(
    tmp_path: Path,
) -> None:
    local_root = tmp_path / "roms"
    system = local_root / "nes"
    system.mkdir(parents=True)
    kept = system / "Kept.romcloud"
    orphan = system / "Orphan.ROMCLOUD"
    foreign = system / "Foreign.romcloud"
    kept.write_text(json.dumps({
        "romcloud_version": "1", "game_id": "kept", "assets": []
    }))
    orphan.write_text(json.dumps({
        "romcloud_version": "1", "game_id": "orphan", "assets": []
    }))
    foreign.write_text("{}")
    missing = [
        (f"missing-{index}", system / f"Missing {index}.romcloud")
        for index in range(100)
    ]

    removed = remove_owned_proxy_files(
        local_root,
        manifest_records=[("kept", kept), *missing],
        keep_game_ids={"kept"},
    )

    assert removed == 0
    assert kept.is_file() and foreign.is_file() and orphan.is_file()


def test_purge_preserves_user_controlled_remote_data(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, home, _local_roms, _cache = _config(tmp_path)
    remote_root = tmp_path / "remote-data"
    remote_save = remote_root / "saves" / "psx" / "Game.srm"
    remote_save.parent.mkdir(parents=True)
    remote_save.write_bytes(b"user-save")
    config = replace(
        config,
        remote_data=RemoteDataConfig(provider="local", root=str(remote_root)),
    )
    write_config(config, str(home / "config" / "romcloud.toml"))
    _isolate_integrations(monkeypatch)

    manage.purge(config=config, romcloud_home=home, ports_dir=tmp_path / "ports")

    assert remote_save.read_bytes() == b"user-save"


def test_uninstall_unmounts_remote_before_source_and_removes_both_helpers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, home, _local_roms, _cache = _config(tmp_path)
    remote_root = tmp_path / "remote-mount"
    remote_root.mkdir()
    config = replace(
        config,
        smb=SMBConfig("rom-nas", "ROMs", "reader"),
        remote_data=RemoteDataConfig(
            provider="smb",
            root=str(remote_root),
            smb=SMBConfig("data-nas", "ROMCloud", "writer"),
        ),
    )
    for helper in (
        cifs_credentials_path(config.credentials_path),
        remote_data_cifs_credentials_path(config.credentials_path),
    ):
        helper.parent.mkdir(parents=True, exist_ok=True)
        helper.write_text("temporary helper")
    calls = []
    _isolate_integrations(monkeypatch)
    monkeypatch.setattr(
        manage.mountlib,
        "unmount_cifs_source",
        lambda path, **kwargs: calls.append(path) or True,
    )
    monkeypatch.setattr(manage.mountlib, "is_target_mounted", lambda path: True)
    monkeypatch.setattr(manage.mountlib, "is_target_mounted_cifs", lambda path, **kwargs: True)

    manage.uninstall(config=config, romcloud_home=home, ports_dir=tmp_path / "ports")

    assert calls == [str(remote_root), config.source.rom_root]
    assert cifs_credentials_path(config.credentials_path).exists()
    assert remote_data_cifs_credentials_path(config.credentials_path).exists()


def test_uninstall_preserves_the_canonical_and_legacy_credential_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, home, _local_roms, _cache = _config(tmp_path)
    config.credentials_path.parent.mkdir(parents=True, exist_ok=True)
    config.credentials_path.write_text('[smb]\npassword = "hunter2"\n', encoding="utf-8")
    config.credentials_path.chmod(0o600)
    legacy = config.credentials_path.with_name("smb.credentials")
    legacy.write_text("username=alice\npassword=hunter2\n", encoding="utf-8")
    legacy.chmod(0o600)
    setup_state = config.credentials_path.parent / "setup-state.json"
    setup_state.write_text("{}", encoding="utf-8")
    stale_ephemeral = config.credentials_path.parent / ".romcloud-cifs-source-abc123"
    stale_ephemeral.write_text("username=alice\npassword=hunter2\n", encoding="utf-8")
    stale_ephemeral.chmod(0o600)
    _isolate_integrations(monkeypatch)

    manage.uninstall(config=config, romcloud_home=home, ports_dir=tmp_path / "ports")

    assert config.credentials_path.exists()
    assert legacy.exists()
    assert setup_state.exists()
    assert stale_ephemeral.exists()
    assert stat.S_IMODE(config.credentials_path.stat().st_mode) == 0o600


def test_uninstall_stops_before_runtime_removal_if_a_mount_cannot_unmount(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, home, _local_roms, _cache = _config(tmp_path)
    (home / "bin").mkdir(parents=True)
    runtime_file = home / "bin" / "romcloud"
    runtime_file.write_text("keep until mount is safe")
    config = replace(config, smb=SMBConfig("rom-nas", "ROMs", "reader"))
    calls = []
    _isolate_integrations(monkeypatch)
    monkeypatch.setattr(
        manage.mountlib,
        "unmount_cifs_source",
        lambda path, **kwargs: (_ for _ in ()).throw(RuntimeError("target busy")),
    )
    monkeypatch.setattr(manage.mountlib, "is_target_mounted", lambda path: True)
    monkeypatch.setattr(manage.mountlib, "is_target_mounted_cifs", lambda path, **kwargs: True)
    monkeypatch.setattr(
        manage.mount_service,
        "remove_service",
        lambda: calls.append("service"),
    )

    with pytest.raises(RuntimeError, match="target busy"):
        manage.uninstall(config=config, romcloud_home=home)

    assert runtime_file.exists()
    assert calls == []


def test_purge_refuses_cache_root_containing_real_roms(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, home, local_roms, _cache = _config(tmp_path)
    unsafe = AppConfig(
        source=config.source,
        cache=CacheConfig(path=str(tmp_path)),
        local_roms_path=config.local_roms_path,
        data_path=config.data_path,
        logging=config.logging,
    )
    _isolate_integrations(monkeypatch)
    with pytest.raises(RuntimeError, match="protected user/provider data"):
        manage.purge(config=unsafe, romcloud_home=home, ports_dir=tmp_path / "ports")
    assert local_roms.exists()


def test_reinstall_after_uninstall_restores_runtime_and_preserved_proxies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, home, local_roms, _cache = _config(tmp_path)
    proxy, _game_id = _catalogued_proxy(config, local_roms)
    (home / "venv").mkdir()
    _isolate_integrations(monkeypatch)
    manage.uninstall(config=config, romcloud_home=home, ports_dir=tmp_path / "ports")
    assert not proxy.exists()

    venv_python = home / "venv" / "bin" / "python"
    venv_python.parent.mkdir(parents=True)
    venv_python.write_text("")
    project_root = tmp_path / "project"
    project_root.mkdir()
    _report, lifecycle_report = manage.repair(
        config=config,
        romcloud_home=home,
        project_root=project_root,
        ports_dir=tmp_path / "ports",
        system_python="/bin/false",
    )

    assert (home / "bin" / "romcloud").exists()
    assert proxy.exists()
    assert lifecycle_report.proxies_restored == 1


def test_bootstrap_layout_can_be_recreated_after_purge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, home, _local_roms, cache = _config(tmp_path)
    _isolate_integrations(monkeypatch)
    manage.purge(config=config, romcloud_home=home, ports_dir=tmp_path / "ports")

    write_config(config, str(home / "config" / "romcloud.toml"))
    cache.mkdir()
    venv_python = home / "venv" / "bin" / "python"
    venv_python.parent.mkdir(parents=True)
    venv_python.write_text("")
    project_root = tmp_path / "project"
    project_root.mkdir()
    manage.repair(
        config=config,
        romcloud_home=home,
        project_root=project_root,
        ports_dir=tmp_path / "ports",
        system_python="/bin/false",
    )

    assert (home / "bin" / "romcloud").exists()
    assert (home / "config" / "romcloud.toml").exists()
    assert cache.exists()


def test_cli_cancellation_and_noninteractive_flags(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _config_value, home, _local_roms, _cache = _config(tmp_path)
    config_path = home / "config" / "romcloud.toml"
    calls: list[str] = []
    monkeypatch.setattr(manage, "uninstall", lambda **kwargs: calls.append("uninstall") or manage.LifecycleReport())
    monkeypatch.setattr(manage, "purge", lambda **kwargs: calls.append("purge") or manage.LifecycleReport())
    runner = CliRunner()

    cancelled = runner.invoke(cli, ["--config", str(config_path), "purge"], input="n\n")
    automated_uninstall = runner.invoke(cli, ["--config", str(config_path), "uninstall", "--yes"])
    automated_purge = runner.invoke(cli, ["--config", str(config_path), "purge", "--yes"])

    assert cancelled.exit_code == 0
    assert "Purge cancelled" in cancelled.output
    assert automated_uninstall.exit_code == 0
    assert automated_purge.exit_code == 0
    assert calls == ["uninstall", "purge"]


def test_cli_repair_uses_persisted_channel(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from romcloud.lifecycle import update as update_module

    config, home, _local_roms, _cache = _config(tmp_path)
    config = replace(config, update_channel="develop")
    config_path = home / "config" / "romcloud.toml"
    write_config(config, str(config_path))
    captured = []
    new = update_module.BuildInfo(
        "1", "a" * 40, "a" * 12, "x", "github:test@develop", channel="develop"
    )
    monkeypatch.setattr(
        update_module,
        "perform_repair",
        lambda *args, **kwargs: captured.append(kwargs["channel"])
        or update_module.UpdateResult(
            previous=None,
            new=new,
            reconcile_log="warning: Google Drive configuration could not be retrieved.",
            warnings=("Google Drive configuration could not be retrieved.",),
            es_restart_required=True,
        ),
    )

    result = CliRunner().invoke(cli, ["--config", str(config_path), "repair"])

    assert result.exit_code == 0, result.output
    assert captured == ["develop"]
    assert "Repair completed with warnings" in result.output
    assert "from develop" in result.output
    assert "Google Drive configuration could not be retrieved" in result.output
    assert "Restart EmulationStation" in result.output


def test_cli_repeated_purge_is_safe_after_config_is_gone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = tmp_path / "home" / "config" / "romcloud.toml"
    received: list[AppConfig] = []
    monkeypatch.setattr(
        manage,
        "purge",
        lambda **kwargs: received.append(kwargs["config"]) or manage.LifecycleReport(),
    )

    result = CliRunner().invoke(cli, ["--config", str(config_path), "purge", "--yes"])

    assert result.exit_code == 0
    assert len(received) == 1
    assert received[0].local_roms_path == "/.__romcloud_missing_config__/roms"


@pytest.mark.parametrize("state", ["active", "unknown"])
def test_preflight_blocks_active_or_unknown_relevant_activity(
    tmp_path: Path, state: str
) -> None:
    config, home, _local_roms, _cache = _config(tmp_path)
    activity = _inactive_activity(game=ActivityState(state, "test signal"))

    with pytest.raises(RuntimeError, match=f"game:{state}"):
        manage.lifecycle_preflight(
            operation="uninstall",
            config=config,
            romcloud_home=home,
            activity=activity,
        )


def test_purge_rejects_owned_root_below_actual_save_root(tmp_path: Path) -> None:
    config, home, _local_roms, _cache = _config(tmp_path)
    save_root = tmp_path / "actual-saves"
    save_root.mkdir()
    unsafe = replace(
        config,
        saves=SavesConfig(local_path=str(save_root)),
        cache=CacheConfig(path=str(save_root / "romcloud-cache")),
    )

    with pytest.raises(RuntimeError, match="protected user/provider data"):
        manage.lifecycle_preflight(
            operation="purge",
            config=unsafe,
            romcloud_home=home,
            activity=_inactive_activity(),
        )


def test_purge_rejects_owned_root_containing_actual_save_root(tmp_path: Path) -> None:
    config, home, _local_roms, _cache = _config(tmp_path)
    owned = tmp_path / "owned"
    save_root = owned / "real-saves"
    save_root.mkdir(parents=True)
    unsafe = replace(
        config,
        saves=SavesConfig(local_path=str(save_root)),
        cache=CacheConfig(path=str(owned)),
    )

    with pytest.raises(RuntimeError, match="protected user/provider data"):
        manage.lifecycle_preflight(
            operation="purge",
            config=unsafe,
            romcloud_home=home,
            activity=_inactive_activity(),
        )


def test_preflight_rejects_local_presentation_inside_actual_save_root(tmp_path: Path) -> None:
    config, home, _local_roms, _cache = _config(tmp_path)
    save_root = tmp_path / "actual-saves"
    local_roms = save_root / "romcloud-presentation"
    local_roms.mkdir(parents=True)
    unsafe = replace(
        config,
        saves=SavesConfig(local_path=str(save_root)),
        local_roms_path=str(local_roms),
    )

    with pytest.raises(RuntimeError, match="presentation root overlaps save root"):
        manage.lifecycle_preflight(
            operation="uninstall",
            config=unsafe,
            romcloud_home=home,
            activity=_inactive_activity(),
        )


def test_preflight_rejects_local_presentation_overlapping_source_root(tmp_path: Path) -> None:
    config, home, _local_roms, _cache = _config(tmp_path)
    unsafe = replace(config, local_roms_path=config.source.rom_root)

    with pytest.raises(RuntimeError, match="presentation root overlaps ROM source root"):
        manage.lifecycle_preflight(
            operation="uninstall",
            config=unsafe,
            romcloud_home=home,
            activity=_inactive_activity(),
        )


def test_preflight_blocks_foreign_mount_at_configured_point(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, home, _local_roms, _cache = _config(tmp_path)
    config = replace(config, smb=SMBConfig("expected-server", "ROMs", "reader"))
    monkeypatch.setattr(manage.mountlib, "is_target_mounted", lambda path: True)
    monkeypatch.setattr(manage.mountlib, "is_target_mounted_cifs", lambda path, **kwargs: False)

    with pytest.raises(RuntimeError, match="foreign mount"):
        manage.lifecycle_preflight(
            operation="uninstall",
            config=config,
            romcloud_home=home,
            activity=_inactive_activity(),
        )


def test_required_purge_delete_failure_is_reported_and_non_successful(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, home, _local_roms, cache = _config(tmp_path)
    _isolate_integrations(monkeypatch)
    original = manage.shutil.rmtree

    def fail_cache(path: Path, *args, **kwargs) -> None:
        if Path(path) == cache:
            raise OSError("injected cache delete failure")
        original(path, *args, **kwargs)

    monkeypatch.setattr(manage.shutil, "rmtree", fail_cache)

    with pytest.raises(manage.LifecycleFailure, match="injected cache") as raised:
        manage.purge(config=config, romcloud_home=home, ports_dir=tmp_path / "ports")

    assert any(stage.status == "failed" for stage in raised.value.report.stages)
    assert home.exists()
    assert cache.exists()


def test_missing_config_never_authorizes_custom_recursive_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, home, _local_roms, _cache = _config(tmp_path)
    marker = home / "foreign.txt"
    marker.write_text("preserve")
    _isolate_integrations(monkeypatch)

    report = manage.purge(
        config=config,
        romcloud_home=home,
        ports_dir=tmp_path / "ports",
        config_trusted=False,
    )

    assert marker.read_text() == "preserve"
    assert any("Configuration is missing" in warning for warning in report.warnings)


def test_configured_arbitrary_data_and_cache_without_ownership_are_preserved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, home, _local_roms, _cache = _config(tmp_path)
    shared_data = tmp_path / "shared-data"
    shared_cache = tmp_path / "shared-cache"
    shared_data.mkdir()
    shared_cache.mkdir()
    (shared_data / "foreign").write_text("keep")
    (shared_cache / "foreign").write_text("keep")
    config = replace(
        config,
        data_path=str(shared_data),
        cache=CacheConfig(path=str(shared_cache)),
    )
    write_config(config, str(home / "config" / "romcloud.toml"))
    _isolate_integrations(monkeypatch)

    report = manage.purge(config=config, romcloud_home=home, ports_dir=tmp_path / "ports")

    assert (shared_data / "foreign").read_text() == "keep"
    assert (shared_cache / "foreign").read_text() == "keep"
    assert {stage.status for stage in report.stages if stage.name.startswith("persistent-")} == {"uncertain"}


def test_valid_config_alone_cannot_authorize_arbitrary_home_deletion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "user-home"
    local_roms = tmp_path / "roms"
    source = tmp_path / "source"
    cache = tmp_path / "shared-cache"
    for path in (local_roms, source, cache):
        path.mkdir()
    config = AppConfig(
        source=SourceConfig(provider="local", rom_root=str(source)),
        cache=CacheConfig(path=str(cache)),
        local_roms_path=str(local_roms),
        data_path=str(tmp_path / "shared-data"),
        logging=LoggingConfig(path=str(home / "logs")),
    )
    write_config(config, str(home / "config" / "romcloud.toml"))
    foreign = home / "bin" / "personal-tool"
    foreign.parent.mkdir(parents=True)
    foreign.write_text("keep")
    _isolate_integrations(monkeypatch)

    report = manage.purge(config=config, romcloud_home=home, ports_dir=tmp_path / "ports")

    assert foreign.read_text() == "keep"
    assert (home / "config" / "romcloud.toml").is_file()
    assert any(stage.status == "uncertain" for stage in report.stages)


def test_owned_idle_manager_may_be_quiesced_but_download_still_blocks(tmp_path: Path) -> None:
    config, home, _local_roms, _cache = _config(tmp_path)
    active_manager = ActivityState("active", "Owned manager endpoint is reachable.")
    preflight = manage.lifecycle_preflight(
        operation="uninstall",
        config=config,
        romcloud_home=home,
        activity=_inactive_activity(browser_manager=active_manager),
    )
    assert preflight.activity.browser_manager.state == "active"

    with pytest.raises(RuntimeError, match="download:active"):
        manage.lifecycle_preflight(
            operation="uninstall",
            config=config,
            romcloud_home=home,
            activity=_inactive_activity(
                browser_manager=active_manager,
                download=ActivityState("active", "verifying=1"),
            ),
        )


def test_unknown_manager_ownership_blocks_lifecycle(tmp_path: Path) -> None:
    config, home, _local_roms, _cache = _config(tmp_path)
    with pytest.raises(RuntimeError, match="browser_manager:unknown"):
        manage.lifecycle_preflight(
            operation="uninstall",
            config=config,
            romcloud_home=home,
            activity=_inactive_activity(
                browser_manager=ActivityState("unknown", "unverified manager")
            ),
        )


def test_fake_cifs_pattern_file_survives_purge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, home, _local_roms, _cache = _config(tmp_path)
    fake = config.credentials_path.parent / ".romcloud-cifs-source-abcdef"
    fake.write_text("unrelated user file\n")
    fake.chmod(0o600)
    _isolate_integrations(monkeypatch)

    manage.purge(config=config, romcloud_home=home, ports_dir=tmp_path / "ports")

    assert fake.read_text() == "unrelated user file\n"


def test_owned_service_cleanup_failure_is_a_warning_not_already_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, home, _local_roms, _cache = _config(tmp_path)
    service = tmp_path / "romcloud-service"
    service.write_text("owned")
    _isolate_integrations(monkeypatch)
    monkeypatch.setattr(manage.mount_service, "SERVICE_SCRIPT_PATH", service)
    monkeypatch.setattr(manage.mount_service, "service_is_owned", lambda *args, **kwargs: service.exists())
    monkeypatch.setattr(manage.mount_service, "remove_service", lambda *args, **kwargs: False)

    report = manage.uninstall(config=config, romcloud_home=home, ports_dir=tmp_path / "ports")

    stage = next(item for item in report.stages if item.name == "startup-service")
    assert stage.status == "warning"
    assert service.exists()


def test_missing_custom_config_cli_creates_no_parent_directories(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = tmp_path / "custom" / "nested" / "romcloud.toml"
    monkeypatch.setattr(manage, "purge", lambda **kwargs: manage.LifecycleReport())

    result = CliRunner().invoke(cli, ["--config", str(config_path), "purge", "--yes"])

    assert result.exit_code == 0, result.output
    assert not config_path.parent.exists()


def test_uninstall_removes_managed_browser_and_preserves_external_browser(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from romcloud.web.browser_runtime import activate_staged_runtime, runtime_root, staging_version_path

    config, home, _local_roms, _cache = _config(tmp_path)
    staged = staging_version_path(config.data_path, "1") / "chrome"
    staged.parent.mkdir(parents=True)
    staged.write_text("browser")
    staged.chmod(0o755)
    activate_staged_runtime(
        config.data_path,
        version="1",
        executable="chrome",
        smoke_test=lambda _: {"compatible": True},
    )
    external = tmp_path / "external-chromium.AppImage"
    external.write_text("external")
    _isolate_integrations(monkeypatch)

    report = manage.uninstall(config=config, romcloud_home=home, ports_dir=tmp_path / "ports")

    assert not runtime_root(config.data_path).exists()
    assert external.read_text() == "external"
    assert next(item for item in report.stages if item.name == "managed-browser").status == "removed"
