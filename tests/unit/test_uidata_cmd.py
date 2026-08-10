"""Unit tests for `romcloud uidata` — the hidden JSON contract used by the
graphical Ports UI (`ports_gfx`, which runs under Batocera's system Python).

Every command must:
- print exactly one JSON object to stdout (nothing else),
- exit 0 on success / 1 on failure,
- never let an exception's traceback reach stdout.
"""

from __future__ import annotations

import json

from click.testing import CliRunner

from romcloud.cli.main import cli
from romcloud.infrastructure.config import AppConfig, CacheConfig, SMBConfig, SourceConfig, write_config


def _build_config(tmp_path, smb=None):
    source_root = tmp_path / "roms"
    source_root.mkdir()
    data_root = tmp_path / "data"
    data_root.mkdir()
    local_roms = tmp_path / "local_roms"
    local_roms.mkdir()
    cache_root = tmp_path / "cache"
    cache_root.mkdir()

    return AppConfig(
        source=SourceConfig(provider="local", rom_root=str(source_root)),
        cache=CacheConfig(path=str(cache_root)),
        local_roms_path=str(local_roms),
        data_path=str(data_root),
        smb=smb,
    )


def _write_and_invoke(tmp_path, args):
    config = _build_config(tmp_path)
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    cfg_path = config_dir / "romcloud.toml"
    write_config(config, str(cfg_path))

    runner = CliRunner()
    return runner.invoke(cli, ["--config", str(cfg_path), "uidata", *args])


class TestHiddenFromHelp:
    def test_uidata_not_shown_in_top_level_help(self):
        result = CliRunner().invoke(cli, ["--help"])
        assert result.exit_code == 0
        assert "uidata" not in result.output


class TestSetupBridge:
    def test_fresh_setup_status_works_without_config(self, tmp_path):
        cfg_path = tmp_path / "missing" / "romcloud.toml"
        result = CliRunner().invoke(cli, ["--config", str(cfg_path), "uidata", "setup-status"])
        assert result.exit_code == 0
        payload = json.loads(result.output)
        assert payload["ok"] is True
        assert payload["state"] == "fresh"

    def test_setup_request_is_read_from_stdin_not_argv(self, tmp_path, monkeypatch):
        import romcloud.cli.commands.uidata as uidata_module

        captured = {}

        def discover(payload):
            captured.update(payload)
            return {"shares": [{"name": "ROMs", "comment": ""}]}

        monkeypatch.setattr(uidata_module, "discover_shares", discover)
        request = {"server": "nas", "username": "alice", "password": "private"}
        result = CliRunner().invoke(
            cli,
            ["--config", str(tmp_path / "missing.toml"), "uidata", "setup-discover"],
            input=json.dumps(request),
        )
        assert result.exit_code == 0
        assert captured == request
        assert "private" not in result.output

    def test_malformed_setup_request_is_json_error(self, tmp_path):
        result = CliRunner().invoke(
            cli,
            ["--config", str(tmp_path / "missing.toml"), "uidata", "setup-discover"],
            input="not-json",
        )
        assert result.exit_code == 1
        payload = json.loads(result.output)
        assert payload["ok"] is False
        assert "Traceback" not in result.output

    def test_unexpected_setup_error_redacts_request_password(self, tmp_path, monkeypatch):
        import romcloud.cli.commands.uidata as uidata_module

        password = "gui-visible-secret"
        monkeypatch.setattr(
            uidata_module,
            "discover_shares",
            lambda payload: (_ for _ in ()).throw(
                RuntimeError(f"backend echoed {password}")
            ),
        )

        result = CliRunner().invoke(
            cli,
            ["--config", str(tmp_path / "missing.toml"), "uidata", "setup-discover"],
            input=json.dumps(
                {"server": "nas", "username": "alice", "password": password}
            ),
        )

        assert result.exit_code == 1
        assert password not in result.output
        assert "***" in result.output


class TestStatus:
    def test_emits_single_json_object(self, tmp_path):
        result = _write_and_invoke(tmp_path, ["status"])

        assert result.exit_code == 0, result.output
        lines = [line for line in result.output.splitlines() if line.strip()]
        assert len(lines) == 1
        payload = json.loads(lines[0])
        assert payload["ok"] is True
        assert payload["games_total"] == 0
        assert payload["cached"] == 0
        assert payload["pinned"] == 0
        assert payload["source_type"] == "Local filesystem"
        assert payload["source_internal_provider"] == "local"
        assert payload["source_description"] == str(tmp_path / "roms")

    def test_emits_smb_source_summary_when_smb_configured(self, tmp_path):
        config = _build_config(tmp_path, smb=SMBConfig(server="nas.local", share="ROMs", username="alice"))
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        cfg_path = config_dir / "romcloud.toml"
        write_config(config, str(cfg_path))

        runner = CliRunner()
        result = runner.invoke(cli, ["--config", str(cfg_path), "uidata", "status"])

        assert result.exit_code == 0, result.output
        payload = json.loads(result.output.strip())
        assert payload["ok"] is True
        assert payload["source_type"] == "SMB"
        assert payload["source_internal_provider"] == "local"
        assert payload["source_server"] == "nas.local"
        assert payload["source_share"] == "ROMs"
        assert payload["source_description"] == "nas.local:ROMs"


class TestRefresh:
    def test_emits_added_skipped_removed_errors(self, tmp_path, monkeypatch):
        from romcloud.integrations.batocera import es_config

        monkeypatch.setattr(
            es_config,
            "refresh",
            lambda systems: type(
                "Result", (), {"included_systems": [], "missing_systems": []}
            )(),
        )
        result = _write_and_invoke(tmp_path, ["refresh"])

        assert result.exit_code == 0, result.output
        payload = json.loads(result.output.strip())
        assert payload["ok"] is True
        assert payload["added"] == 0
        assert payload["skipped"] == 0
        assert payload["removed"] == 0
        assert payload["errors"] == []
        assert payload["es_systems"] == []
        assert payload["es_restart_required"] is True


class TestHealthcheck:
    def test_emits_source_reachability(self, tmp_path):
        result = _write_and_invoke(tmp_path, ["healthcheck"])

        assert result.exit_code == 0, result.output
        payload = json.loads(result.output.strip())
        assert payload["ok"] is True
        assert payload["source_type"] == "Local filesystem"
        assert payload["source_internal_provider"] == "local"
        assert payload["source_description"] == str(tmp_path / "roms")
        assert payload["source_reachable"] is True

    def test_emits_smb_labels_and_metadata_when_smb_configured(self, tmp_path):
        config = _build_config(tmp_path, smb=SMBConfig(server="nas.local", share="ROMs", username="alice"))
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        cfg_path = config_dir / "romcloud.toml"
        write_config(config, str(cfg_path))

        runner = CliRunner()
        result = runner.invoke(cli, ["--config", str(cfg_path), "uidata", "healthcheck"])

        assert result.exit_code == 0, result.output
        payload = json.loads(result.output.strip())
        assert payload["ok"] is True
        assert payload["source_type"] == "SMB"
        assert payload["source_internal_provider"] == "local"
        assert payload["source_server"] == "nas.local"
        assert payload["source_share"] == "ROMs"
        assert payload["source_description"] == "nas.local:ROMs"


class TestCacheStatus:
    def test_emits_cache_summary_fields(self, tmp_path):
        result = _write_and_invoke(tmp_path, ["cache-status"])

        assert result.exit_code == 0, result.output
        payload = json.loads(result.output.strip())
        assert payload["ok"] is True
        for key in ("complete", "pinned", "total_bytes", "free_bytes", "max_bytes", "min_free_bytes"):
            assert key in payload


class TestErrorsNeverLeakTracebacks:
    def test_container_failure_is_reported_as_json_not_traceback(self, tmp_path, monkeypatch):
        import romcloud.cli.commands.uidata as uidata_module

        def _boom(ctx):
            raise RuntimeError("container build failed")

        monkeypatch.setattr(uidata_module, "get_container", _boom)

        result = _write_and_invoke(tmp_path, ["status"])

        assert result.exit_code == 1
        payload = json.loads(result.output.strip())
        assert payload["ok"] is False
        assert "container build failed" in payload["error"]
        assert "Traceback" not in result.output
