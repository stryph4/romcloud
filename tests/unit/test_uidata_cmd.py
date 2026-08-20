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


class TestManagerBridge:
    def test_manager_status_surfaces_runtime_details(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "romcloud.cli.commands.uidata.load_config",
            lambda path: type("Config", (), {"data_path": str(tmp_path / "data")})(),
        )
        monkeypatch.setattr(
            "romcloud.web.lifecycle.manager_status",
            lambda data_path: {
                "running": True,
                "url": "https://batocera.local:8765/",
                "token": "token",
            },
        )
        result = CliRunner().invoke(
            cli,
            ["--config", str(tmp_path / "config.toml"), "uidata", "manager-status"],
        )
        payload = json.loads(result.output)
        assert result.exit_code == 0
        assert payload["running"] is True
        assert payload["token"] == "token"

    def test_manager_start_reuses_existing_manager_lifecycle(self, tmp_path, monkeypatch):
        captured = {}
        monkeypatch.setattr(
            "romcloud.cli.commands.uidata.load_config",
            lambda path: type("Config", (), {"data_path": str(tmp_path / "data")})(),
        )

        def start(binary, data_path):
            captured.update(binary=str(binary), data_path=str(data_path))
            return {
                "running": True,
                "url": "https://batocera.local:8765/",
                "token": "token",
                "started": True,
            }

        monkeypatch.setattr("romcloud.web.lifecycle.start_manager", start)
        monkeypatch.setenv("ROMCLOUD_BIN", "/opt/romcloud/bin/romcloud")
        result = CliRunner().invoke(
            cli,
            ["--config", str(tmp_path / "config.toml"), "uidata", "manager-start"],
        )
        payload = json.loads(result.output)
        assert result.exit_code == 0
        assert payload["running"] is True
        assert captured["binary"] == "/opt/romcloud/bin/romcloud"

    def test_manager_boot_start_records_attempt_then_healthy_activation(
        self, tmp_path, monkeypatch
    ):
        events = []
        monkeypatch.setattr(
            "romcloud.cli.commands.uidata.load_config",
            lambda path: type("Config", (), {"data_path": str(tmp_path / "data")})(),
        )
        monkeypatch.setattr(
            "romcloud.web.lifecycle.start_manager",
            lambda *args: {"running": True, "started": True},
        )
        monkeypatch.setattr(
            "romcloud.cli.commands.uidata.startup_activation.record_startup_attempt",
            lambda path: events.append("attempt"),
        )
        monkeypatch.setattr(
            "romcloud.cli.commands.uidata.startup_activation.mark_activated",
            lambda path: events.append("activated") or True,
        )

        result = CliRunner().invoke(
            cli,
            ["--config", str(tmp_path / "config.toml"), "uidata", "manager-boot-start"],
        )

        assert result.exit_code == 0, result.output
        assert json.loads(result.output)["running"] is True
        assert events == ["attempt", "activated"]

    def test_manager_boot_start_failure_is_recorded_and_surfaced(
        self, tmp_path, monkeypatch
    ):
        failures = []
        monkeypatch.setattr(
            "romcloud.cli.commands.uidata.load_config",
            lambda path: type("Config", (), {"data_path": str(tmp_path / "data")})(),
        )
        monkeypatch.setattr(
            "romcloud.web.lifecycle.start_manager",
            lambda *args: (_ for _ in ()).throw(RuntimeError("bind failed")),
        )
        monkeypatch.setattr(
            "romcloud.cli.commands.uidata.startup_activation.record_startup_attempt",
            lambda path: None,
        )
        monkeypatch.setattr(
            "romcloud.cli.commands.uidata.startup_activation.record_startup_failure",
            lambda path, detail: failures.append(detail),
        )

        result = CliRunner().invoke(
            cli,
            ["--config", str(tmp_path / "config.toml"), "uidata", "manager-boot-start"],
        )

        assert result.exit_code == 1
        assert json.loads(result.output)["error"] == "bind failed"
        assert failures == ["bind failed"]


class TestSetupBridge:
    def test_fresh_setup_status_works_without_config(self, tmp_path):
        cfg_path = tmp_path / "missing" / "romcloud.toml"
        result = CliRunner().invoke(cli, ["--config", str(cfg_path), "uidata", "setup-status"])
        assert result.exit_code == 0
        payload = json.loads(result.output)
        assert payload["ok"] is True
        assert payload["state"] == "fresh"

    def test_setup_status_surfaces_manager_boot_failure_distinctly(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setattr(
            "romcloud.cli.commands.uidata.startup_activation.activation_status",
            lambda path: {
                "startup_restart_required": False,
                "startup_manager_startup_failed": True,
                "startup_manager_failure_message": "Manager bind failed",
            },
        )

        result = CliRunner().invoke(
            cli,
            [
                "--config",
                str(tmp_path / "missing" / "romcloud.toml"),
                "uidata",
                "setup-status",
            ],
        )
        payload = json.loads(result.output)

        assert result.exit_code == 0
        assert payload["startup_restart_required"] is False
        assert payload["startup_manager_startup_failed"] is True
        assert payload["startup_manager_failure_message"] == "Manager bind failed"

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

    def test_sftp_source_with_smb_remote_data_dispatches_smb_validation(
        self, tmp_path, monkeypatch
    ):
        import romcloud.cli.commands.uidata as uidata_module

        calls = []
        monkeypatch.setattr(
            uidata_module,
            "validate_share",
            lambda payload, progress=None: calls.append(("smb", payload))
            or {"systems": [], "count": 0, "validation": {"connected": True}},
        )
        monkeypatch.setattr(
            uidata_module,
            "validate_sftp_source",
            lambda payload, progress=None: calls.append(("sftp", payload)) or {},
        )
        monkeypatch.setattr(
            uidata_module, "_require_capability_if_configured", lambda *args: None
        )
        request = {
            "purpose": "remote_data",
            "source_type": "sftp",
            "remote_data_type": "smb",
        }

        result = CliRunner().invoke(
            cli,
            ["--config", str(tmp_path / "missing.toml"), "uidata", "setup-validate"],
            input=json.dumps(request),
        )

        assert result.exit_code == 0, result.output
        assert calls == [("smb", request)]

    def test_smb_source_with_sftp_remote_data_dispatches_sftp_validation(
        self, tmp_path, monkeypatch
    ):
        import romcloud.cli.commands.uidata as uidata_module

        calls = []
        monkeypatch.setattr(
            uidata_module,
            "validate_share",
            lambda payload, progress=None: calls.append(("smb", payload)) or {},
        )
        monkeypatch.setattr(
            uidata_module,
            "validate_sftp_source",
            lambda payload, progress=None: calls.append(("sftp", payload))
            or {"systems": [], "count": 0, "validation": {"connected": True}},
        )
        request = {
            "purpose": "remote_data",
            "source_type": "smb",
            "remote_data_type": "sftp",
        }

        result = CliRunner().invoke(
            cli,
            ["--config", str(tmp_path / "missing.toml"), "uidata", "setup-validate"],
            input=json.dumps(request),
        )

        assert result.exit_code == 0, result.output
        assert calls == [("sftp", request)]

    def test_sftp_browse_action_forwards_independent_remote_request(
        self, tmp_path, monkeypatch
    ):
        import romcloud.cli.commands.uidata as uidata_module

        captured = []
        monkeypatch.setattr(
            uidata_module,
            "browse_sftp_directory",
            lambda payload, progress=None: captured.append(dict(payload))
            or {"path": "/Data", "parent": "/", "entries": []},
        )
        monkeypatch.setattr(
            uidata_module, "_require_capability_if_configured", lambda *args: None
        )
        request = {
            "purpose": "remote_data",
            "source_type": "sftp",
            "server": "roms.example",
            "remote_data_type": "sftp",
            "remote_server": "data.example",
            "sftp_browse_path": "/Data",
        }

        result = CliRunner().invoke(
            cli,
            [
                "--config",
                str(tmp_path / "missing.toml"),
                "uidata",
                "setup-browse-sftp",
            ],
            input=json.dumps(request),
        )

        assert result.exit_code == 0, result.output
        assert captured == [request]
        assert json.loads(result.output)["path"] == "/Data"


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
            "install",
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


class TestLibraryModeAction:
    """Mode lifecycle state is passed explicitly to the graphical client."""

    def test_genuine_transition_requests_terminal_es_handoff(self, tmp_path, monkeypatch):
        from romcloud.integrations.batocera.game_access import LibraryPresentationReport

        monkeypatch.setattr(
            "romcloud.integrations.batocera.game_access.set_operating_mode",
            lambda config, mode, progress=None: LibraryPresentationReport(
                offline=False, mode_changed=True, es_restarted=True
            ),
        )

        result = _write_and_invoke(tmp_path, ["library-cache"])

        assert result.exit_code == 0, result.output
        payload = json.loads(result.output.strip().splitlines()[-1])
        assert payload["ok"] is True
        assert payload["mode_changed"] is True
        assert payload["es_restart_requested"] is True

    def test_same_mode_reentry_does_not_request_terminal_handoff(self, tmp_path, monkeypatch):
        from romcloud.integrations.batocera.game_access import LibraryPresentationReport

        monkeypatch.setattr(
            "romcloud.integrations.batocera.game_access.set_operating_mode",
            lambda config, mode, progress=None: LibraryPresentationReport(
                offline=False, es_restarted=False
            ),
        )

        result = _write_and_invoke(tmp_path, ["library-cache"])

        assert result.exit_code == 0, result.output
        payload = json.loads(result.output.strip().splitlines()[-1])
        assert payload["ok"] is True
        assert payload["mode_changed"] is False
        assert payload["es_restart_requested"] is False


class TestLibrarySyncBridge:
    def test_preview_returns_lightweight_import_counts(self, monkeypatch):
        import romcloud.cli.commands.uidata as uidata_module

        preview = type(
            "Preview",
            (),
            {"as_dict": lambda self: {"games_eligible": 10, "video_references": 2}},
        )()
        container = type(
            "Container", (), {"library_sync": type("Service", (), {"preview_source_import": lambda self: preview})()}
        )()
        monkeypatch.setattr(uidata_module, "_load_context_config", lambda ctx: None)
        monkeypatch.setattr(uidata_module, "get_container", lambda ctx: container)

        result = CliRunner().invoke(
            uidata_module.uidata_group,
            ["library-sync-preview"],
            obj={"config_path": "unused"},
        )

        assert result.exit_code == 0
        assert json.loads(result.stdout)["games_eligible"] == 10

    def test_import_forwards_structured_progress(self, monkeypatch):
        import romcloud.cli.commands.uidata as uidata_module
        from romcloud.core.models.librarysync import LibrarySyncReport
        from romcloud.core.progress import emit_progress

        calls: list[bool] = []

        class Service:
            def sync(self, progress=None, *, full=False):
                calls.append(full)
                emit_progress(
                    progress,
                    "library_sync",
                    "media",
                    "running",
                    "ps2: media file 1 / 2",
                    current=1,
                    total=2,
                )
                return LibrarySyncReport(direction="sync", rendered=1)

        container = type(
            "Container",
            (),
            {
                "library_sync": Service(),
                "config": object(),
                "game_repo": type("Repo", (), {"list_systems": lambda self: ["ps2"]})(),
            },
        )()
        monkeypatch.setattr(uidata_module, "_load_context_config", lambda ctx: None)
        monkeypatch.setattr(uidata_module, "get_container", lambda ctx: container)
        monkeypatch.setattr(
            "romcloud.integrations.batocera.presentation.refresh_emulationstation",
            lambda config, systems: None,
        )

        result = CliRunner().invoke(
            uidata_module.uidata_group,
            ["library-sync"],
            obj={"config_path": "unused"},
        )

        assert result.exit_code == 0
        assert "@romcloud-progress" in result.output
        assert calls == [False]
        payload_line = next(
            line for line in reversed(result.output.splitlines()) if line.startswith("{")
        )
        assert json.loads(payload_line)["rendered"] == 1

    def test_full_import_bridge_calls_existing_full_mode(self, monkeypatch):
        import romcloud.cli.commands.uidata as uidata_module
        from romcloud.core.models.librarysync import LibrarySyncReport

        calls: list[bool] = []

        class Service:
            def sync(self, progress=None, *, full=False):
                calls.append(full)
                return LibrarySyncReport(
                    direction="sync",
                    reconciliation="full" if full else "quick",
                )

        container = type(
            "Container",
            (),
            {
                "library_sync": Service(),
                "config": object(),
                "game_repo": type(
                    "Repo", (), {"list_systems": lambda self: ["ps2"]}
                )(),
            },
        )()
        monkeypatch.setattr(uidata_module, "_load_context_config", lambda ctx: None)
        monkeypatch.setattr(uidata_module, "get_container", lambda ctx: container)
        monkeypatch.setattr(
            "romcloud.integrations.batocera.presentation.refresh_emulationstation",
            lambda config, systems: None,
        )

        result = CliRunner().invoke(
            uidata_module.uidata_group,
            ["library-sync-full"],
            obj={"config_path": "unused"},
        )

        assert result.exit_code == 0
        assert calls == [True]
        assert json.loads(result.stdout)["reconciliation"] == "full"


class TestUpdateBridge:
    def test_check_uses_shared_updater_without_network(self, tmp_path, monkeypatch):
        from romcloud.lifecycle import update as update_module

        current = update_module.BuildInfo(
            version="1.0.0",
            commit="a" * 40,
            commit_short="a" * 12,
            build_date="x",
            source="test",
        )
        latest = update_module.CommitInfo(sha="b" * 40, date="x", message="new")
        monkeypatch.setattr(
            update_module,
            "check_for_update",
            lambda home, progress=None: update_module.CheckResult(
                current=current,
                latest_commit=latest,
                update_available=True,
                latest_version="1.1.0",
            ),
        )

        result = CliRunner().invoke(
            cli,
            ["--config", str(tmp_path / "missing.toml"), "uidata", "update-check"],
        )

        assert result.exit_code == 0
        payload = json.loads(result.stdout.strip())
        assert payload["update_available"] is True
        assert payload["available_version"] == "1.1.0"

    def test_install_uses_shared_lifecycle_and_reports_restart(self, tmp_path, monkeypatch):
        from romcloud.lifecycle import update as update_module

        new = update_module.BuildInfo(
            version="1.1.0",
            commit="b" * 40,
            commit_short="b" * 12,
            build_date="x",
            source="test",
        )
        monkeypatch.setattr(
            update_module,
            "perform_update",
            lambda home, python, progress=None: update_module.UpdateResult(
                previous=None, new=new
            ),
        )

        result = CliRunner().invoke(
            cli,
            ["--config", str(tmp_path / "missing.toml"), "uidata", "update-install"],
        )

        assert result.exit_code == 0
        payload = json.loads(result.stdout.strip())
        assert payload["version"] == "1.1.0"
        assert payload["restart_required"] is True


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
