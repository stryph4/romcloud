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
    ShareValidationResult,
)
from romcloud.infrastructure.config import (
    AppConfig,
    CacheConfig,
    LoggingConfig,
    RemoteDataConfig,
    SavesConfig,
    SMBConfig,
    SourceConfig,
    write_config,
)
from romcloud.infrastructure.credentials import (
    load_remote_data_smb_password,
    load_smb_password,
    write_smb_password,
)
from romcloud.infrastructure.providers.local import StorageAccessResult
from romcloud.core.progress import emit_progress


def _payload(**overrides):
    payload = {
        "server": "nas.local",
        "share": "ROMs",
        "username": "player",
        "password": "secret-value",
        "rom_root": "/userdata/romcloud/source",
        "cache_root": "/userdata/romcloud/cache",
        "max_size_gb": 50,
        "min_free_gb": 5,
    }
    payload.update(overrides)
    return payload


def _config(config_path: Path) -> AppConfig:
    home = config_path.parent.parent
    return AppConfig(
        source=SourceConfig("local", "/userdata/romcloud/source"),
        cache=CacheConfig("/userdata/romcloud/cache", 50, 5),
        local_roms_path="/userdata/roms",
        data_path=str(home / "data"),
        logging=LoggingConfig(),
        smb=SMBConfig("nas.local", "ROMs", "player"),
    )


class _Discovery:
    def __init__(
        self,
        *,
        reachable=True,
        auth=True,
        shares=(ShareInfo("ROMs"),),
        validation=None,
    ):
        self.reachable = reachable
        self.auth = auth
        self.shares = shares
        self.validation = validation or ShareValidationResult(
            True, "ROMs", top_level_entries=("psx",)
        )

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

    def validate_share(self, target, credentials, share):
        return self.validation

    def browse_directory(self, target, credentials, share, path=""):
        return self.validation

    def detect_systems(self, validation):
        return SimpleNamespace(detected_systems=("psx",), count=1)


class _Container:
    def __init__(
        self,
        config,
        *,
        errors=(),
        source_reachable=True,
        remote_reachable=True,
        remote_probe=None,
    ):
        self.config = config
        self.provider = SimpleNamespace(
            validate_access=lambda root: StorageAccessResult(
                source_reachable,
                source_reachable,
                detail="" if source_reachable else "read access denied",
            )
        )
        def refresh(progress=None):
            emit_progress(
                progress,
                "catalog_refresh",
                "system_progress",
                "running",
                "Scanning games",
                current=1,
                total=2,
            )
            emit_progress(
                progress,
                "catalog_refresh",
                "refresh_completed",
                "success" if not errors else "error",
                "Library scan complete",
                current=2,
                total=2,
            )
            return SimpleNamespace(errors=errors)

        self.catalog = SimpleNamespace(refresh=refresh)
        self.game_repo = SimpleNamespace(list_systems=lambda: ["psx", "snes"])
        self.saves = SimpleNamespace(
            is_remote_reachable=lambda: remote_reachable,
            validate_remote_storage=lambda: remote_probe
            or StorageAccessResult(
                    True,
                    True,
                    write_verified=remote_reachable,
                    cleanup_verified=remote_reachable,
                    detail="" if remote_reachable else "not writable",
                ),
        )


def _patch_apply_dependencies(
    monkeypatch,
    *,
    refresh_errors=(),
    mount_error=None,
    source_reachable=True,
    remote_reachable=True,
    remote_probe=None,
):
    monkeypatch.setattr(
        graphical_setup,
        "validate_share",
        lambda payload, progress=None: {
            "systems": ["psx", "snes"],
            "count": 2,
        },
    )
    monkeypatch.setattr(graphical_setup.mount_service, "install_service", lambda *args, **kwargs: None)
    if mount_error is None:
        monkeypatch.setattr(
            graphical_setup.mount_worker.mountlib, "mount_cifs_source", lambda **kwargs: None
        )
    else:
        def fail_mount(**kwargs):
            raise RuntimeError(mount_error)
        monkeypatch.setattr(graphical_setup.mount_worker.mountlib, "mount_cifs_source", fail_mount)
    monkeypatch.setattr(
        graphical_setup,
        "Container",
        lambda config: _Container(
            config,
            errors=refresh_errors,
            source_reachable=source_reachable,
            remote_reachable=remote_reachable,
            remote_probe=remote_probe,
        ),
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

    @pytest.mark.parametrize("mode", ["connected", "cache", "offline"])
    def test_configured_install_stays_configured_when_source_is_unreachable(
        self, tmp_path, mode
    ):
        """Setup completeness must be independent from runtime source
        availability. Real-hardware regression: a Steam Deck resuming with
        the NAS still unavailable must reopen the normal dashboard, in
        every persisted operating mode (including Offline), never the
        first-run wizard."""
        from romcloud.core.capabilities import OperatingMode
        from romcloud.infrastructure.library_view import write_operating_mode

        config_path = tmp_path / "config" / "romcloud.toml"
        config = _config(config_path)
        write_config(config, str(config_path))
        write_smb_password(config.credentials_path, "secret")
        write_operating_mode(config, OperatingMode(mode))

        assert not Path(config.source.rom_root).exists()
        result = graphical_setup.setup_state(config_path)

        assert result["state"] == "configured"


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

    def test_source_share_validation_reports_connected_and_read_verified(
        self, monkeypatch
    ):
        monkeypatch.setattr(
            graphical_setup,
            "build_default_smb_discovery_service",
            lambda: _Discovery(),
        )

        result = graphical_setup.validate_share(_payload())

        assert result["validation"] == {
            "connected": True,
            "read_verified": True,
        }

    def test_bad_credentials_fail_cleanly_without_password(self, monkeypatch):
        password = "top-secret-password"
        validation = ShareValidationResult(
            False,
            "ROMs",
            error_kind=SMBErrorKind.AUTH_FAILED,
            detail=f"credential {password} rejected",
        )
        monkeypatch.setattr(
            graphical_setup,
            "build_default_smb_discovery_service",
            lambda: _Discovery(validation=validation),
        )

        with pytest.raises(ValueError) as exc:
            graphical_setup.validate_share(_payload(password=password))

        assert password not in str(exc.value)
        assert "password" in str(exc.value).lower()

    def test_remote_directory_browsing_preserves_relative_path_and_file_types(
        self, monkeypatch
    ):
        from romcloud.services.smb_discovery import SMBDirectoryEntry

        discovery = _Discovery(
            validation=ShareValidationResult(
                True,
                "ROMs",
                entries=(
                    SMBDirectoryEntry("ps2", True),
                    SMBDirectoryEntry("README.txt", False),
                ),
            )
        )
        monkeypatch.setattr(
            graphical_setup,
            "build_default_smb_discovery_service",
            lambda: discovery,
        )

        result = graphical_setup.browse_smb_directory(
            _payload(source_remote_path="Roms")
        )

        assert result["path"] == "Roms"
        assert result["parent"] == ""
        assert result["entries"] == [
            {"name": "ps2", "is_directory": True},
            {"name": "README.txt", "is_directory": False},
        ]

    def test_validation_progress_is_structured_and_password_free(self, monkeypatch):
        password = "never-show-this"
        validation = ShareValidationResult(
            False,
            "ROMs",
            error_kind=SMBErrorKind.ACCESS_DENIED,
            detail=f"denied for {password}",
        )
        monkeypatch.setattr(
            graphical_setup,
            "build_default_smb_discovery_service",
            lambda: _Discovery(validation=validation),
        )
        events = []

        with pytest.raises(ValueError):
            graphical_setup.validate_share(
                _payload(password=password), progress=events.append
            )

        assert [event.stage for event in events] == ["directory", "directory"]
        assert events[-1].status == "error"
        assert password not in events[-1].detail
        assert "***" in events[-1].detail


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
            {"cache_root": "/userdata/romcloud/source/cache"},
        ],
    )
    def test_invalid_or_unsafe_values_are_rejected(self, changes):
        with pytest.raises(ValueError):
            SetupRequest.from_payload(_payload(**changes))

    def test_defaults_use_consolidated_runtime_layout(self):
        request = SetupRequest.from_payload(
            {
                "server": "nas.local",
                "share": "ROMs",
                "username": "player",
                "password": "secret",
            }
        )

        assert request.rom_root == "/userdata/romcloud/source"
        assert request.cache_root == "/userdata/romcloud/cache"
        assert request.remote_data_type == "none"

    def test_remote_data_must_not_overlap_source_or_cache(self):
        with pytest.raises(ValueError, match="cannot overlap"):
            SetupRequest.from_payload(
                _payload(
                    remote_data_type="local",
                    remote_data_root="/userdata/romcloud/source/data",
                )
            )

    def test_library_sync_is_opt_in_and_requires_writable_remote_data(self):
        assert SetupRequest.from_payload(_payload()).library_sync_enabled is False
        with pytest.raises(ValueError, match="Library Sync requires"):
            SetupRequest.from_payload(_payload(library_sync_enabled=True))

        request = SetupRequest.from_payload(
            _payload(
                remote_data_type="local",
                remote_data_root="/mnt/romcloud-data",
                library_sync_enabled=True,
            )
        )
        assert request.library_sync_enabled is True

    def test_remote_data_smb_must_not_reuse_rom_share(self):
        with pytest.raises(ValueError, match="separate writable share"):
            SetupRequest.from_payload(
                _payload(
                    remote_data_type="smb",
                    remote_server="NAS.local",
                    remote_share="roms",
                    remote_username="writer",
                    remote_password="write-secret",
                )
            )

    def test_reusing_source_credentials_resolves_independent_remote_target(self):
        request = SetupRequest.from_payload(
            _payload(
                remote_data_type="smb",
                remote_share="ROMCloud",
                remote_reuse_source_credentials=True,
            )
        )

        assert request.remote_server == request.server == "nas.local"
        assert request.remote_username == request.username == "player"
        assert request.remote_password == request.password == "secret-value"
        assert request.remote_share == "ROMCloud"

    def test_explicit_remote_credentials_remain_independent(self):
        request = SetupRequest.from_payload(
            _payload(
                remote_data_type="smb",
                remote_server="data-nas",
                remote_share="ROMCloud",
                remote_username="writer",
                remote_password="write-secret",
            )
        )

        assert request.remote_server == "data-nas"
        assert request.remote_username == "writer"
        assert request.remote_password == "write-secret"


class TestApply:
    def test_reconfigure_preserves_savesync_selection_settings(self, tmp_path):
        config_path = tmp_path / "config" / "romcloud.toml"
        existing = _config(config_path)
        existing = AppConfig(
            **{
                **existing.__dict__,
                "saves": SavesConfig(local_path="/custom/saves", xbox_enabled=True),
            }
        )

        updated = graphical_setup._build_config(
            config_path,
            SetupRequest.from_payload(_payload()),
            existing,
        )

        assert updated.saves == existing.saves

    def test_success_marks_setup_configured(self, tmp_path, monkeypatch):
        config_path = tmp_path / "config" / "romcloud.toml"
        _patch_apply_dependencies(monkeypatch)
        result = graphical_setup.apply_setup(config_path, _payload())
        assert result["system_count"] == 2
        assert result["source_validation"]["read_verified"] is True
        assert graphical_setup.setup_state(config_path)["state"] == "configured"
        assert not (config_path.parent / graphical_setup.SETUP_STATE_FILENAME).exists()

    def test_setup_streams_phase_and_catalog_progress_to_the_gui(
        self, tmp_path, monkeypatch
    ):
        config_path = tmp_path / "config" / "romcloud.toml"
        _patch_apply_dependencies(monkeypatch)
        events = []

        graphical_setup.apply_setup(config_path, _payload(), progress=events.append)

        assert any(
            event.stage == "system_progress"
            and event.current == 1
            and event.total == 2
            for event in events
        )
        phases = [(event.stage, event.status) for event in events]
        assert ("mount", "running") in phases
        assert ("emulationstation", "running") in phases
        assert ("emulationstation", "success") in phases
        assert phases[-1] == ("complete", "success")

    def test_enabled_library_sync_does_not_import_optional_enrichment(
        self, tmp_path, monkeypatch
    ):
        config_path = tmp_path / "config" / "romcloud.toml"
        remote_root = tmp_path / "remote-data"
        _patch_apply_dependencies(monkeypatch)
        calls = []

        def reconcile(config, **kwargs):
            calls.append(kwargs)

        monkeypatch.setattr(
            "romcloud.integrations.batocera.game_access.reconcile_game_access",
            reconcile,
        )
        events = []
        graphical_setup.apply_setup(
            config_path,
            _payload(
                remote_data_type="local",
                remote_data_root=str(remote_root),
                library_sync_enabled=True,
            ),
            progress=events.append,
        )

        assert calls == [{"render_library_metadata": False}]
        assert all(event.stage != "library_sync" for event in events)

    def test_local_source_from_graphical_setup_persists_without_smb(
        self, tmp_path, monkeypatch
    ):
        config_path = tmp_path / "config" / "romcloud.toml"
        rom_root = tmp_path / "roms"
        (rom_root / "psx").mkdir(parents=True)
        _patch_apply_dependencies(monkeypatch)
        monkeypatch.setattr(
            graphical_setup,
            "validate_local_source",
            lambda payload, progress=None: {
                "systems": ["psx"],
                "count": 1,
                "validation": {"connected": True, "read_verified": True},
            },
        )

        graphical_setup.apply_setup(
            config_path,
            _payload(
                source_type="local",
                server="",
                share="",
                username="",
                password="",
                rom_root=str(rom_root),
            ),
        )
        config = graphical_setup.load_config(str(config_path))

        assert config.source.rom_root == str(rom_root)
        assert config.smb is None

    def test_source_read_validation_failure_is_clean_and_redacted(
        self, tmp_path, monkeypatch
    ):
        config_path = tmp_path / "config" / "romcloud.toml"
        _patch_apply_dependencies(monkeypatch, source_reachable=False)

        with pytest.raises(RuntimeError, match="ROM library access validation failed") as exc:
            graphical_setup.apply_setup(config_path, _payload())

        assert "secret-value" not in str(exc.value)

    def test_mount_failure_is_partial_and_password_is_not_persisted_in_state(self, tmp_path, monkeypatch):
        config_path = tmp_path / "config" / "romcloud.toml"
        _patch_apply_dependencies(monkeypatch, mount_error="secret-value rejected")
        with pytest.raises(RuntimeError, match="mount and test"):
            graphical_setup.apply_setup(config_path, _payload())
        state_path = config_path.parent / graphical_setup.SETUP_STATE_FILENAME
        state_text = state_path.read_text()
        assert "secret-value" not in state_text
        assert json.loads(state_text)["failed_step"] == "mount and test storage"
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

    def test_independent_remote_smb_target_is_mounted_read_write(
        self, tmp_path, monkeypatch
    ):
        config_path = tmp_path / "config" / "romcloud.toml"
        _patch_apply_dependencies(monkeypatch)
        calls = []
        monkeypatch.setattr(
            graphical_setup.mount_worker.mountlib,
            "mount_cifs_source",
            lambda **kwargs: calls.append(kwargs),
        )
        payload = _payload(
            remote_data_type="smb",
            remote_server="backup-nas.local",
            remote_share="ROMCloud",
            remote_username="sync-user",
            remote_password="sync-secret",
        )

        graphical_setup.apply_setup(config_path, payload)
        config = graphical_setup.load_config(str(config_path))

        assert [(call["server"], call["share"]) for call in calls] == [
            ("nas.local", "ROMs"),
            ("backup-nas.local", "ROMCloud"),
        ]
        assert calls[0]["read_only"] is True
        assert calls[1]["read_only"] is False
        assert config.remote_data == RemoteDataConfig(
            provider="smb",
            root="/userdata/romcloud/remote",
            smb=SMBConfig("backup-nas.local", "ROMCloud", "sync-user"),
        )
        assert load_smb_password(config.credentials_path) == "secret-value"
        assert load_remote_data_smb_password(config.credentials_path) == "sync-secret"

    def test_reused_remote_credentials_are_persisted_independently(
        self, tmp_path, monkeypatch
    ):
        config_path = tmp_path / "config" / "romcloud.toml"
        _patch_apply_dependencies(monkeypatch)
        payload = _payload(
            remote_data_type="smb",
            remote_share="ROMCloud",
            remote_reuse_source_credentials=True,
        )

        graphical_setup.apply_setup(config_path, payload)
        config = graphical_setup.load_config(str(config_path))

        assert config.smb.share == "ROMs"
        assert config.remote_data.smb.share == "ROMCloud"
        assert config.remote_data.smb is not config.smb
        assert load_smb_password(config.credentials_path) == "secret-value"
        assert load_remote_data_smb_password(config.credentials_path) == "secret-value"

    def test_selected_remote_directories_persist_for_both_mounts(
        self, tmp_path, monkeypatch
    ):
        config_path = tmp_path / "config" / "romcloud.toml"
        _patch_apply_dependencies(monkeypatch)
        payload = _payload(
            source_remote_path="Roms/Console",
            remote_data_type="smb",
            remote_server="backup-nas.local",
            remote_share="ROMCloud",
            remote_remote_path="Data/ROMCloud",
            remote_username="sync-user",
            remote_password="sync-secret",
        )

        graphical_setup.apply_setup(config_path, payload)
        config = graphical_setup.load_config(str(config_path))

        assert config.smb.remote_path == "Roms/Console"
        assert config.remote_data.smb.remote_path == "Data/ROMCloud"

    def test_local_remote_data_root_is_persisted_only_after_writable_validation(
        self, tmp_path, monkeypatch
    ):
        config_path = tmp_path / "config" / "romcloud.toml"
        remote_root = tmp_path / "remote-data"
        _patch_apply_dependencies(monkeypatch)

        graphical_setup.apply_setup(
            config_path,
            _payload(remote_data_type="local", remote_data_root=str(remote_root)),
        )

        config = graphical_setup.load_config(str(config_path))
        assert config.remote_data == RemoteDataConfig("local", str(remote_root))
        assert remote_root.is_dir()

    def test_unwritable_remote_data_fails_without_exposing_password(
        self, tmp_path, monkeypatch
    ):
        config_path = tmp_path / "config" / "romcloud.toml"
        _patch_apply_dependencies(monkeypatch, remote_reachable=False)
        monkeypatch.setattr(
            graphical_setup.mount_worker.mountlib,
            "mount_cifs_source",
            lambda **kwargs: SimpleNamespace(already_mounted=False),
        )
        unmounted = []
        monkeypatch.setattr(
            "romcloud.infrastructure.mount.unmount_cifs_source",
            lambda path: unmounted.append(path) or True,
        )
        payload = _payload(
            remote_data_type="smb",
            remote_server="backup-nas.local",
            remote_share="ROMCloud",
            remote_username="sync-user",
            remote_password="remote-secret-value",
        )

        with pytest.raises(RuntimeError, match="not writable"):
            graphical_setup.apply_setup(config_path, payload)

        state_text = (config_path.parent / graphical_setup.SETUP_STATE_FILENAME).read_text()
        assert "remote-secret-value" not in state_text
        assert unmounted == [
            "/userdata/romcloud/remote",
            "/userdata/romcloud/source",
        ]

    def test_remote_probe_cleanup_failure_is_surfaced(self, tmp_path, monkeypatch):
        config_path = tmp_path / "config" / "romcloud.toml"
        _patch_apply_dependencies(
            monkeypatch,
            remote_probe=StorageAccessResult(
                True,
                True,
                write_verified=True,
                cleanup_verified=False,
                detail="cleanup failed for ROMCloud probe: delete denied",
            ),
        )
        payload = _payload(
            remote_data_type="smb",
            remote_server="backup-nas.local",
            remote_share="ROMCloud",
            remote_username="sync-user",
            remote_password="remote-secret-value",
        )

        with pytest.raises(RuntimeError, match="cleanup failed") as exc:
            graphical_setup.apply_setup(config_path, payload)

        assert "remote-secret-value" not in str(exc.value)

    def test_failed_setup_reports_incomplete_mount_cleanup(
        self, tmp_path, monkeypatch
    ):
        config_path = tmp_path / "config" / "romcloud.toml"
        _patch_apply_dependencies(monkeypatch, remote_reachable=False)
        monkeypatch.setattr(
            graphical_setup.mount_worker.mountlib,
            "mount_cifs_source",
            lambda **kwargs: SimpleNamespace(already_mounted=False),
        )

        def cleanup(path):
            if path == "/userdata/romcloud/remote":
                raise RuntimeError("target busy")
            return True

        monkeypatch.setattr(
            "romcloud.infrastructure.mount.unmount_cifs_source", cleanup
        )
        payload = _payload(
            remote_data_type="smb",
            remote_server="backup-nas.local",
            remote_share="ROMCloud",
            remote_username="sync-user",
            remote_password="remote-secret-value",
        )

        with pytest.raises(RuntimeError, match="mount cleanup failed"):
            graphical_setup.apply_setup(config_path, payload)

        state_text = (config_path.parent / graphical_setup.SETUP_STATE_FILENAME).read_text()
        assert "target busy" in state_text
        assert "remote-secret-value" not in state_text
