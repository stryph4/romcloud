"""Unit tests for the SMB mount diagnostic line added to `romcloud healthcheck`.

Only relevant when `[smb]` is configured; must never crash healthcheck even
if the underlying diagnostics call fails.
"""

from __future__ import annotations

from click.testing import CliRunner

from romcloud.cli.main import cli
from romcloud.cli.commands.healthcheck import healthcheck_cmd
from romcloud.infrastructure.config import AppConfig, CacheConfig, SMBConfig, SourceConfig
from romcloud.infrastructure.config import write_config
from romcloud.infrastructure.mount_worker import MountDiagnostics


def _build_config(tmp_path, smb=None):
    source_root = tmp_path / "roms"
    source_root.mkdir()
    data_root = tmp_path / "data"
    data_root.mkdir()
    return AppConfig(
        source=SourceConfig(provider="local", rom_root=str(source_root)),
        cache=CacheConfig(path=str(tmp_path / "cache")),
        local_roms_path=str(tmp_path / "local_roms"),
        data_path=str(data_root),
        smb=smb,
    )


def _invoke(config):
    return CliRunner().invoke(healthcheck_cmd, [], obj={"config": config})


class TestMountDiagnosticLine:
    def test_absent_when_no_smb_configured(self, tmp_path):
        result = _invoke(_build_config(tmp_path, smb=None))
        assert "SMB source mounted" not in result.output
        assert "Source reachable (Local filesystem)" in result.output

    def test_shown_and_passing_when_mounted(self, tmp_path, monkeypatch):
        diag = MountDiagnostics(
            configured=True, mounted=True, worker_pid=None,
            last_state="success", last_detail="mounted", last_timestamp="x",
        )
        monkeypatch.setattr("romcloud.infrastructure.mount_worker.get_diagnostics", lambda *a, **k: diag)

        config = _build_config(tmp_path, smb=SMBConfig(server="nas.local", share="ROMs"))
        result = _invoke(config)

        assert "Source reachable (SMB)" in result.output
        assert "SMB source mounted" in result.output
        assert "✓  SMB source mounted" in result.output

    def test_shown_and_failing_with_detail_when_not_mounted(self, tmp_path, monkeypatch):
        diag = MountDiagnostics(
            configured=True, mounted=False, worker_pid=None,
            last_state="failed", last_detail="SMB authentication failed", last_timestamp="x",
        )
        monkeypatch.setattr("romcloud.infrastructure.mount_worker.get_diagnostics", lambda *a, **k: diag)

        config = _build_config(tmp_path, smb=SMBConfig(server="nas.local", share="ROMs"))
        result = _invoke(config)

        assert "Source reachable (SMB)" in result.output
        assert "✗  SMB source mounted" in result.output
        assert "last attempt failed" in result.output
        assert result.exit_code != 0

    def test_never_crashes_healthcheck_if_diagnostics_raise(self, tmp_path, monkeypatch):
        def _boom(*a, **k):
            raise RuntimeError("cannot read /proc/mounts")

        monkeypatch.setattr("romcloud.infrastructure.mount_worker.get_diagnostics", _boom)

        config = _build_config(tmp_path, smb=SMBConfig(server="nas.local", share="ROMs"))
        result = _invoke(config)

        # A failed check legitimately exits 1 (normal `ctx.exit`, not a crash) —
        # what must never happen is the RuntimeError itself propagating out.
        assert not isinstance(result.exception, RuntimeError)
        assert "Source reachable (SMB)" in result.output
        assert "SMB source mounted" in result.output
        assert "error checking status" in result.output


class TestStartupMigrationFromHealthcheck:
    def test_healthcheck_triggers_legacy_credentials_migration_idempotently(self, tmp_path, monkeypatch):
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
            configured=True, mounted=True, worker_pid=None,
            last_state="success", last_detail="ok", last_timestamp="x",
        )
        monkeypatch.setattr("romcloud.infrastructure.mount_worker.get_diagnostics", lambda *a, **k: diag)

        runner = CliRunner()
        result1 = runner.invoke(cli, ["--config", str(cfg_path), "healthcheck"])
        result2 = runner.invoke(cli, ["--config", str(cfg_path), "healthcheck"])

        assert result1.exit_code == 0, result1.output
        assert result2.exit_code == 0, result2.output
        assert not legacy.exists()
        assert (cfg_path.parent / "credentials.toml").exists()
