"""Unit tests for `romcloud uidata savesync-*` — the GUI's SaveSync bridge."""

from __future__ import annotations

import json

from click.testing import CliRunner

from romcloud.cli.main import cli
from romcloud.infrastructure.config import AppConfig, CacheConfig, SourceConfig, write_config


def _build_config(tmp_path):
    source_root = tmp_path / "roms"
    source_root.mkdir()
    data_root = tmp_path / "data"
    data_root.mkdir()
    local_roms = tmp_path / "local_roms"
    local_roms.mkdir()
    cache_root = tmp_path / "cache"
    cache_root.mkdir()
    saves_root = tmp_path / "saves"
    saves_root.mkdir()

    from romcloud.infrastructure.config import SavesConfig

    return AppConfig(
        source=SourceConfig(provider="local", rom_root=str(source_root)),
        cache=CacheConfig(path=str(cache_root)),
        local_roms_path=str(local_roms),
        data_path=str(data_root),
        saves=SavesConfig(local_path=str(saves_root)),
    )


def _config_path(tmp_path):
    config = _build_config(tmp_path)
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    cfg_path = config_dir / "romcloud.toml"
    write_config(config, str(cfg_path))
    return cfg_path


def _invoke(cfg_path, args, input=None):
    return CliRunner().invoke(cli, ["--config", str(cfg_path), "uidata", *args], input=input)


class TestSavesyncStatus:
    def test_reports_connectivity_and_defaults(self, tmp_path):
        result = _invoke(_config_path(tmp_path), ["savesync-status"])

        assert result.exit_code == 0, result.output
        payload = json.loads(result.output.strip())
        assert payload["ok"] is True
        assert payload["remote_reachable"] is True
        assert payload["xbox_enabled"] is False
        assert payload["xbox_hdd_size_bytes"] is None
        assert payload["last_upload"] is None
        assert payload["last_download"] is None


class TestSavesyncPreview:
    def test_preview_upload_returns_diff_and_counts(self, tmp_path):
        cfg_path = _config_path(tmp_path)
        (tmp_path / "saves" / "psx").mkdir(parents=True)
        (tmp_path / "saves" / "psx" / "Game.srm").write_bytes(b"save-data")

        result = _invoke(cfg_path, ["savesync-preview"], input=json.dumps({"direction": "upload"}))

        assert result.exit_code == 0, result.output
        payload = json.loads(result.output.strip())
        assert payload["ok"] is True
        assert payload["added"] == 1
        assert payload["diff"]["direction"] == "upload"
        assert payload["diff"]["entries"][0]["relative_path"] == "psx/Game.srm"

    def test_missing_direction_is_a_clean_error(self, tmp_path):
        result = _invoke(_config_path(tmp_path), ["savesync-preview"], input=json.dumps({}))

        assert result.exit_code == 1
        payload = json.loads(result.output.strip())
        assert payload["ok"] is False
        assert "Traceback" not in result.output


class TestSavesyncCommit:
    def test_commit_upload_round_trips_diff_from_preview(self, tmp_path):
        cfg_path = _config_path(tmp_path)
        (tmp_path / "saves" / "psx").mkdir(parents=True)
        (tmp_path / "saves" / "psx" / "Game.srm").write_bytes(b"save-data")

        preview = _invoke(cfg_path, ["savesync-preview"], input=json.dumps({"direction": "upload"}))
        diff_payload = json.loads(preview.output.strip())["diff"]

        commit = _invoke(
            cfg_path,
            ["savesync-commit"],
            input=json.dumps({"direction": "upload", "diff": diff_payload}),
        )

        assert commit.exit_code == 0, commit.output
        payload = json.loads(commit.output.strip())
        assert payload["ok"] is True
        assert payload["record"]["artifact_count"] == 1

        status = json.loads(_invoke(cfg_path, ["savesync-status"]).output.strip())
        assert status["last_upload"] is not None


class TestSavesyncSettings:
    def test_enable_xbox_persists_to_config(self, tmp_path):
        cfg_path = _config_path(tmp_path)

        result = _invoke(cfg_path, ["savesync-settings"], input=json.dumps({"xbox_enabled": True}))

        assert result.exit_code == 0, result.output
        payload = json.loads(result.output.strip())
        assert payload["ok"] is True
        assert payload["xbox_enabled"] is True

        status = json.loads(_invoke(cfg_path, ["savesync-status"]).output.strip())
        assert status["xbox_enabled"] is True
