"""Unit tests for `romcloud uidata savesync-*` — the GUI's SaveSync bridge."""

from __future__ import annotations

import json

from click.testing import CliRunner

from romcloud.cli.main import cli
from romcloud.infrastructure.config import (
    AppConfig,
    CacheConfig,
    RemoteDataConfig,
    SourceConfig,
    write_config,
)


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
    remote_data_root = tmp_path / "remote-data"
    remote_data_root.mkdir()

    from romcloud.infrastructure.config import SavesConfig

    return AppConfig(
        source=SourceConfig(provider="local", rom_root=source_root.as_posix()),
        cache=CacheConfig(path=cache_root.as_posix()),
        local_roms_path=local_roms.as_posix(),
        data_path=data_root.as_posix(),
        remote_data=RemoteDataConfig(provider="local", root=remote_data_root.as_posix()),
        saves=SavesConfig(local_path=saves_root.as_posix()),
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
    def test_reports_local_state_without_remote_probe(self, tmp_path, monkeypatch):
        def unexpected_probe(_self):
            raise AssertionError("local status must not touch remote storage")

        monkeypatch.setattr(
            "romcloud.services.saves.SaveSyncService.validate_remote_storage",
            unexpected_probe,
        )
        monkeypatch.setattr(
            "romcloud.services.saves.SaveSyncService.is_remote_reachable",
            unexpected_probe,
        )
        result = _invoke(_config_path(tmp_path), ["savesync-status"])

        assert result.exit_code == 0, result.output
        payload = json.loads(result.output.strip())
        assert payload["ok"] is True
        assert payload["remote_configured"] is True
        assert payload["auto_sync_enabled"] is False
        assert "remote_reachable" not in payload
        assert payload["xbox_enabled"] is False
        assert payload["xbox_hdd_size_bytes"] is None
        assert payload["rpcs3_installed_games_enabled"] is False
        assert payload["include_local_games"] is True
        assert payload["sync_status"] == "clean"
        assert payload["active_conflicts"] == 0
        assert payload["last_upload"] is None
        assert payload["last_download"] is None

    def test_reports_savesync_unavailable_without_remote_data(self, tmp_path):
        config = _build_config(tmp_path)
        config = AppConfig(
            source=config.source,
            cache=config.cache,
            local_roms_path=config.local_roms_path,
            data_path=config.data_path,
            saves=config.saves,
        )
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        cfg_path = config_dir / "romcloud.toml"
        write_config(config, str(cfg_path))

        result = _invoke(cfg_path, ["savesync-status"])
        payload = json.loads(result.output.strip())

        assert payload["remote_configured"] is False


class TestSavesyncAvailability:
    def test_reports_verified_writable_remote_data(self, tmp_path):
        cfg_path = _config_path(tmp_path)
        result = _invoke(cfg_path, ["savesync-availability"])

        assert result.exit_code == 0, result.output
        payload = json.loads(result.output.strip())
        assert payload["remote_configured"] is True
        assert payload["remote_available"] is True
        assert payload["remote_reachable"] is True
        assert payload["access"]["write_verified"] is True
        assert payload["access"]["cleanup_verified"] is True
        assert payload["sync_status"] == "clean"
        status = json.loads(_invoke(cfg_path, ["savesync-status"]).output)
        assert status["sync_status"] == "clean"

    def test_reports_unavailable_when_remote_data_path_is_missing(self, tmp_path):
        cfg_path = _config_path(tmp_path)
        (tmp_path / "remote-data").rmdir()

        result = _invoke(cfg_path, ["savesync-availability"])

        assert result.exit_code == 0, result.output
        payload = json.loads(result.output.strip())
        assert payload["remote_configured"] is True
        assert payload["remote_available"] is False
        assert payload["access"]["connected"] is False
        assert payload["sync_status"] == "remote-unavailable"
        status = json.loads(_invoke(cfg_path, ["savesync-status"]).output)
        assert status["sync_status"] == "remote-unavailable"

    def test_unconfigured_returns_without_provider_access(self, tmp_path):
        config = _build_config(tmp_path)
        config = AppConfig(
            source=config.source,
            cache=config.cache,
            local_roms_path=config.local_roms_path,
            data_path=config.data_path,
            saves=config.saves,
        )
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        cfg_path = config_dir / "romcloud.toml"
        write_config(config, str(cfg_path))

        result = _invoke(cfg_path, ["savesync-availability"])

        assert result.exit_code == 0, result.output
        payload = json.loads(result.output.strip())
        assert payload["remote_configured"] is False
        assert payload["remote_available"] is False
        assert "not configured" in payload["detail"]


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
    def test_auto_sync_toggle_persists_without_mutating_savesync_state(self, tmp_path):
        cfg_path = _config_path(tmp_path)
        _invoke(cfg_path, ["savesync-status"])
        state_path = tmp_path / "data" / "savesync-state.json"
        state_before = state_path.read_bytes()

        enabled = _invoke(
            cfg_path,
            ["savesync-settings"],
            input=json.dumps({"auto_sync_enabled": True}),
        )
        assert enabled.exit_code == 0, enabled.output
        assert json.loads(enabled.output)["auto_sync_enabled"] is True
        assert json.loads(_invoke(cfg_path, ["savesync-status"]).output)[
            "auto_sync_enabled"
        ] is True
        assert state_path.read_bytes() == state_before

        disabled = _invoke(
            cfg_path,
            ["savesync-settings"],
            input=json.dumps({"auto_sync_enabled": False}),
        )
        assert disabled.exit_code == 0, disabled.output
        assert json.loads(disabled.output)["auto_sync_enabled"] is False
        assert state_path.read_bytes() == state_before

    def test_enable_xbox_persists_to_config(self, tmp_path):
        cfg_path = _config_path(tmp_path)

        result = _invoke(cfg_path, ["savesync-settings"], input=json.dumps({"xbox_enabled": True}))

        assert result.exit_code == 0, result.output
        payload = json.loads(result.output.strip())
        assert payload["ok"] is True
        assert payload["xbox_enabled"] is True

        status = json.loads(_invoke(cfg_path, ["savesync-status"]).output.strip())
        assert status["xbox_enabled"] is True

    def test_legacy_rpcs3_setting_is_ignored_by_savesync_status(self, tmp_path):
        cfg_path = _config_path(tmp_path)

        result = _invoke(
            cfg_path,
            ["savesync-settings"],
            input=json.dumps({"rpcs3_installed_games_enabled": True}),
        )

        assert result.exit_code == 0, result.output
        payload = json.loads(result.output.strip())
        assert payload["rpcs3_installed_games_enabled"] is True
        status = json.loads(_invoke(cfg_path, ["savesync-status"]).output.strip())
        assert status["rpcs3_installed_games_enabled"] is False

    def test_enable_local_games_persists_to_config(self, tmp_path):
        cfg_path = _config_path(tmp_path)

        result = _invoke(
            cfg_path,
            ["savesync-settings"],
            input=json.dumps({"include_local_games": True}),
        )

        assert result.exit_code == 0, result.output
        payload = json.loads(result.output.strip())
        assert payload["include_local_games"] is True
        status = json.loads(_invoke(cfg_path, ["savesync-status"]).output.strip())
        assert status["include_local_games"] is True


class TestSavesyncReconcile:
    def test_reconcile_endpoint_reports_preflight_and_result(self, tmp_path):
        cfg_path = _config_path(tmp_path)
        (tmp_path / "saves/psx").mkdir(parents=True)
        (tmp_path / "saves/psx/Game.srm").write_bytes(b"save")
        enabled = _invoke(
            cfg_path,
            ["savesync-settings"],
            input=json.dumps({"include_local_games": True}),
        )
        assert enabled.exit_code == 0, enabled.output

        result = _invoke(
            cfg_path,
            ["savesync-reconcile"],
            input=json.dumps({}),
        )

        assert result.exit_code == 0, result.output
        payload = json.loads(result.output.strip())
        assert payload["preflight"]["uploads"] == 1
        assert payload["report"]["uploaded"] == 1
