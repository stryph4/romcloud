"""Unit tests for romcloud.integrations.batocera.mount_service.

Covers: generated service script content (dispatches `start` to the
non-blocking `romcloud mount boot-start`, `stop`/`status` to their matching
subcommands; `start`/`stop` always exit 0 so a ROMCloud failure can never
fail Batocera boot), install/remove file handling, correct permissions,
and graceful tolerance of a missing `batocera-services` binary.
"""

from __future__ import annotations

from pathlib import Path

from romcloud.integrations.batocera import mount_service


class TestGenerateServiceScript:
    def test_contains_shebang(self):
        content = mount_service.generate_service_script("/userdata/system/romcloud/bin/romcloud")
        assert content.startswith("#!/bin/bash\n")

    def test_start_dispatches_to_boot_start_not_blocking_start(self):
        """`start` must route through the non-blocking `boot-start`, never
        the blocking `mount start` — real-hardware testing showed the latter
        can hang Batocera boot."""
        content = mount_service.generate_service_script("/path/to/romcloud")
        assert 'ROMCLOUD_BIN="/path/to/romcloud"' in content
        assert '"${ROMCLOUD_BIN}" mount boot-start' in content
        assert 'if "${ROMCLOUD_BIN}" uidata manager-start' in content
        assert '"${ROMCLOUD_BIN}" uidata startup-integration-activated' in content
        assert content.index("manager-start") < content.index(
            "startup-integration-activated"
        )

    def test_stop_and_status_dispatch_correctly(self):
        content = mount_service.generate_service_script("/path/to/romcloud")
        assert '"${ROMCLOUD_BIN}" mount stop --shutdown' in content
        assert '"${ROMCLOUD_BIN}" uidata manager-stop' in content
        assert '"${ROMCLOUD_BIN}" mount status' in content

    def test_start_and_stop_always_exit_zero(self):
        """Even if the romcloud command itself fails, `start`/`stop` must
        still report success to Batocera's service supervisor —
        "ROMCloud may fail; Batocera must not."""
        content = mount_service.generate_service_script("/path/to/romcloud")
        start_block = content.split('start)')[1].split(';;')[0]
        stop_block = content.split('stop)')[1].split(';;')[0]
        assert "|| true" in start_block
        assert "exit 0" in start_block
        assert "|| true" in stop_block
        assert "exit 0" in stop_block

    def test_does_not_use_errexit(self):
        """`set -e` could abort the script before reaching the safety-net
        `exit 0` — must not be used."""
        content = mount_service.generate_service_script("/path/to/romcloud")
        set_line = next(line for line in content.splitlines() if line.startswith("set "))
        assert set_line == "set -uo pipefail"

    def test_unknown_argument_prints_usage_and_fails(self):
        content = mount_service.generate_service_script("/path/to/romcloud")
        assert "Usage:" in content
        assert "exit 1" in content


class TestInstallService:
    def test_writes_executable_script(self, tmp_path):
        service_path = tmp_path / "services" / mount_service.SERVICE_NAME
        result_path = mount_service.install_service("/bin/romcloud", service_path=service_path)

        assert result_path == service_path
        assert service_path.exists()
        assert service_path.stat().st_mode & 0o777 == 0o755

    def test_tolerates_missing_batocera_services_binary(self, tmp_path, monkeypatch):
        """Must not raise even when batocera-services isn't on PATH (e.g. dev machine)."""
        monkeypatch.setenv("PATH", str(tmp_path / "empty-bin"))
        (tmp_path / "empty-bin").mkdir()

        service_path = tmp_path / "services" / mount_service.SERVICE_NAME
        # Should not raise.
        mount_service.install_service("/bin/romcloud", service_path=service_path)
        assert service_path.exists()

    def test_is_service_installed(self, tmp_path):
        service_path = tmp_path / mount_service.SERVICE_NAME
        assert mount_service.is_service_installed(service_path=service_path) is False
        service_path.write_text("x")
        assert mount_service.is_service_installed(service_path=service_path) is True

    def test_changed_script_marks_restart_required(self, tmp_path, monkeypatch):
        monkeypatch.setattr(mount_service.subprocess, "run", lambda *args, **kwargs: None)
        service_path = tmp_path / "services" / mount_service.SERVICE_NAME
        config_path = tmp_path / "batocera.conf"
        config_path.write_text(f"system.services={mount_service.SERVICE_NAME}\n")
        activation_path = tmp_path / "state" / "startup-integration.json"

        mount_service.install_service(
            "/bin/romcloud",
            service_path=service_path,
            activation_state_path=activation_path,
            services_config_path=config_path,
        )

        assert mount_service.startup_activation.activation_status(
            activation_path
        )["startup_restart_required"] is True

    def test_unchanged_enabled_service_does_not_mark_restart_required(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setattr(mount_service.subprocess, "run", lambda *args, **kwargs: None)
        service_path = tmp_path / "services" / mount_service.SERVICE_NAME
        service_path.parent.mkdir()
        service_path.write_text(
            mount_service.generate_service_script("/bin/romcloud"), encoding="utf-8"
        )
        config_path = tmp_path / "batocera.conf"
        config_path.write_text(f"system.services={mount_service.SERVICE_NAME}\n")
        activation_path = tmp_path / "state" / "startup-integration.json"

        mount_service.install_service(
            "/bin/romcloud",
            service_path=service_path,
            activation_state_path=activation_path,
            services_config_path=config_path,
        )

        assert not activation_path.exists()

    def test_newly_enabled_service_marks_restart_required(self, tmp_path, monkeypatch):
        monkeypatch.setattr(mount_service.subprocess, "run", lambda *args, **kwargs: None)
        service_path = tmp_path / "services" / mount_service.SERVICE_NAME
        service_path.parent.mkdir()
        service_path.write_text(
            mount_service.generate_service_script("/bin/romcloud"), encoding="utf-8"
        )
        config_path = tmp_path / "batocera.conf"
        config_path.write_text("system.services=other_service\n")
        activation_path = tmp_path / "state" / "startup-integration.json"

        mount_service.install_service(
            "/bin/romcloud",
            service_path=service_path,
            activation_state_path=activation_path,
            services_config_path=config_path,
        )

        assert activation_path.exists()


class TestRemoveService:
    def test_removes_existing_script(self, tmp_path, monkeypatch):
        monkeypatch.setenv("PATH", str(tmp_path / "empty-bin"))
        (tmp_path / "empty-bin").mkdir()

        service_path = tmp_path / "services" / mount_service.SERVICE_NAME
        mount_service.install_service("/bin/romcloud", service_path=service_path)

        removed = mount_service.remove_service(service_path=service_path)
        assert removed is True
        assert not service_path.exists()

    def test_noop_when_nothing_installed(self, tmp_path, monkeypatch):
        monkeypatch.setenv("PATH", str(tmp_path / "empty-bin"))
        (tmp_path / "empty-bin").mkdir()

        service_path = tmp_path / "services" / mount_service.SERVICE_NAME
        removed = mount_service.remove_service(service_path=service_path)
        assert removed is False

    def test_does_not_touch_other_files_in_services_directory(self, tmp_path, monkeypatch):
        monkeypatch.setenv("PATH", str(tmp_path / "empty-bin"))
        (tmp_path / "empty-bin").mkdir()

        services_dir = tmp_path / "services"
        services_dir.mkdir()
        unrelated = services_dir / "some-other-service"
        unrelated.write_text("keep me")

        service_path = services_dir / mount_service.SERVICE_NAME
        mount_service.install_service("/bin/romcloud", service_path=service_path)
        mount_service.remove_service(service_path=service_path)

        assert unrelated.exists()
        assert unrelated.read_text() == "keep me"
