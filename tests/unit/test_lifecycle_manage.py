from __future__ import annotations

import json
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
from romcloud.infrastructure.repositories.game import GameRepository
from romcloud.infrastructure.repositories.proxy import ProxyRepository
from romcloud.integrations.batocera.proxy_ownership import remove_owned_proxy_files
from romcloud.lifecycle import manage


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
    monkeypatch.setattr(manage.mount_service, "remove_service", lambda: False)
    monkeypatch.setattr(manage.es_config, "remove", lambda: False)


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
    (home / "version.json").write_text("{}")
    ports_dir = tmp_path / "ports"
    (ports_dir / "images").mkdir(parents=True)
    (ports_dir / "ROMCloud.sh").write_text("owned")
    (ports_dir / "images" / "ROMCloud.png").write_bytes(b"owned")
    (ports_dir / "gamelist.xml").write_text(
        '<?xml version="1.0"?><gameList><game><path>./ROMCloud.sh</path></game>'
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
    monkeypatch.setattr(manage.mount_service, "remove_service", lambda: calls.append("service"))
    monkeypatch.setattr(manage.es_config, "remove", lambda: calls.append("es"))

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

    assert report.proxies_removed == 2
    assert repeated.proxies_removed == 0
    assert not home.exists()
    assert not cache.exists()
    assert not proxy.exists() and not signed_orphan.exists()
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
        lambda: original_remove_service(service_path=service_path),
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

    assert removed == 1
    assert kept.is_file() and foreign.is_file()
    assert not orphan.exists()


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
        lambda path: calls.append(path) or True,
    )

    manage.uninstall(config=config, romcloud_home=home, ports_dir=tmp_path / "ports")

    assert calls == [str(remote_root), config.source.rom_root]
    assert not cifs_credentials_path(config.credentials_path).exists()
    assert not remote_data_cifs_credentials_path(config.credentials_path).exists()


def test_uninstall_removes_the_canonical_and_legacy_credential_files(
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

    assert not config.credentials_path.exists()
    assert not legacy.exists()
    assert not setup_state.exists()
    assert not stale_ephemeral.exists()


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
        lambda path: (_ for _ in ()).throw(RuntimeError("target busy")),
    )
    monkeypatch.setattr(
        manage.mount_service,
        "remove_service",
        lambda: calls.append("service"),
    )

    with pytest.raises(RuntimeError, match="uninstall stopped"):
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
    with pytest.raises(RuntimeError, match="protected ROM data"):
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
        ),
    )

    result = CliRunner().invoke(cli, ["--config", str(config_path), "repair"])

    assert result.exit_code == 0, result.output
    assert captured == ["develop"]
    assert "from develop" in result.output
    assert "Google Drive configuration could not be retrieved" in result.output


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
