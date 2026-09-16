from __future__ import annotations

import json
import os
from pathlib import Path

from click.testing import CliRunner

from romcloud.core.models.troubleshoot import TroubleshootFinding, TroubleshootReport
from romcloud.infrastructure.config import (
    AppConfig,
    CacheConfig,
    SourceConfig,
    load_config_read_only,
    write_config,
)
from romcloud.troubleshoot import (
    ActivitySnapshot,
    ActivityState,
    TroubleshootPaths,
    collect_diagnostics,
    run_quick_repair,
)


def _install(tmp_path: Path, *, catalog: bytes | None = None) -> tuple[Path, AppConfig]:
    home = tmp_path / "home"
    config_path = home / "config" / "romcloud.toml"
    source = tmp_path / "source"
    source.mkdir()
    local = tmp_path / "roms"
    local.mkdir()
    cache = tmp_path / "cache"
    cache.mkdir()
    data = home / "data"
    data.mkdir(parents=True)
    config = AppConfig(
        source=SourceConfig("local", str(source)),
        cache=CacheConfig(str(cache), min_free_gb=0),
        local_roms_path=str(local),
        data_path=str(data),
    )
    config_path.parent.mkdir(parents=True)
    write_config(config, str(config_path))
    if catalog is not None:
        (data / "catalog.db").write_bytes(catalog)
    return config_path, config


def _paths(tmp_path: Path) -> TroubleshootPaths:
    return TroubleshootPaths(
        auto_savesync_hook=tmp_path / "system" / "hook",
        mount_service=tmp_path / "system" / "service",
        services_config=tmp_path / "system" / "batocera.conf",
        es_stock=tmp_path / "system" / "es_systems.cfg",
        es_override=tmp_path / "system" / "override.cfg",
    )


def _inactive() -> ActivitySnapshot:
    inactive = ActivityState("inactive")
    return ActivitySnapshot(inactive, inactive, inactive, inactive, inactive, inactive)


def _tree_bytes(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file()
    }


def test_result_aggregation_and_quick_repair_eligibility() -> None:
    report = TroubleshootReport(
        (
            TroubleshootFinding("ok", "runtime", "healthy", "info", "okay"),
            TroubleshootFinding(
                "fix", "runtime", "warning", "warning", "stale", fixability="automatic"
            ),
        )
    )
    assert report.overall_status == "warning"
    assert report.quick_repair_available is True
    assert report.summary["healthy"] == 1


def test_read_only_config_loader_does_not_migrate_legacy_paths_or_credentials(tmp_path: Path) -> None:
    config_path, _ = _install(tmp_path)
    raw = config_path.read_text(encoding="utf-8")
    raw = "\n".join(
        'rom_root = "/userdata/romcloud-source"' if line.startswith("rom_root = ") else line
        for line in raw.splitlines()
    ) + "\n"
    config_path.write_text(raw, encoding="utf-8")
    legacy = config_path.parent / "smb.credentials"
    legacy.write_text("username=a\npassword=secret\n", encoding="utf-8")
    before = _tree_bytes(tmp_path)

    loaded = load_config_read_only(str(config_path))

    assert loaded.source.rom_root == "/userdata/romcloud-source"
    assert _tree_bytes(tmp_path) == before


def test_missing_catalog_diagnostics_never_create_database_or_other_state(tmp_path: Path, monkeypatch) -> None:
    config_path, config = _install(tmp_path)
    monkeypatch.setattr("romcloud.troubleshoot._inspect_browser", lambda *_: None)
    before = _tree_bytes(tmp_path)

    report, _ = collect_diagnostics(
        config_path, paths=_paths(tmp_path), activity=_inactive()
    )

    assert next(item for item in report.findings if item.id == "database.catalog").status == "error"
    assert not (Path(config.data_path) / "catalog.db").exists()
    assert _tree_bytes(tmp_path) == before


def test_corrupt_catalog_is_reported_and_preserved(tmp_path: Path, monkeypatch) -> None:
    config_path, config = _install(tmp_path, catalog=b"not sqlite")
    monkeypatch.setattr("romcloud.troubleshoot._inspect_browser", lambda *_: None)
    path = Path(config.data_path) / "catalog.db"
    before = path.read_bytes()

    report, _ = collect_diagnostics(config_path, paths=_paths(tmp_path), activity=_inactive())

    assert next(item for item in report.findings if item.id == "database.catalog").status == "error"
    assert path.read_bytes() == before


def test_malformed_direct_manifest_is_not_treated_as_missing(tmp_path: Path, monkeypatch) -> None:
    config_path, config = _install(tmp_path)
    manifest = Path(config.data_path) / "direct-links.json"
    manifest.write_text("{broken", encoding="utf-8")
    monkeypatch.setattr("romcloud.troubleshoot._inspect_browser", lambda *_: None)

    report, _ = collect_diagnostics(config_path, paths=_paths(tmp_path), activity=_inactive())

    finding = next(item for item in report.findings if item.id == "presentation.direct_manifest")
    assert finding.status == "error"
    assert "malformed" in finding.message.lower()
    assert manifest.read_text(encoding="utf-8") == "{broken"


def test_quick_repair_rewrites_owned_core_wrappers_then_second_run_is_noop(tmp_path: Path, monkeypatch) -> None:
    config_path, _ = _install(tmp_path)
    home = config_path.parent.parent
    (home / "venv" / "bin").mkdir(parents=True)
    python = home / "venv" / "bin" / "python"
    python.write_text("", encoding="utf-8")
    monkeypatch.setattr("romcloud.troubleshoot._inspect_browser", lambda *_: None)

    first = run_quick_repair(config_path, paths=_paths(tmp_path), activity=_inactive())
    second = run_quick_repair(config_path, paths=_paths(tmp_path), activity=_inactive())

    assert (home / "bin" / "romcloud").is_file()
    assert (home / "bin" / "romcloud-run").is_file()
    assert any(item.status == "fixed" for item in first.findings if item.id.startswith("runtime.") and "wrapper" in item.id)
    assert not any(item.status == "fixed" for item in second.findings if item.id.startswith("runtime.") and "wrapper" in item.id)


def test_healthcheck_and_troubleshoot_share_collectors_and_do_not_migrate(tmp_path: Path, monkeypatch) -> None:
    if os.name == "nt":
        import pytest

        pytest.skip("The full CLI imports POSIX fcntl-backed cache coordination.")
    from romcloud.cli.main import cli

    config_path, _ = _install(tmp_path)
    legacy = config_path.parent / "smb.credentials"
    legacy.write_text("username=a\npassword=secret\n", encoding="utf-8")
    monkeypatch.setattr("romcloud.troubleshoot._inspect_browser", lambda *_: None)

    health = CliRunner().invoke(cli, ["--config", str(config_path), "healthcheck"])
    troubleshoot = CliRunner().invoke(
        cli, ["--config", str(config_path), "troubleshoot", "--json-output"]
    )

    assert "Catalog" in health.output
    assert json.loads(troubleshoot.output)["operation"] == "troubleshoot"
    assert legacy.exists()
    assert not (config_path.parent / "credentials.toml").exists()
