"""Unit tests for romcloud.integrations.batocera.mount_service.

Covers: generated service script content (dispatches start/stop/status to
`romcloud mount ...`), install/remove file handling, correct permissions,
and graceful tolerance of a missing `batocera-services` binary.
"""

from __future__ import annotations

from pathlib import Path

from romcloud.integrations.batocera import mount_service


class TestGenerateServiceScript:
    def test_contains_shebang(self):
        content = mount_service.generate_service_script("/userdata/system/romcloud/bin/romcloud")
        assert content.startswith("#!/bin/bash\n")

    def test_dispatches_to_romcloud_mount_subcommands(self):
        content = mount_service.generate_service_script("/path/to/romcloud")
        assert 'ROMCLOUD_BIN="/path/to/romcloud"' in content
        assert '"${ROMCLOUD_BIN}" mount start' in content
        assert '"${ROMCLOUD_BIN}" mount stop' in content
        assert '"${ROMCLOUD_BIN}" mount status' in content

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
