"""The legacy command is now a renderer over shared pure diagnostics."""

from __future__ import annotations

from pathlib import Path

from click.testing import CliRunner

from romcloud.cli.commands.healthcheck import healthcheck_cmd
from romcloud.core.models.troubleshoot import TroubleshootFinding, TroubleshootReport
from romcloud.infrastructure.config import (
    AppConfig,
    CacheConfig,
    RemoteDataConfig,
    SFTPConfig,
    SourceConfig,
    write_config,
)
from romcloud.core.storage import StorageAccessResult
from romcloud.troubleshoot import ActivitySnapshot, ActivityState, collect_diagnostics


def _inactive() -> ActivitySnapshot:
    state = ActivityState("inactive")
    return ActivitySnapshot(state, state, state, state, state, state)


def test_healthcheck_renders_shared_findings_and_returns_nonzero(monkeypatch) -> None:
    report = TroubleshootReport(
        (TroubleshootFinding("bad", "database", "error", "error", "Catalog is corrupt."),)
    )
    monkeypatch.setattr(
        "romcloud.cli.commands.healthcheck.collect_diagnostics",
        lambda _path: (report, None),
    )

    result = CliRunner().invoke(
        healthcheck_cmd, [], obj={"config_path": "/does/not/matter"}
    )

    assert result.exit_code == 1
    assert "Catalog is corrupt" in result.output


def test_source_disabled_configuration_never_constructs_provider(tmp_path: Path, monkeypatch) -> None:
    home = tmp_path / "home"
    config_path = home / "config" / "romcloud.toml"
    for path in (home / "data", tmp_path / "roms", tmp_path / "remote", tmp_path / "cache"):
        path.mkdir(parents=True)
    config = AppConfig(
        source=SourceConfig("none", "", ()),
        cache=CacheConfig(str(tmp_path / "cache"), min_free_gb=0),
        local_roms_path=str(tmp_path / "roms"),
        data_path=str(home / "data"),
        remote_data=RemoteDataConfig("local", str(tmp_path / "remote")),
    )
    config_path.parent.mkdir(parents=True)
    write_config(config, str(config_path))
    monkeypatch.setattr("romcloud.troubleshoot._inspect_browser", lambda *_: None)

    report, _ = collect_diagnostics(config_path, activity=_inactive())

    finding = next(item for item in report.findings if item.id == "source.connectivity")
    assert finding.status == "healthy"
    assert "disabled" in finding.message.lower()


def test_valid_read_only_sftp_is_healthy_and_never_calls_write_probe(tmp_path: Path, monkeypatch) -> None:
    home = tmp_path / "home"
    config_path = home / "config" / "romcloud.toml"
    for path in (home / "data", tmp_path / "roms", tmp_path / "cache"):
        path.mkdir(parents=True)
    sftp = SFTPConfig(
        host="example.test", username="alice", host_key_fingerprint="SHA256:test"
    )
    config = AppConfig(
        source=SourceConfig("none", "", ()),
        cache=CacheConfig(str(tmp_path / "cache"), min_free_gb=0),
        local_roms_path=str(tmp_path / "roms"),
        data_path=str(home / "data"),
        remote_data=RemoteDataConfig("sftp", "/romcloud", sftp=sftp),
    )
    config_path.parent.mkdir(parents=True)
    write_config(config, str(config_path))
    monkeypatch.setattr(
        "romcloud.infrastructure.providers.sftp.SFTPProvider.validate_access",
        lambda *_: StorageAccessResult(True, True, False, None, "read-only"),
    )
    monkeypatch.setattr(
        "romcloud.infrastructure.providers.sftp.SFTPProvider._probe_write",
        lambda *_: (_ for _ in ()).throw(AssertionError("write probe called")),
    )
    monkeypatch.setattr("romcloud.troubleshoot._inspect_browser", lambda *_: None)

    report, _ = collect_diagnostics(config_path, activity=_inactive())

    finding = next(item for item in report.findings if item.id == "remote_data.connectivity")
    assert finding.status == "healthy"
    assert "read-only" in finding.detail.lower()


def test_provider_exception_becomes_finding(tmp_path: Path, monkeypatch) -> None:
    home = tmp_path / "home"
    config_path = home / "config" / "romcloud.toml"
    for path in (home / "data", tmp_path / "roms", tmp_path / "cache"):
        path.mkdir(parents=True)
    sftp = SFTPConfig(
        host="example.test", username="alice", host_key_fingerprint="SHA256:test"
    )
    config = AppConfig(
        source=SourceConfig("sftp", "/roms"),
        sftp=sftp,
        cache=CacheConfig(str(tmp_path / "cache"), min_free_gb=0),
        local_roms_path=str(tmp_path / "roms"),
        data_path=str(home / "data"),
    )
    config_path.parent.mkdir(parents=True)
    write_config(config, str(config_path))
    monkeypatch.setattr(
        "romcloud.infrastructure.providers.sftp.SFTPProvider.validate_access",
        lambda *_: (_ for _ in ()).throw(RuntimeError("authentication rejected")),
    )
    monkeypatch.setattr("romcloud.troubleshoot._inspect_browser", lambda *_: None)

    report, _ = collect_diagnostics(config_path, activity=_inactive())

    finding = next(item for item in report.findings if item.id == "source.connectivity")
    assert finding.status == "error"
    assert "authentication rejected" in finding.detail
