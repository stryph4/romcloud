from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from romcloud.lifecycle import setup as graphical_setup
from romcloud.lifecycle.setup import SetupRequest
from romcloud.services.smb_discovery import (
    AuthResult,
    ListSharesResult,
    SMBErrorKind,
    ShareInfo,
)
from romcloud.infrastructure.config import (
    AppConfig,
    CacheConfig,
    LoggingConfig,
    SMBConfig,
    SourceConfig,
    write_config,
)
from romcloud.infrastructure.credentials import write_smb_password


def _payload(**overrides):
    payload = {
        "server": "nas.local",
        "share": "ROMs",
        "username": "player",
        "password": "secret-value",
        "rom_root": "/userdata/romcloud-source",
        "cache_root": "/userdata/romcloud-cache",
        "max_size_gb": 50,
        "min_free_gb": 5,
    }
    payload.update(overrides)
    return payload


def _config(config_path: Path) -> AppConfig:
    home = config_path.parent.parent
    return AppConfig(
        source=SourceConfig("local", "/userdata/romcloud-source"),
        cache=CacheConfig("/userdata/romcloud-cache", 50, 5),
        local_roms_path="/userdata/roms",
        data_path=str(home / "data"),
        logging=LoggingConfig(),
        smb=SMBConfig("nas.local", "ROMs", "player"),
    )


class _Discovery:
    def __init__(self, *, reachable=True, auth=True, shares=(ShareInfo("ROMs"),)):
        self.reachable = reachable
        self.auth = auth
        self.shares = shares

    def validate_server(self, target):
        return SimpleNamespace(ok=self.reachable, detail="host unreachable")

    def authenticate(self, target, credentials):
        return AuthResult(
            ok=self.auth,
            error_kind=None if self.auth else SMBErrorKind.AUTH_FAILED,
            detail="" if self.auth else "authentication failed",
        )

    def list_shares(self, target, credentials):
        if not self.shares:
            return ListSharesResult(False, error_kind=SMBErrorKind.NO_SHARES_FOUND, detail="no shares")
        return ListSharesResult(True, shares=tuple(self.shares))


class _Container:
    def __init__(self, config, *, errors=()):
        self.config = config
        self.catalog = SimpleNamespace(refresh=lambda: SimpleNamespace(errors=errors))
        self.game_repo = SimpleNamespace(list_systems=lambda: ["psx", "snes"])


def _patch_apply_dependencies(monkeypatch, *, refresh_errors=(), mount_error=None):
    monkeypatch.setattr(
        graphical_setup,
        "validate_share",
        lambda payload: {"systems": ["psx", "snes"], "count": 2},
    )
    monkeypatch.setattr(graphical_setup.mount_service, "install_service", lambda *args, **kwargs: None)
    if mount_error is None:
        monkeypatch.setattr(graphical_setup, "mount_cifs_source", lambda *args, **kwargs: None)
    else:
        def fail_mount(*args, **kwargs):
            raise RuntimeError(mount_error)
        monkeypatch.setattr(graphical_setup, "mount_cifs_source", fail_mount)
    monkeypatch.setattr(
        graphical_setup,
        "Container",
        lambda config: _Container(config, errors=refresh_errors),
    )
    monkeypatch.setattr(graphical_setup.es_config, "install", lambda systems: None)


class TestSetupState:
    def test_fresh_install(self, tmp_path):
        result = graphical_setup.setup_state(tmp_path / "config" / "romcloud.toml")
        assert result["state"] == "fresh"

    def test_failed_initial_write_without_config_is_partial(self, tmp_path):
        config_path = tmp_path / "config" / "romcloud.toml"
        config_path.parent.mkdir()
        state_path = config_path.parent / graphical_setup.SETUP_STATE_FILENAME
        state_path.write_text('{"status":"failed","failed_step":"write configuration"}')
        result = graphical_setup.setup_state(config_path)
        assert result["state"] == "partial"
        assert result["failed_step"] == "write configuration"

    def test_configured_install(self, tmp_path):
        config_path = tmp_path / "config" / "romcloud.toml"
        config = _config(config_path)
        write_config(config, str(config_path))
        write_smb_password(config.credentials_path, "secret")
        assert graphical_setup.setup_state(config_path)["state"] == "configured"

    def test_missing_credentials_is_partial(self, tmp_path):
        config_path = tmp_path / "config" / "romcloud.toml"
        write_config(_config(config_path), str(config_path))
        result = graphical_setup.setup_state(config_path)
        assert result["state"] == "partial"
        assert "credentials" in " ".join(result["issues"]).lower()

    def test_interrupted_apply_is_partial(self, tmp_path):
        config_path = tmp_path / "config" / "romcloud.toml"
        config = _config(config_path)
        write_config(config, str(config_path))
        write_smb_password(config.credentials_path, "secret")
        state_path = config_path.parent / graphical_setup.SETUP_STATE_FILENAME
        state_path.write_text('{"status":"applying","step":"refresh catalog"}')
        assert graphical_setup.setup_state(config_path)["state"] == "partial"


class TestDiscovery:
    def test_successful_discovery(self, monkeypatch):
        monkeypatch.setattr(graphical_setup, "build_default_smb_discovery_service", lambda: _Discovery())
        result = graphical_setup.discover_shares(_payload(share=""))
        assert result["shares"] == [{"name": "ROMs", "comment": ""}]

    @pytest.mark.parametrize(
        ("discovery", "message"),
        [
            (_Discovery(reachable=False), "unreachable"),
            (_Discovery(auth=False), "authentication"),
            (_Discovery(shares=()), "no shares"),
        ],
    )
    def test_discovery_failures_are_clear(self, monkeypatch, discovery, message):
        monkeypatch.setattr(graphical_setup, "build_default_smb_discovery_service", lambda: discovery)
        with pytest.raises(ValueError, match=message):
            graphical_setup.discover_shares(_payload(share=""))


class TestCacheValidation:
    def test_defaults_are_valid(self):
        request = SetupRequest.from_payload(_payload())
        assert request.max_size_gb == 50

    @pytest.mark.parametrize(
        "changes",
        [
            {"max_size_gb": 0},
            {"min_free_gb": -1},
            {"cache_root": "relative/cache"},
            {"cache_root": "/userdata/romcloud-source/cache"},
        ],
    )
    def test_invalid_or_unsafe_values_are_rejected(self, changes):
        with pytest.raises(ValueError):
            SetupRequest.from_payload(_payload(**changes))


class TestApply:
    def test_success_marks_setup_configured(self, tmp_path, monkeypatch):
        config_path = tmp_path / "config" / "romcloud.toml"
        _patch_apply_dependencies(monkeypatch)
        result = graphical_setup.apply_setup(config_path, _payload())
        assert result["system_count"] == 2
        assert graphical_setup.setup_state(config_path)["state"] == "configured"
        assert not (config_path.parent / graphical_setup.SETUP_STATE_FILENAME).exists()

    def test_mount_failure_is_partial_and_password_is_not_persisted_in_state(self, tmp_path, monkeypatch):
        config_path = tmp_path / "config" / "romcloud.toml"
        _patch_apply_dependencies(monkeypatch, mount_error="secret-value rejected")
        with pytest.raises(RuntimeError, match="mount and test"):
            graphical_setup.apply_setup(config_path, _payload())
        state_path = config_path.parent / graphical_setup.SETUP_STATE_FILENAME
        state_text = state_path.read_text()
        assert "secret-value" not in state_text
        assert json.loads(state_text)["failed_step"] == "mount and test source"
        assert graphical_setup.setup_state(config_path)["state"] == "partial"

    def test_refresh_failure_can_be_retried(self, tmp_path, monkeypatch):
        config_path = tmp_path / "config" / "romcloud.toml"
        _patch_apply_dependencies(monkeypatch, refresh_errors=(("psx", "scan failed"),))
        with pytest.raises(RuntimeError, match="refresh catalog"):
            graphical_setup.apply_setup(config_path, _payload())
        assert graphical_setup.setup_state(config_path)["state"] == "partial"

        _patch_apply_dependencies(monkeypatch)
        graphical_setup.apply_setup(config_path, _payload())
        assert graphical_setup.setup_state(config_path)["state"] == "configured"

    def test_failed_repair_restores_existing_working_config(self, tmp_path, monkeypatch):
        config_path = tmp_path / "config" / "romcloud.toml"
        config = _config(config_path)
        write_config(config, str(config_path))
        write_smb_password(config.credentials_path, "old-secret")
        old_config = config_path.read_bytes()
        old_credentials = config.credentials_path.read_bytes()
        _patch_apply_dependencies(monkeypatch, mount_error="new setup failed")

        with pytest.raises(RuntimeError):
            graphical_setup.apply_setup(config_path, _payload(server="new-nas"))

        assert config_path.read_bytes() == old_config
        assert config.credentials_path.read_bytes() == old_credentials
        assert graphical_setup.setup_state(config_path)["state"] == "configured"
