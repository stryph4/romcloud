from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from click.testing import CliRunner

from romcloud.bootstrap.container import Container
from romcloud.cli.main import cli
from romcloud.infrastructure.config import (
    AppConfig,
    CacheConfig,
    SMBConfig,
    SourceConfig,
    load_config,
    write_config,
)
from romcloud.infrastructure import config as config_module
from romcloud.infrastructure import mount_worker
from romcloud.lifecycle import install, runtime_layout


def _patch_layout_paths(monkeypatch, tmp_path: Path) -> dict[str, Path]:
    paths = {
        "legacy_source": tmp_path / "romcloud-source",
        "legacy_remote": tmp_path / "romcloud-saves-source",
        "legacy_cache": tmp_path / "romcloud-cache",
        "source": tmp_path / "romcloud" / "source",
        "cache": tmp_path / "romcloud" / "cache",
    }
    monkeypatch.setattr(runtime_layout, "LEGACY_SOURCE_ROOT", paths["legacy_source"])
    monkeypatch.setattr(runtime_layout, "LEGACY_REMOTE_MOUNT", paths["legacy_remote"])
    monkeypatch.setattr(runtime_layout, "LEGACY_CACHE_ROOT", paths["legacy_cache"])
    monkeypatch.setattr(runtime_layout, "SOURCE_ROOT", paths["source"])
    monkeypatch.setattr(runtime_layout, "CACHE_ROOT", paths["cache"])
    return paths


def _legacy_config(config_path: Path, paths: dict[str, Path]) -> None:
    config = AppConfig(
        source=SourceConfig("local", str(paths["legacy_source"])),
        cache=CacheConfig(str(paths["legacy_cache"])),
        local_roms_path=str(config_path.parent / "roms"),
        data_path=str(config_path.parent / "data"),
        smb=SMBConfig("nas.local", "ROMs", "reader"),
    )
    write_config(config, str(config_path))
    raw = config_path.read_text()
    config_path.write_text(
        raw.replace(
            "[saves]\n",
            "[saves]\n"
            f'remote_mount_path = "{paths["legacy_remote"]}"\n'
            'remote_subdir = "romcloud-saves"\n',
        )
    )


def test_exact_owned_legacy_paths_are_reconciled_conservatively(
    tmp_path, monkeypatch
):
    paths = _patch_layout_paths(monkeypatch, tmp_path)
    for name in ("legacy_source", "legacy_remote"):
        paths[name].mkdir()
    (paths["legacy_cache"] / ".partial").mkdir(parents=True)
    cached_rom = paths["legacy_cache"] / "psx" / "Game.chd"
    cached_rom.parent.mkdir()
    cached_rom.write_bytes(b"cached")
    config_path = tmp_path / "system" / "config" / "romcloud.toml"
    _legacy_config(config_path, paths)
    stopped = []
    unmounted = []
    monkeypatch.setattr(
        runtime_layout.mount_worker,
        "stop_worker",
        lambda home: stopped.append(home),
    )
    monkeypatch.setattr(
        runtime_layout.mount,
        "unmount_cifs_source",
        lambda path: unmounted.append(Path(path)) or True,
    )

    assert runtime_layout.reconcile_legacy_runtime_layout(config_path) is True

    config = load_config(str(config_path))
    assert config.source.rom_root == str(paths["source"])
    assert config.cache.path == str(paths["cache"])
    assert (paths["cache"] / "psx" / "Game.chd").read_bytes() == b"cached"
    assert unmounted == [paths["legacy_remote"], paths["legacy_source"]]
    assert len(stopped) == 1
    rewritten = config_path.read_text()
    assert "remote_mount_path" not in rewritten
    assert "remote_subdir" not in rewritten
    assert config.remote_data is None


def test_ambiguous_legacy_contents_are_never_deleted(tmp_path, monkeypatch):
    paths = _patch_layout_paths(monkeypatch, tmp_path)
    for name in ("legacy_source", "legacy_remote", "legacy_cache"):
        paths[name].mkdir()
        (paths[name] / "user-file").write_text("preserve")
    config_path = tmp_path / "system" / "config" / "romcloud.toml"
    _legacy_config(config_path, paths)
    monkeypatch.setattr(runtime_layout.mount_worker, "stop_worker", lambda home: None)
    monkeypatch.setattr(runtime_layout.mount, "unmount_cifs_source", lambda path: False)

    runtime_layout.reconcile_legacy_runtime_layout(config_path)

    for name in ("legacy_source", "legacy_remote", "legacy_cache"):
        assert (paths[name] / "user-file").read_text() == "preserve"
    config = load_config(str(config_path))
    assert config.source.rom_root == str(paths["source"])
    assert config.cache.path == str(paths["cache"])


def test_similar_but_non_exact_paths_are_ignored(tmp_path, monkeypatch):
    paths = _patch_layout_paths(monkeypatch, tmp_path)
    config_path = tmp_path / "system" / "config" / "romcloud.toml"
    config = AppConfig(
        source=SourceConfig("local", f'{paths["legacy_source"]}-user'),
        cache=CacheConfig(f'{paths["legacy_cache"]}-user'),
        local_roms_path=str(tmp_path / "roms"),
        data_path=str(tmp_path / "system" / "data"),
        smb=SMBConfig("nas.local", "ROMs"),
    )
    write_config(config, str(config_path))
    original = config_path.read_bytes()
    calls = []
    monkeypatch.setattr(
        runtime_layout.mount_worker,
        "stop_worker",
        lambda home: calls.append(home),
    )

    assert runtime_layout.reconcile_legacy_runtime_layout(config_path) is False
    assert calls == []
    assert config_path.read_bytes() == original
    loaded = load_config(str(config_path))
    assert loaded.source.rom_root == f'{paths["legacy_source"]}-user'
    assert loaded.cache.path == f'{paths["legacy_cache"]}-user'


def test_unrelated_remote_subdir_text_does_not_claim_legacy_mount(
    tmp_path, monkeypatch
):
    paths = _patch_layout_paths(monkeypatch, tmp_path)
    config_path = tmp_path / "system" / "config" / "romcloud.toml"
    config = AppConfig(
        source=SourceConfig("local", str(tmp_path / "roms")),
        cache=CacheConfig(str(tmp_path / "cache")),
        local_roms_path=str(tmp_path / "local-roms"),
        data_path=str(tmp_path / "system" / "data"),
    )
    write_config(config, str(config_path))
    with config_path.open("a", encoding="utf-8") as fh:
        fh.write('\n[unrelated]\nremote_subdir = "romcloud-saves"\n')
    calls = []
    monkeypatch.setattr(
        runtime_layout.mount,
        "unmount_cifs_source",
        lambda path: calls.append(path),
    )

    assert runtime_layout.reconcile_legacy_runtime_layout(config_path) is False
    assert calls == []


def test_failed_legacy_remote_unmount_does_not_block_config_migration(
    tmp_path, monkeypatch
):
    paths = _patch_layout_paths(monkeypatch, tmp_path)
    paths["legacy_remote"].mkdir()
    config_path = tmp_path / "system" / "config" / "romcloud.toml"
    _legacy_config(config_path, paths)
    monkeypatch.setattr(runtime_layout.mount_worker, "stop_worker", lambda home: None)

    def fail_remote(path):
        if Path(path) == paths["legacy_remote"]:
            raise RuntimeError("busy")
        return False

    monkeypatch.setattr(runtime_layout.mount, "unmount_cifs_source", fail_remote)

    assert runtime_layout.reconcile_legacy_runtime_layout(config_path) is True
    config = load_config(str(config_path))
    assert config.source.rom_root == str(paths["source"])
    assert config.cache.path == str(paths["cache"])
    assert config.remote_data is None
    rewritten = config_path.read_text()
    assert "remote_mount_path" not in rewritten
    assert "remote_subdir" not in rewritten
    assert paths["legacy_remote"].is_dir()


def test_exact_091_config_migrates_atomically_and_preserves_smb_and_unrelated_settings(
    tmp_path, monkeypatch
):
    config_path = tmp_path / "system" / "config" / "romcloud.toml"
    config_path.parent.mkdir(parents=True)
    data_path = tmp_path / "system" / "data"
    config_path.write_text(
        '# deployed 0.9.1 config\n'
        '[source]\n'
        'provider = "local"\n'
        'rom_root = "/userdata/romcloud-source"  # historical default\n\n'
        '[cache]\n'
        'path = "/userdata/romcloud-cache"\n\n'
        '[smb]\n'
        'server = "omnivault"\n'
        'share = "Roms"\n'
        'username = "reader"\n'
        'port = 445\n\n'
        '[saves]\n'
        'local_path = "/userdata/saves"\n'
        'remote_subdir = "romcloud-saves"\n'
        'xbox_enabled = false\n\n'
        '[data]\n'
        f'path = "{data_path}"\n\n'
        '[custom_plugin]\n'
        'enabled = true\n',
        encoding="utf-8",
    )
    config_path.chmod(0o640)
    writes = []
    real_atomic_write = config_module.atomic_write_text

    def record_atomic_write(path, content, **kwargs):
        writes.append((path, content))
        return real_atomic_write(path, content, **kwargs)

    monkeypatch.setattr(config_module, "atomic_write_text", record_atomic_write)

    config = load_config(str(config_path))

    assert len(writes) == 1
    assert config_path.stat().st_mode & 0o777 == 0o640
    assert config.source.rom_root == "/userdata/romcloud/source"
    assert config.cache.path == "/userdata/romcloud/cache"
    assert config.smb == SMBConfig("omnivault", "Roms", "reader", 445)
    assert config.remote_data is None
    assert Container(config).saves.is_remote_configured is False
    saves_status = CliRunner().invoke(
        cli, ["--config", str(config_path), "saves", "status"]
    )
    assert saves_status.exit_code == 0, saves_status.output
    assert "not configured (SaveSync unavailable)" in saves_status.output
    mounts = mount_worker.configured_mounts(config)
    assert [(target.label, target.mount_point, target.read_only) for target in mounts] == [
        ("ROM catalog", "/userdata/romcloud/source", True)
    ]
    rewritten = config_path.read_text(encoding="utf-8")
    assert 'rom_root = "/userdata/romcloud/source"  # historical default' in rewritten
    assert 'path = "/userdata/romcloud/cache"' in rewritten
    assert "remote_subdir" not in rewritten
    assert "[remote_data]" not in rewritten
    assert '[custom_plugin]\nenabled = true' in rewritten

    # Runtime loading is idempotent and does not rewrite the file again.
    assert load_config(str(config_path)) == config
    assert len(writes) == 1


def test_shared_install_update_repair_reconciler_invokes_layout_cleanup(
    tmp_path, monkeypatch
):
    home = tmp_path / "system" / "romcloud"
    project = tmp_path / "project"
    project.mkdir()
    (home / "venv" / "bin").mkdir(parents=True)
    (home / "venv" / "bin" / "python").write_text("")
    calls = []
    monkeypatch.setattr(
        runtime_layout,
        "reconcile_legacy_runtime_layout",
        lambda path: calls.append(path) or False,
    )
    monkeypatch.setattr(
        install,
        "write_core_wrappers",
        lambda *args: SimpleNamespace(),
    )
    monkeypatch.setattr(
        install,
        "install_ports_ui",
        lambda **kwargs: SimpleNamespace(installed=False),
    )
    monkeypatch.setattr(install, "reconcile_mount_service", lambda *args: None)
    monkeypatch.setattr(install, "reconcile_es_override", lambda *args: None)
    monkeypatch.setattr(install, "reconcile_ports_gamelist", lambda *args: None)

    install.reconcile_install(romcloud_home=home, project_root=project)

    assert calls == [home / "config" / "romcloud.toml"]
