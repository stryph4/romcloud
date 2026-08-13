"""Unit tests for the `romcloud mount` CLI commands.

Focuses on the boot-safety hardening: `boot-start` must never block or
raise (even if config/container access itself fails), `stop`/`remove` must
terminate any running worker, `install` must never depend on PATH, and
`status` must surface the richer worker/failure diagnostics. Underlying
mount/worker logic is exercised in test_mount.py / test_mount_worker.py;
here we only verify the CLI wiring via monkeypatching.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from click.testing import CliRunner

from romcloud.cli.main import cli
import romcloud.cli.commands.mount as mount_cmd_module
import romcloud.services.connections as connections_service
from romcloud.core.exceptions import MountError
from romcloud.cli.commands.mount import mount_group
from romcloud.infrastructure.config import AppConfig, CacheConfig, SMBConfig, SourceConfig, write_config
from romcloud.infrastructure.mount_worker import MountDiagnostics


def _fake_smb(server="nas.local", share="ROMs", username="alice", port=445):
    return SimpleNamespace(server=server, share=share, username=username, port=port)


def _fake_config(
    *, smb=None, rom_root="/mnt/roms", romcloud_home="/opt/romcloud", saves_mount=None
):
    home = Path(romcloud_home)
    return SimpleNamespace(
        source=SimpleNamespace(rom_root=rom_root),
        smb=smb,
        credentials_path=home / "config" / "credentials.toml",
        data_path=str(home / "data"),
        logging=SimpleNamespace(level="INFO", path=None),
        remote_data=(
            SimpleNamespace(
                provider="smb",
                root=saves_mount,
                smb=_fake_smb(server="data-nas.local", share="ROMCloud"),
            )
            if saves_mount is not None
            else None
        ),
    )


def _invoke(args, config):
    return CliRunner().invoke(mount_group, args, obj={"config": config})


class TestBootStart:
    def test_catalog_mount_alone_does_not_hide_missing_savesync_mount(self, monkeypatch):
        monkeypatch.setattr(
            mount_cmd_module.mount,
            "is_target_mounted_cifs",
            lambda path, **kwargs: path == "/mnt/roms",
        )
        monkeypatch.setattr(mount_cmd_module.mount_worker, "is_worker_running", lambda *a: None)
        monkeypatch.setattr(mount_cmd_module.mount_worker, "spawn_worker", lambda *a: 4242)

        result = _invoke(
            ["boot-start"],
            _fake_config(smb=_fake_smb(), saves_mount="/mnt/saves-rw"),
        )

        assert result.exit_code == 0, result.output
        assert "4242" in result.output

    def test_already_mounted_skips_spawn(self, monkeypatch):
        monkeypatch.setattr(mount_cmd_module.mount, "is_target_mounted_cifs", lambda *a, **k: True)
        monkeypatch.setattr(
            mount_cmd_module.mount, "is_target_mounted_read_only", lambda *a, **k: True
        )
        spawned = []
        monkeypatch.setattr(mount_cmd_module.mount_worker, "spawn_worker", lambda *a, **k: spawned.append(1))

        result = _invoke(["boot-start"], _fake_config(smb=_fake_smb()))

        assert result.exit_code == 0, result.output
        assert "Already mounted" in result.output
        assert spawned == []

    def test_worker_already_running_skips_spawn(self, monkeypatch):
        monkeypatch.setattr(mount_cmd_module.mount, "is_target_mounted_cifs", lambda *a, **k: False)
        monkeypatch.setattr(mount_cmd_module.mount_worker, "is_worker_running", lambda *a, **k: 4242)
        spawned = []
        monkeypatch.setattr(mount_cmd_module.mount_worker, "spawn_worker", lambda *a, **k: spawned.append(1))

        result = _invoke(["boot-start"], _fake_config(smb=_fake_smb()))

        assert result.exit_code == 0, result.output
        assert "4242" in result.output
        assert spawned == []

    def test_spawns_worker_and_returns_immediately(self, monkeypatch):
        monkeypatch.setattr(mount_cmd_module.mount, "is_target_mounted_cifs", lambda *a, **k: False)
        monkeypatch.setattr(mount_cmd_module.mount_worker, "is_worker_running", lambda *a, **k: None)
        monkeypatch.setattr(mount_cmd_module.mount_worker, "spawn_worker", lambda *a, **k: 9999)

        result = _invoke(["boot-start"], _fake_config(smb=_fake_smb()))

        assert result.exit_code == 0, result.output
        assert "background" in result.output.lower()
        assert "9999" in result.output

    def test_no_smb_configured_is_a_clean_noop(self, monkeypatch):
        called = []
        monkeypatch.setattr(mount_cmd_module.mount, "is_target_mounted_cifs", lambda *a, **k: called.append(1))

        result = _invoke(["boot-start"], _fake_config(smb=None))

        assert result.exit_code == 0, result.output
        assert called == []

    def test_never_raises_even_if_mount_check_fails(self, monkeypatch):
        """Core rule: ROMCloud may fail; Batocera must not — boot-start must
        never propagate an exception, no matter what breaks underneath."""

        def _boom(*a, **k):
            raise RuntimeError("disk on fire")

        monkeypatch.setattr(mount_cmd_module.mount, "is_target_mounted_cifs", _boom)

        result = _invoke(["boot-start"], _fake_config(smb=_fake_smb()))

        assert result.exit_code == 0
        assert result.exception is None
        assert "warning" in result.output.lower()

    def test_never_raises_even_if_config_access_fails(self, monkeypatch):
        def _boom(ctx, **kwargs):
            raise RuntimeError("container build failed")

        monkeypatch.setattr(mount_cmd_module, "_get_mount_config", _boom)

        result = CliRunner().invoke(mount_group, ["boot-start"], obj={"config": _fake_config(smb=_fake_smb())})

        assert result.exit_code == 0
        assert result.exception is None


class TestWorkerCommand:
    def test_invokes_run_worker_and_uses_its_exit_code(self, monkeypatch):
        monkeypatch.setattr(mount_cmd_module.mount_worker, "run_worker", lambda *a, **k: 0)
        result = _invoke(["worker"], _fake_config(smb=_fake_smb()))
        assert result.exit_code == 0

    def test_worker_is_hidden_from_help(self):
        assert mount_group.commands["worker"].hidden is True


class TestStop:
    def test_shutdown_selects_safe_config_parsing_before_container_build(
        self, monkeypatch
    ):
        config = _fake_config(smb=_fake_smb())
        loaded = []
        monkeypatch.setattr(
            mount_cmd_module,
            "load_config",
            lambda path, **kwargs: loaded.append((path, kwargs)) or config,
        )
        monkeypatch.setattr(
            mount_cmd_module,
            "configure_logging",
            lambda **kwargs: None,
        )
        monkeypatch.setattr(
            mount_cmd_module.mount_worker,
            "configured_mounts",
            lambda config, **kwargs: (object(),),
        )
        monkeypatch.setattr(
            mount_cmd_module,
            "unmount_connections",
            lambda config, **kwargs: {"changed": False},
        )

        result = CliRunner().invoke(
            cli,
            ["--config", "/local/romcloud.toml", "mount", "stop", "--shutdown"],
        )

        assert result.exit_code == 0, result.output
        assert loaded == [
            ("/local/romcloud.toml", {"resolve_paths": False})
        ]

    def test_boot_start_uses_safe_local_only_initialization(self, monkeypatch):
        config = _fake_config(smb=_fake_smb())
        config.logging.path = "/userdata/romcloud/source/logs"
        loaded = []
        logging_calls = []
        target_calls = []
        monkeypatch.setattr(
            mount_cmd_module,
            "load_config",
            lambda path, **kwargs: loaded.append((path, kwargs)) or config,
        )
        monkeypatch.setattr(
            mount_cmd_module,
            "configure_logging",
            lambda **kwargs: logging_calls.append(kwargs),
        )
        monkeypatch.setattr(
            mount_cmd_module.mount_worker,
            "configured_mounts",
            lambda cfg, **kwargs: target_calls.append(kwargs) or (object(),),
        )
        monkeypatch.setattr(
            mount_cmd_module.mount_worker,
            "all_configured_mounts_are_mounted",
            lambda cfg, **kwargs: target_calls.append(kwargs) or False,
        )
        monkeypatch.setattr(
            mount_cmd_module.mount_worker,
            "is_worker_running",
            lambda home: None,
        )
        monkeypatch.setattr(
            mount_cmd_module.mount_worker,
            "spawn_worker",
            lambda home: 4242,
        )

        result = CliRunner().invoke(
            cli,
            ["--config", "/local/romcloud.toml", "mount", "boot-start"],
        )

        assert result.exit_code == 0, result.output
        assert loaded == [
            ("/local/romcloud.toml", {"resolve_paths": False})
        ]
        assert logging_calls[0]["log_dir"] is None
        assert target_calls == [
            {"resolve_paths": False},
            {"resolve_paths": False},
        ]

    def test_unmounts_savesync_before_catalog(self, monkeypatch):
        calls = []
        monkeypatch.setattr(mount_cmd_module.mount_worker, "stop_worker", lambda *a: None)
        monkeypatch.setattr(
            mount_cmd_module.mount,
            "unmount_cifs_source",
            lambda path: calls.append(path) or True,
        )
        monkeypatch.setattr(
            connections_service,
            "connection_status",
            lambda config: {"state": "disconnected"},
        )

        result = _invoke(
            ["stop"],
            _fake_config(smb=_fake_smb(), saves_mount="/mnt/saves-rw"),
        )

        assert result.exit_code == 0, result.output
        assert calls == ["/mnt/saves-rw", "/mnt/roms"]

    def test_stops_worker_then_unmounts(self, monkeypatch):
        calls = []
        monkeypatch.setattr(mount_cmd_module.mount_worker, "stop_worker", lambda *a, **k: calls.append("stop_worker"))
        monkeypatch.setattr(
            mount_cmd_module.mount, "unmount_cifs_source", lambda *a, **k: calls.append("unmount") or True
        )
        monkeypatch.setattr(
            connections_service,
            "connection_status",
            lambda config: {"state": "disconnected"},
        )

        result = _invoke(["stop"], _fake_config(smb=_fake_smb()))

        assert result.exit_code == 0, result.output
        assert calls == ["stop_worker", "unmount"]
        assert "Unmounted" in result.output

    def test_not_mounted_reports_clearly(self, monkeypatch):
        monkeypatch.setattr(mount_cmd_module.mount_worker, "stop_worker", lambda *a, **k: False)
        monkeypatch.setattr(mount_cmd_module.mount, "unmount_cifs_source", lambda *a, **k: False)
        monkeypatch.setattr(
            connections_service,
            "connection_status",
            lambda config: {"state": "disconnected"},
        )

        result = _invoke(["stop"], _fake_config(smb=_fake_smb()))

        assert result.exit_code == 0, result.output
        assert "Was not mounted" in result.output

    def test_no_smb_configured_is_a_clean_noop(self):
        result = _invoke(["stop"], _fake_config(smb=None))
        assert result.exit_code == 0
        assert "nothing to mount" in result.output

    def test_unmount_failure_does_not_skip_the_other_configured_mount(
        self, monkeypatch
    ):
        calls = []
        monkeypatch.setattr(mount_cmd_module.mount_worker, "stop_worker", lambda *a: None)

        def unmount(path):
            calls.append(path)
            if path == "/mnt/saves-rw":
                raise MountError("remote busy")
            return True

        monkeypatch.setattr(mount_cmd_module.mount, "unmount_cifs_source", unmount)

        result = _invoke(
            ["stop"],
            _fake_config(smb=_fake_smb(), saves_mount="/mnt/saves-rw"),
        )

        assert result.exit_code != 0
        assert calls == ["/mnt/saves-rw", "/mnt/roms"]


class TestRemove:
    def test_stops_worker_and_cleans_state_when_smb_configured(self, monkeypatch):
        calls = []
        monkeypatch.setattr(mount_cmd_module.mount_worker, "stop_worker", lambda *a, **k: calls.append("stop"))
        monkeypatch.setattr(
            mount_cmd_module.mount_worker, "cleanup_runtime_state", lambda *a, **k: calls.append("cleanup")
        )
        monkeypatch.setattr(mount_cmd_module.mount_service, "remove_service", lambda: True)

        result = _invoke(["remove"], _fake_config(smb=_fake_smb()))

        assert result.exit_code == 0, result.output
        assert calls == ["stop", "cleanup"]
        assert "Removed boot service" in result.output

    def test_skips_worker_cleanup_when_not_configured(self, monkeypatch):
        calls = []
        monkeypatch.setattr(mount_cmd_module.mount_worker, "stop_worker", lambda *a, **k: calls.append("stop"))
        monkeypatch.setattr(mount_cmd_module.mount_service, "remove_service", lambda: False)

        result = _invoke(["remove"], _fake_config(smb=None))

        assert result.exit_code == 0, result.output
        assert calls == []
        assert "nothing to do" in result.output.lower()

    def test_unmounts_all_targets_before_removing_service(self, monkeypatch):
        calls = []
        monkeypatch.setattr(
            mount_cmd_module.mount_worker,
            "stop_worker",
            lambda *a: calls.append("stop"),
        )
        monkeypatch.setattr(
            mount_cmd_module.mount_worker,
            "cleanup_runtime_state",
            lambda *a: calls.append("cleanup"),
        )
        monkeypatch.setattr(
            mount_cmd_module.mount,
            "unmount_cifs_source",
            lambda path: calls.append(path) or True,
        )
        monkeypatch.setattr(
            mount_cmd_module.mount_service,
            "remove_service",
            lambda: calls.append("service") or True,
        )

        result = _invoke(
            ["remove"],
            _fake_config(smb=_fake_smb(), saves_mount="/mnt/saves-rw"),
        )

        assert result.exit_code == 0, result.output
        assert calls == [
            "stop",
            "/mnt/saves-rw",
            "/mnt/roms",
            "cleanup",
            "service",
        ]


class TestInstall:
    def test_uses_deterministic_bin_path_not_shutil_which(self, monkeypatch):
        captured = {}
        monkeypatch.setattr(
            mount_cmd_module.mount_service,
            "install_service",
            lambda romcloud_bin: captured.setdefault("bin", romcloud_bin) or Path("/svc/romcloud_mount"),
        )

        result = _invoke(["install"], _fake_config(smb=_fake_smb(), romcloud_home="/opt/romcloud"))

        assert result.exit_code == 0, result.output
        assert captured["bin"] == str(Path("/opt/romcloud") / "bin" / "romcloud")


class TestStatus:
    def test_shows_rich_diagnostics(self, monkeypatch):
        diag = MountDiagnostics(
            configured=True,
            mounted=False,
            worker_pid=555,
            last_state="failed",
            last_detail="SMB authentication failed",
            last_timestamp="2026-08-08T00:00:00Z",
        )
        monkeypatch.setattr(mount_cmd_module.mount_worker, "get_diagnostics", lambda *a, **k: diag)

        result = _invoke(["status"], _fake_config(smb=_fake_smb()))

        assert result.exit_code == 0, result.output
        assert "555" in result.output
        assert "SMB authentication failed" in result.output

    def test_no_smb_configured(self):
        result = _invoke(["status"], _fake_config(smb=None))
        assert result.exit_code == 0
        assert "nothing to mount" in result.output

    def test_shows_cached_endpoint_when_present(self, monkeypatch):
        diag = MountDiagnostics(
            configured=True,
            mounted=True,
            worker_pid=None,
            last_state="success",
            last_detail="",
            last_timestamp="",
            cached_endpoint="192.0.2.10",
        )
        monkeypatch.setattr(mount_cmd_module.mount_worker, "get_diagnostics", lambda *a, **k: diag)

        result = _invoke(["status"], _fake_config(smb=_fake_smb()))

        assert result.exit_code == 0, result.output
        assert "192.0.2.10" in result.output

    def test_omits_cached_endpoint_line_when_absent(self, monkeypatch):
        diag = MountDiagnostics(
            configured=True, mounted=True, worker_pid=None,
            last_state="success", last_detail="", last_timestamp="",
        )
        monkeypatch.setattr(mount_cmd_module.mount_worker, "get_diagnostics", lambda *a, **k: diag)

        result = _invoke(["status"], _fake_config(smb=_fake_smb()))

        assert result.exit_code == 0, result.output
        assert "Cached IP" not in result.output


class TestStartupMigrationFromMountStatus:
    def test_mount_status_triggers_legacy_credentials_migration(self, tmp_path, monkeypatch):
        home = tmp_path / "romcloud"
        config_dir = home / "config"
        source_root = home / "roms"
        source_root.mkdir(parents=True)
        data_root = home / "data"
        data_root.mkdir(parents=True)
        cache_root = home / "cache"
        cache_root.mkdir(parents=True)
        local_roms = home / "local_roms"
        local_roms.mkdir(parents=True)

        config = AppConfig(
            source=SourceConfig(provider="local", rom_root=str(source_root)),
            cache=CacheConfig(path=str(cache_root)),
            local_roms_path=str(local_roms),
            data_path=str(data_root),
            smb=SMBConfig(server="nas.local", share="ROMs", username="alice"),
        )
        config_dir.mkdir(parents=True)
        cfg_path = config_dir / "romcloud.toml"
        write_config(config, str(cfg_path))

        legacy = cfg_path.parent / "smb.credentials"
        legacy.write_text('username=testuser\npassword=testpass\n', encoding="utf-8")
        legacy.chmod(0o600)

        diag = MountDiagnostics(
            configured=True, mounted=False, worker_pid=None,
            last_state="success", last_detail="ok", last_timestamp="x",
        )
        monkeypatch.setattr(mount_cmd_module.mount_worker, "get_diagnostics", lambda *a, **k: diag)

        result = CliRunner().invoke(cli, ["--config", str(cfg_path), "mount", "status"])

        assert result.exit_code == 0, result.output
        assert not legacy.exists()
        assert (cfg_path.parent / "credentials.toml").exists()


class TestExistingStartBehaviorIntact:
    def test_start_mounts_catalog_ro_and_savesync_rw(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            mount_cmd_module.mount_worker,
            "credentials_for_mount",
            lambda config, target: "hunter2",
        )
        calls = []
        monkeypatch.setattr(
            mount_cmd_module.mount,
            "mount_cifs_source",
            lambda **kwargs: calls.append(kwargs) or SimpleNamespace(
                mounted=True, already_mounted=False, detail="mounted"
            ),
        )

        result = _invoke(
            ["start"],
            _fake_config(
                smb=_fake_smb(), saves_mount="/mnt/saves-rw", romcloud_home=str(tmp_path)
            ),
        )

        assert result.exit_code == 0, result.output
        assert [item["mount_point"] for item in calls] == ["/mnt/roms", "/mnt/saves-rw"]
        assert [item["server"] for item in calls] == ["nas.local", "data-nas.local"]
        assert [item["share"] for item in calls] == ["ROMs", "ROMCloud"]
        assert calls[0]["read_only"] is True
        assert calls[1]["read_only"] is False

    def test_start_still_blocks_and_reports_mounted(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            mount_cmd_module.mount_worker,
            "credentials_for_mount",
            lambda *a: "hunter2",
        )
        monkeypatch.setattr(
            mount_cmd_module.mount,
            "mount_cifs_source",
            lambda **k: SimpleNamespace(mounted=True, already_mounted=False, detail="mounted"),
        )

        result = _invoke(
            ["start"], _fake_config(smb=_fake_smb(), romcloud_home=str(tmp_path))
        )

        assert result.exit_code == 0, result.output
        assert "Mounted." in result.output

    def test_start_requires_password(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            mount_cmd_module.mount_worker,
            "credentials_for_mount",
            lambda *a: None,
        )
        result = _invoke(
            ["start"], _fake_config(smb=_fake_smb(), romcloud_home=str(tmp_path))
        )
        assert result.exit_code != 0
        assert "No SMB password stored" in result.output

    def test_start_rolls_back_only_mounts_created_before_later_failure(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setattr(
            mount_cmd_module.mount_worker,
            "credentials_for_mount",
            lambda config, target: "secret",
        )
        attempts = []
        unmounted = []

        def mount_target(**kwargs):
            attempts.append(kwargs["mount_point"])
            if kwargs["mount_point"] == "/mnt/saves-rw":
                raise MountError("remote unavailable")
            return SimpleNamespace(mounted=True, already_mounted=False, detail="mounted")

        monkeypatch.setattr(mount_cmd_module.mount, "mount_cifs_source", mount_target)
        monkeypatch.setattr(
            mount_cmd_module.mount,
            "unmount_cifs_source",
            lambda path: unmounted.append(path) or True,
        )

        result = _invoke(
            ["start"],
            _fake_config(
                smb=_fake_smb(), saves_mount="/mnt/saves-rw", romcloud_home=str(tmp_path)
            ),
        )

        assert result.exit_code != 0
        assert attempts == ["/mnt/roms", "/mnt/saves-rw"]
        assert unmounted == ["/mnt/roms"]
