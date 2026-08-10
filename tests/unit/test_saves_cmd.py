"""Unit tests for `romcloud saves` (SaveSync v1 CLI)."""

from __future__ import annotations

import json

from click.testing import CliRunner

from romcloud.cli.main import cli
from romcloud.infrastructure.config import AppConfig, CacheConfig, SavesConfig, SourceConfig, write_config


def _config_path(tmp_path, *, xbox_enabled: bool = False):
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

    config = AppConfig(
        source=SourceConfig(provider="local", rom_root=str(source_root)),
        cache=CacheConfig(path=str(cache_root)),
        local_roms_path=str(local_roms),
        data_path=str(data_root),
        saves=SavesConfig(local_path=str(saves_root), xbox_enabled=xbox_enabled),
    )
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    cfg_path = config_dir / "romcloud.toml"
    write_config(config, str(cfg_path))
    return cfg_path


def _invoke(cfg_path, args, input=None):
    return CliRunner().invoke(cli, ["--config", str(cfg_path), "saves", *args], input=input)


class TestStatus:
    def test_reports_never_synced(self, tmp_path):
        result = _invoke(_config_path(tmp_path), ["status"])
        assert result.exit_code == 0, result.output
        assert "Last upload: never" in result.output
        assert "Last download: never" in result.output
        assert "disabled" in result.output  # xbox opt-in


class TestPreview:
    def test_preview_upload_lists_added_entries(self, tmp_path):
        cfg_path = _config_path(tmp_path)
        (tmp_path / "saves" / "psx").mkdir(parents=True)
        (tmp_path / "saves" / "psx" / "Game.srm").write_bytes(b"data")

        result = _invoke(cfg_path, ["preview-upload"])

        assert result.exit_code == 0, result.output
        assert "Added:     1" in result.output
        assert "[added] psx/Game.srm" in result.output

    def test_preview_excludes_unsupported_system(self, tmp_path):
        cfg_path = _config_path(tmp_path)
        (tmp_path / "saves" / "dolphin").mkdir(parents=True)
        (tmp_path / "saves" / "dolphin" / "Game.gci").write_bytes(b"x")

        result = _invoke(cfg_path, ["preview-upload"])

        assert "Added:     0" in result.output
        assert "dolphin" not in result.output


class TestUploadConfirmation:
    def test_declining_confirmation_does_not_commit(self, tmp_path):
        cfg_path = _config_path(tmp_path)
        (tmp_path / "saves" / "psx").mkdir(parents=True)
        (tmp_path / "saves" / "psx" / "Game.srm").write_bytes(b"data")

        result = _invoke(cfg_path, ["upload"], input="n\n")

        assert result.exit_code == 0
        assert "Cancelled." in result.output
        status = _invoke(cfg_path, ["status"]).output
        assert "Last upload: never" in status

    def test_yes_flag_skips_prompt_and_commits(self, tmp_path):
        cfg_path = _config_path(tmp_path)
        (tmp_path / "saves" / "psx").mkdir(parents=True)
        (tmp_path / "saves" / "psx" / "Game.srm").write_bytes(b"data")

        result = _invoke(cfg_path, ["upload", "--yes"])

        assert result.exit_code == 0, result.output
        assert "Done." in result.output
        status = _invoke(cfg_path, ["status"]).output
        assert "Last upload: never" not in status

    def test_nothing_to_do_when_already_in_sync(self, tmp_path):
        cfg_path = _config_path(tmp_path)
        (tmp_path / "saves" / "psx").mkdir(parents=True)
        (tmp_path / "saves" / "psx" / "Game.srm").write_bytes(b"data")
        _invoke(cfg_path, ["upload", "--yes"])

        result = _invoke(cfg_path, ["upload", "--yes"])

        assert "Nothing to do" in result.output


class TestDownload:
    def test_download_after_upload_round_trips(self, tmp_path):
        cfg_path = _config_path(tmp_path)
        (tmp_path / "saves" / "psx").mkdir(parents=True)
        (tmp_path / "saves" / "psx" / "Game.srm").write_bytes(b"data")
        _invoke(cfg_path, ["upload", "--yes"])
        (tmp_path / "saves" / "psx" / "Game.srm").unlink()

        result = _invoke(cfg_path, ["download", "--yes"])

        assert result.exit_code == 0, result.output
        assert (tmp_path / "saves" / "psx" / "Game.srm").read_bytes() == b"data"


class TestXboxToggle:
    def test_enable_then_disable_persists_setting(self, tmp_path):
        cfg_path = _config_path(tmp_path)

        enable_result = _invoke(cfg_path, ["xbox-enable"])
        assert "must transfer the entire virtual" in enable_result.output
        assert "enabled" in _invoke(cfg_path, ["status"]).output

        _invoke(cfg_path, ["xbox-disable"])
        assert "disabled" in _invoke(cfg_path, ["status"]).output

    def test_enable_reports_current_hdd_size(self, tmp_path):
        cfg_path = _config_path(tmp_path)
        (tmp_path / "saves" / "xbox").mkdir(parents=True)
        (tmp_path / "saves" / "xbox" / "xbox_hdd.qcow2").write_bytes(b"x" * 2048)

        result = _invoke(cfg_path, ["xbox-enable"])

        assert "Current size:" in result.output

    def test_uploading_xbox_hdd_only_after_enabling(self, tmp_path):
        cfg_path = _config_path(tmp_path)
        (tmp_path / "saves" / "xbox").mkdir(parents=True)
        (tmp_path / "saves" / "xbox" / "xbox_hdd.qcow2").write_bytes(b"vhd" * 1000)

        before = _invoke(cfg_path, ["preview-upload"])
        assert "Added:     0" in before.output

        _invoke(cfg_path, ["xbox-enable"])
        after = _invoke(cfg_path, ["preview-upload"])
        assert "Added:     1" in after.output
        assert "xbox/xbox_hdd.qcow2" in after.output
