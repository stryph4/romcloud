"""Unit tests for the `romcloud _reconcile-install` hidden CLI command.

The underlying reconciliation logic is exercised in test_installer.py; here
we only verify the CLI's wiring — argument parsing, output formatting, and
exit codes.
"""

from __future__ import annotations

import stat
from pathlib import Path

from click.testing import CliRunner

from romcloud.cli.commands.reconcile import reconcile_install_cmd
from romcloud.cli.main import cli


def _write_fake_system_python(path: Path, *, has_pygame: bool) -> Path:
    exit_code = "0" if has_pygame else "1"
    path.write_text(
        "#!/bin/bash\n"
        'if [[ "$1" == "-c" && "$2" == "import pygame" ]]; then\n'
        f"    exit {exit_code}\n"
        "fi\n"
        'exec /usr/bin/python3 "$@"\n'
    )
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return path


def _run(args):
    return CliRunner().invoke(reconcile_install_cmd, args)


class TestReconcileInstallCmd:
    def test_hidden_from_help(self) -> None:
        result = CliRunner().invoke(cli, ["--help"])
        assert "_reconcile-install" not in result.output

    def test_writes_wrappers_and_reports_success(self, tmp_path: Path, monkeypatch) -> None:
        from romcloud.integrations.batocera import mount_service, es_config

        monkeypatch.setattr(mount_service, "SERVICE_SCRIPT_PATH", tmp_path / "services" / "romcloud_mount")
        monkeypatch.setattr(es_config, "ROMCLOUD_OVERRIDE_PATH", tmp_path / "es_systems_romcloud.cfg")
        monkeypatch.setenv("ROMCLOUD_GOOGLE_OAUTH_CLIENT_ID", "cli-client")
        monkeypatch.setenv("ROMCLOUD_GOOGLE_OAUTH_CLIENT_SECRET", "cli-secret")

        romcloud_home = tmp_path / "romcloud"
        project_root = tmp_path / "project"
        (project_root / "ports_gfx").mkdir(parents=True)
        (project_root / "ports_gfx" / "__init__.py").write_text("")

        result = _run(
            [
                "--romcloud-home",
                str(romcloud_home),
                "--project-root",
                str(project_root),
                "--ports-dir",
                str(tmp_path / "ports"),
                "--system-python",
                "",
            ]
        )

        assert result.exit_code == 0, result.output
        assert "Wrote CLI wrapper" in result.output
        assert "Wrote launch wrapper" in result.output
        assert "Deployed Google Drive OAuth metadata" in result.output
        assert (romcloud_home / "bin" / "romcloud").exists()
        assert (
            romcloud_home / "runtime" / "google-oauth-client.json"
        ).is_file()

    def test_ports_ui_installed_message(self, tmp_path: Path, monkeypatch) -> None:
        from romcloud.integrations.batocera import mount_service, es_config

        monkeypatch.setattr(mount_service, "SERVICE_SCRIPT_PATH", tmp_path / "services" / "romcloud_mount")
        monkeypatch.setattr(es_config, "ROMCLOUD_OVERRIDE_PATH", tmp_path / "es_systems_romcloud.cfg")

        romcloud_home = tmp_path / "romcloud"
        project_root = tmp_path / "project"
        (project_root / "ports_gfx").mkdir(parents=True)
        (project_root / "ports_gfx" / "__init__.py").write_text("")
        fake_python = _write_fake_system_python(tmp_path / "fake-python-pygame", has_pygame=True)
        ports_dir = tmp_path / "ports"
        ports_dir.mkdir()

        result = _run(
            [
                "--romcloud-home",
                str(romcloud_home),
                "--project-root",
                str(project_root),
                "--ports-dir",
                str(ports_dir),
                "--system-python",
                str(fake_python),
            ]
        )

        assert result.exit_code == 0, result.output
        assert "Wrote graphical Ports wrapper" in result.output
        assert "Installed Batocera Port entry" in result.output

    def test_pygame_missing_reports_skip_message(self, tmp_path: Path, monkeypatch) -> None:
        from romcloud.integrations.batocera import mount_service, es_config

        monkeypatch.setattr(mount_service, "SERVICE_SCRIPT_PATH", tmp_path / "services" / "romcloud_mount")
        monkeypatch.setattr(es_config, "ROMCLOUD_OVERRIDE_PATH", tmp_path / "es_systems_romcloud.cfg")

        romcloud_home = tmp_path / "romcloud"
        project_root = tmp_path / "project"
        (project_root / "ports_gfx").mkdir(parents=True)
        (project_root / "ports_gfx" / "__init__.py").write_text("")
        fake_python = _write_fake_system_python(tmp_path / "fake-python-no-pygame", has_pygame=False)

        result = _run(
            [
                "--romcloud-home",
                str(romcloud_home),
                "--project-root",
                str(project_root),
                "--ports-dir",
                str(tmp_path / "ports"),
                "--system-python",
                str(fake_python),
            ]
        )

        assert result.exit_code == 0, result.output
        assert "Skipping graphical Ports UI" in result.output

    def test_required_wrapper_failure_exits_nonzero(self, tmp_path: Path, monkeypatch) -> None:
        import romcloud.lifecycle.install as installer_module

        def _boom(bin_dir, venv_python):
            raise OSError("disk full")

        monkeypatch.setattr(installer_module, "write_core_wrappers", _boom)

        project_root = tmp_path / "project"
        project_root.mkdir()

        result = _run(
            [
                "--romcloud-home",
                str(tmp_path / "romcloud"),
                "--project-root",
                str(project_root),
                "--ports-dir",
                str(tmp_path / "ports"),
                "--system-python",
                "",
            ]
        )

        assert result.exit_code != 0
        assert "ERROR" in result.output

    def test_missing_project_root_fails_argument_validation(self, tmp_path: Path) -> None:
        result = _run(
            [
                "--romcloud-home",
                str(tmp_path / "romcloud"),
                "--project-root",
                str(tmp_path / "does-not-exist"),
                "--ports-dir",
                str(tmp_path / "ports"),
            ]
        )
        assert result.exit_code != 0
