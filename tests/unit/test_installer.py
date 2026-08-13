"""Unit tests for romcloud.lifecycle.install — the shared
install/update artifact reconciliation logic used by both scripts/install.sh
and romcloud.lifecycle.update.perform_update.
"""

from __future__ import annotations

import stat
from pathlib import Path

import pytest

from romcloud.lifecycle import install as inst


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


def _make_ports_gfx_source(project_root: Path, *, marker: str = "v1") -> Path:
    source = project_root / "ports_gfx"
    source.mkdir(parents=True, exist_ok=True)
    (source / "__init__.py").write_text("")
    (source / "client.py").write_text(f"# {marker}\n")
    (source / "app.py").write_text(f"# {marker}\n")
    (source / "wizard.py").write_text(f"# {marker}\n")
    (source / "assets").mkdir(parents=True, exist_ok=True)
    (source / "assets" / "icon.png").write_bytes(f"fake-png-{marker}".encode())
    return source


# ── core wrappers (required) ──────────────────────────────────────────────────


class TestWriteCoreWrappers:
    def test_writes_executable_wrappers(self, tmp_path: Path) -> None:
        bin_dir = tmp_path / "bin"
        venv_python = tmp_path / "venv" / "bin" / "python"

        result = inst.write_core_wrappers(bin_dir, venv_python)

        assert result.cli_wrapper == bin_dir / "romcloud"
        assert result.launch_wrapper == bin_dir / "romcloud-run"
        assert f'exec "{venv_python}" -m romcloud.cli.main "$@"' in result.cli_wrapper.read_text()
        assert result.launch_wrapper.read_text().splitlines()[0] == f"#!{venv_python}"
        assert result.cli_wrapper.stat().st_mode & 0o111
        assert result.launch_wrapper.stat().st_mode & 0o111

    def test_rerun_overwrites_stale_content(self, tmp_path: Path) -> None:
        bin_dir = tmp_path / "bin"
        venv_python = tmp_path / "venv" / "bin" / "python"
        bin_dir.mkdir(parents=True)
        (bin_dir / "romcloud").write_text("stale content")

        inst.write_core_wrappers(bin_dir, venv_python)

        assert "stale content" not in (bin_dir / "romcloud").read_text()

    def test_idempotent_repeat_calls(self, tmp_path: Path) -> None:
        bin_dir = tmp_path / "bin"
        venv_python = tmp_path / "venv" / "bin" / "python"

        first = inst.write_core_wrappers(bin_dir, venv_python)
        first_content = first.cli_wrapper.read_text()
        inst.write_core_wrappers(bin_dir, venv_python)

        assert (bin_dir / "romcloud").read_text() == first_content


# ── system python detection ───────────────────────────────────────────────────


class TestDetectSystemPython:
    def test_explicit_override_wins(self) -> None:
        assert inst.detect_system_python("/explicit/python") == "/explicit/python"

    def test_falls_back_to_usr_bin_python3_or_path(self) -> None:
        # Whatever this dev machine has, detection must not raise and must
        # return either None or a string path.
        result = inst.detect_system_python(None)
        assert result is None or isinstance(result, str)


# ── graphical Ports UI (best-effort) ──────────────────────────────────────────


class TestInstallPortsUi:
    def test_installed_when_pygame_available(self, tmp_path: Path) -> None:
        project_root = tmp_path / "project"
        _make_ports_gfx_source(project_root)
        fake_python = _write_fake_system_python(tmp_path / "fake-python-pygame", has_pygame=True)
        ports_gfx_dir = tmp_path / "romcloud" / "ports-gfx"
        bin_dir = tmp_path / "romcloud" / "bin"
        ports_dir = tmp_path / "ports"
        ports_dir.mkdir()

        result = inst.install_ports_ui(
            project_root=project_root,
            ports_gfx_dir=ports_gfx_dir,
            bin_dir=bin_dir,
            romcloud_bin=bin_dir / "romcloud",
            ports_dir=ports_dir,
            system_python=str(fake_python),
        )

        assert result.installed is True
        assert result.error is None
        assert (ports_gfx_dir / "ports_gfx" / "client.py").exists()
        assert (ports_gfx_dir / "ports_gfx" / "wizard.py").exists()
        assert result.wrapper_path == bin_dir / "romcloud-ports"
        wrapper_content = result.wrapper_path.read_text()
        assert f'exec "{fake_python}" -m ports_gfx "$@"' in wrapper_content
        assert 'event="wrapper_start"' in wrapper_content
        assert "ROMCLOUD_DISPLAY_LOG" in wrapper_content
        assert f'mkdir -p "{bin_dir.parent / "logs"}"' in wrapper_content
        assert result.launch_progress_wrapper_path == bin_dir / "romcloud-launch-progress"
        launch_progress_content = result.launch_progress_wrapper_path.read_text()
        assert f'exec "{fake_python}" -m ports_gfx.launch_progress "$@"' in launch_progress_content
        assert "ROMCLOUD_BIN" not in launch_progress_content
        assert result.port_entry_path == ports_dir / "ROMCloud.sh"
        port_entry_content = result.port_entry_path.read_text()
        assert f'exec "{result.wrapper_path}" "$@"' in port_entry_content
        assert 'event="port_entry_start"' in port_entry_content

    def test_no_system_python_found(self, tmp_path: Path, monkeypatch) -> None:
        project_root = tmp_path / "project"
        _make_ports_gfx_source(project_root)
        monkeypatch.setattr(inst, "detect_system_python", lambda explicit=None: None)

        result = inst.install_ports_ui(
            project_root=project_root,
            ports_gfx_dir=tmp_path / "ports-gfx",
            bin_dir=tmp_path / "bin",
            romcloud_bin=tmp_path / "bin" / "romcloud",
            ports_dir=tmp_path / "ports",
            system_python=None,
        )

        assert result.installed is False
        assert result.skip_reason == "no_system_python"
        assert not (tmp_path / "ports-gfx").exists()
        assert result.launch_progress_wrapper_path is None

    def test_pygame_missing_is_skipped_cleanly(self, tmp_path: Path) -> None:
        project_root = tmp_path / "project"
        _make_ports_gfx_source(project_root)
        fake_python = _write_fake_system_python(tmp_path / "fake-python-no-pygame", has_pygame=False)

        result = inst.install_ports_ui(
            project_root=project_root,
            ports_gfx_dir=tmp_path / "ports-gfx",
            bin_dir=tmp_path / "bin",
            romcloud_bin=tmp_path / "bin" / "romcloud",
            ports_dir=tmp_path / "ports",
            system_python=str(fake_python),
        )

        assert result.installed is False
        assert result.skip_reason == "no_pygame"
        assert not (tmp_path / "bin" / "romcloud-ports").exists()
        assert not (tmp_path / "bin" / "romcloud-launch-progress").exists()
        assert not (tmp_path / "ports-gfx").exists()

    def test_missing_ports_dir_skips_port_entry_but_not_wrapper(self, tmp_path: Path) -> None:
        project_root = tmp_path / "project"
        _make_ports_gfx_source(project_root)
        fake_python = _write_fake_system_python(tmp_path / "fake-python-pygame", has_pygame=True)
        missing_ports_dir = tmp_path / "does-not-exist" / "ports"

        result = inst.install_ports_ui(
            project_root=project_root,
            ports_gfx_dir=tmp_path / "ports-gfx",
            bin_dir=tmp_path / "bin",
            romcloud_bin=tmp_path / "bin" / "romcloud",
            ports_dir=missing_ports_dir,
            system_python=str(fake_python),
        )

        assert result.installed is True
        assert result.wrapper_path.exists()
        assert result.port_entry_path is None
        assert result.port_entry_skip_reason == "ports_dir_missing"
        assert not missing_ports_dir.exists()

    def test_missing_source_payload_is_skipped_cleanly(self, tmp_path: Path) -> None:
        project_root = tmp_path / "project-without-ports-gfx"
        project_root.mkdir()
        fake_python = _write_fake_system_python(tmp_path / "fake-python-pygame", has_pygame=True)

        result = inst.install_ports_ui(
            project_root=project_root,
            ports_gfx_dir=tmp_path / "ports-gfx",
            bin_dir=tmp_path / "bin",
            romcloud_bin=tmp_path / "bin" / "romcloud",
            ports_dir=tmp_path / "ports",
            system_python=str(fake_python),
        )

        assert result.installed is False
        assert result.skip_reason == "no_source_payload"

    def test_stale_ports_gfx_payload_is_replaced(self, tmp_path: Path) -> None:
        project_root = tmp_path / "project"
        _make_ports_gfx_source(project_root, marker="new")
        fake_python = _write_fake_system_python(tmp_path / "fake-python-pygame", has_pygame=True)
        ports_gfx_dir = tmp_path / "ports-gfx"

        stale_target = ports_gfx_dir / "ports_gfx"
        stale_target.mkdir(parents=True)
        (stale_target / "stale_only_file.py").write_text("# stale\n")
        (stale_target / "client.py").write_text("# old\n")

        result = inst.install_ports_ui(
            project_root=project_root,
            ports_gfx_dir=ports_gfx_dir,
            bin_dir=tmp_path / "bin",
            romcloud_bin=tmp_path / "bin" / "romcloud",
            ports_dir=tmp_path / "ports",
            system_python=str(fake_python),
        )

        assert result.installed is True
        assert not (stale_target / "stale_only_file.py").exists()
        assert "new" in (stale_target / "client.py").read_text()

    def test_repeated_reconciliation_is_idempotent(self, tmp_path: Path) -> None:
        project_root = tmp_path / "project"
        _make_ports_gfx_source(project_root)
        fake_python = _write_fake_system_python(tmp_path / "fake-python-pygame", has_pygame=True)
        ports_gfx_dir = tmp_path / "ports-gfx"
        bin_dir = tmp_path / "bin"
        ports_dir = tmp_path / "ports"
        ports_dir.mkdir()

        kwargs = dict(
            project_root=project_root,
            ports_gfx_dir=ports_gfx_dir,
            bin_dir=bin_dir,
            romcloud_bin=bin_dir / "romcloud",
            ports_dir=ports_dir,
            system_python=str(fake_python),
        )

        first = inst.install_ports_ui(**kwargs)
        second = inst.install_ports_ui(**kwargs)

        assert first.installed is second.installed is True
        assert (ports_gfx_dir / "ports_gfx" / "client.py").exists()


# ── previously-enabled Batocera integrations (best-effort) ────────────────────


class TestReconcileMountService:
    def test_installs_owned_boot_service_when_never_installed(self, tmp_path: Path, monkeypatch) -> None:
        from romcloud.integrations.batocera import mount_service

        service_path = tmp_path / "services" / "romcloud_mount"
        monkeypatch.setattr(mount_service, "SERVICE_SCRIPT_PATH", service_path)

        assert inst.reconcile_mount_service(tmp_path / "bin") is True
        assert service_path.is_file()
        assert "mount boot-start" in service_path.read_text(encoding="utf-8")

    def test_restores_missing_override_from_existing_catalog(self, tmp_path: Path, monkeypatch) -> None:
        from romcloud.integrations.batocera import mount_service

        service_path = tmp_path / "services" / "romcloud_mount"
        service_path.parent.mkdir(parents=True)
        service_path.write_text("stale script content")
        monkeypatch.setattr(mount_service, "SERVICE_SCRIPT_PATH", service_path)

        result = inst.reconcile_mount_service(tmp_path / "bin")

        assert result is True
        content = service_path.read_text()
        assert "stale script content" not in content
        assert str(tmp_path / "bin" / "romcloud") in content


class TestReconcileEsOverride:
    def test_not_applicable_when_never_installed(self, tmp_path: Path, monkeypatch) -> None:
        from romcloud.integrations.batocera import es_config

        monkeypatch.setattr(es_config, "ROMCLOUD_OVERRIDE_PATH", tmp_path / "es_systems_romcloud.cfg")

        assert inst.reconcile_es_override(tmp_path / "romcloud.toml") is None

    def test_reconciled_when_already_installed(self, tmp_path: Path, monkeypatch) -> None:
        from romcloud.integrations.batocera import es_config
        from romcloud.core.models.game import Game, GameAsset
        from romcloud.infrastructure.database import Database
        from romcloud.infrastructure.repositories.game import GameRepository
        from datetime import datetime, timezone

        stock_path = tmp_path / "es_systems.cfg"
        stock_path.write_text(
            '<?xml version="1.0"?>\n'
            "<systemList>\n"
            "  <system>\n"
            "    <name>snes</name>\n"
            "    <fullname>Super Nintendo</fullname>\n"
            "    <extension>.sfc</extension>\n"
            "    <command>emulatorlauncher %CONTROLLERSCONFIG% -system %SYSTEM% "
            "-rom %ROM% -gameinfoxml %GAMEINFOXML% -systemname %SYSTEMNAME%</command>\n"
            "  </system>\n"
            "</systemList>\n"
        )
        override_path = tmp_path / "es_systems_romcloud.cfg"

        monkeypatch.setattr(es_config, "STOCK_ES_SYSTEMS_PATH", stock_path)
        monkeypatch.setattr(es_config, "ROMCLOUD_OVERRIDE_PATH", override_path)
        monkeypatch.setattr(es_config, "WRAPPER_SCRIPT_PATH", tmp_path / "bin" / "romcloud-run")

        data_dir = tmp_path / "data"
        data_dir.mkdir()
        db = Database(str(data_dir / "catalog.db"))
        db.initialize()
        GameRepository(db).save(
            Game.create(
                system="snes",
                title="Some Game",
                source_provider="local",
                source_root="/roms",
                assets=[GameAsset(filename="Some Game.sfc", relative_path="snes/Some Game.sfc", is_primary=True)],
            )
        )

        config_path = tmp_path / "romcloud.toml"
        config_path.write_text(
            "[source]\n"
            'provider = "local"\n'
            'rom_root = "/roms"\n'
            "\n"
            "[cache]\n"
            f'path = "{tmp_path / "cache"}"\n'
            "\n"
            "[local_roms]\n"
            f'path = "{tmp_path / "roms"}"\n'
            "\n"
            "[data]\n"
            f'path = "{data_dir}"\n'
        )

        result = inst.reconcile_es_override(config_path)

        assert result is True
        content = override_path.read_text()
        assert "snes" in content
        assert ".romcloud" in content


class TestReconcilePortsGamelist:
    def test_not_applicable_when_no_port_entry_installed(self, tmp_path: Path) -> None:
        ports_ui = inst.PortsUiResult(installed=False, skip_reason="no_pygame")

        assert inst.reconcile_ports_gamelist(ports_ui, tmp_path / "ports") is None

    def test_reconciled_when_port_entry_installed(self, tmp_path: Path) -> None:
        ports_gfx_target = tmp_path / "ports-gfx" / "ports_gfx"
        (ports_gfx_target / "assets").mkdir(parents=True)
        (ports_gfx_target / "assets" / "icon.png").write_bytes(b"fake-png-bytes")
        ports_dir = tmp_path / "ports"
        ports_dir.mkdir()
        port_entry_path = ports_dir / "ROMCloud.sh"
        port_entry_path.write_text("#!/bin/bash\nexec true\n")

        ports_ui = inst.PortsUiResult(
            installed=True,
            ports_gfx_dir=ports_gfx_target,
            wrapper_path=tmp_path / "bin" / "romcloud-ports",
            port_entry_path=port_entry_path,
        )

        result = inst.reconcile_ports_gamelist(ports_ui, ports_dir)

        assert result is True
        content = (ports_dir / "gamelist.xml").read_text()
        assert "<path>./ROMCloud.sh</path>" in content
        assert "<image>./images/ROMCloud.png</image>" in content
        assert (ports_dir / "images" / "ROMCloud.png").read_bytes() == b"fake-png-bytes"

    def test_preserves_unrelated_entries_when_reconciled(self, tmp_path: Path) -> None:
        ports_gfx_target = tmp_path / "ports-gfx" / "ports_gfx"
        ports_gfx_target.mkdir(parents=True)
        ports_dir = tmp_path / "ports"
        ports_dir.mkdir()
        (ports_dir / "gamelist.xml").write_text(
            """<?xml version="1.0"?>
<gameList>
  <game>
    <path>./SomeOtherPort.sh</path>
    <name>Some Other Port</name>
    <image>./images/some-other-port.png</image>
  </game>
</gameList>
"""
        )
        port_entry_path = ports_dir / "ROMCloud.sh"
        port_entry_path.write_text("#!/bin/bash\nexec true\n")

        ports_ui = inst.PortsUiResult(
            installed=True,
            ports_gfx_dir=ports_gfx_target,
            wrapper_path=tmp_path / "bin" / "romcloud-ports",
            port_entry_path=port_entry_path,
        )

        result = inst.reconcile_ports_gamelist(ports_ui, ports_dir)

        assert result is True
        content = (ports_dir / "gamelist.xml").read_text()
        assert "<path>./SomeOtherPort.sh</path>" in content
        assert "<path>./ROMCloud.sh</path>" in content


# ── full reconciliation ────────────────────────────────────────────────────────


class TestReconcileInstall:
    def test_reconciles_auto_savesync_hook(self, tmp_path: Path, monkeypatch) -> None:
        from romcloud.integrations.batocera import auto_savesync

        hook = tmp_path / "scripts" / "romcloud-autosync"
        monkeypatch.setattr(auto_savesync, "HOOK_PATH", hook)

        assert inst.reconcile_auto_savesync_hook(tmp_path / "bin") is True
        assert hook.is_file()
        assert "_autosync gameStop" not in hook.read_text(encoding="utf-8")
        assert "_autosync game-stop" in hook.read_text(encoding="utf-8")

    def test_reconciles_core_and_ports_ui(self, tmp_path: Path, monkeypatch) -> None:
        from romcloud.integrations.batocera import auto_savesync, mount_service, es_config

        # Isolate from any real Batocera paths on the machine running tests.
        monkeypatch.setattr(mount_service, "SERVICE_SCRIPT_PATH", tmp_path / "services" / "romcloud_mount")
        monkeypatch.setattr(es_config, "ROMCLOUD_OVERRIDE_PATH", tmp_path / "es_systems_romcloud.cfg")
        monkeypatch.setattr(
            auto_savesync, "HOOK_PATH", tmp_path / "scripts" / "romcloud-autosync"
        )

        romcloud_home = tmp_path / "romcloud"
        project_root = tmp_path / "project"
        _make_ports_gfx_source(project_root)
        fake_python = _write_fake_system_python(tmp_path / "fake-python-pygame", has_pygame=True)
        ports_dir = tmp_path / "ports"
        ports_dir.mkdir()

        report = inst.reconcile_install(
            romcloud_home=romcloud_home,
            project_root=project_root,
            ports_dir=ports_dir,
            system_python=str(fake_python),
        )

        assert report.core.cli_wrapper == romcloud_home / "bin" / "romcloud"
        assert report.core.cli_wrapper.exists()
        assert report.core.launch_wrapper.exists()
        assert report.ports_ui.installed is True
        assert report.mount_service is True
        assert report.es_override is None
        assert report.ports_gamelist is True
        assert report.autosync_hook is True
        assert auto_savesync.HOOK_PATH.is_file()
        assert report.ports_ui.launch_progress_wrapper_path == romcloud_home / "bin" / "romcloud-launch-progress"
        assert report.ports_ui.launch_progress_wrapper_path.exists()
        gamelist_content = (ports_dir / "gamelist.xml").read_text()
        assert "<path>./ROMCloud.sh</path>" in gamelist_content
        assert "<image>./images/ROMCloud.png</image>" in gamelist_content
        assert (ports_dir / "images" / "ROMCloud.png").exists()

    def test_repeated_reconciliation_is_harmless(self, tmp_path: Path, monkeypatch) -> None:
        from romcloud.integrations.batocera import auto_savesync, mount_service, es_config

        monkeypatch.setattr(mount_service, "SERVICE_SCRIPT_PATH", tmp_path / "services" / "romcloud_mount")
        monkeypatch.setattr(es_config, "ROMCLOUD_OVERRIDE_PATH", tmp_path / "es_systems_romcloud.cfg")
        monkeypatch.setattr(
            auto_savesync, "HOOK_PATH", tmp_path / "scripts" / "romcloud-autosync"
        )

        romcloud_home = tmp_path / "romcloud"
        project_root = tmp_path / "project"
        _make_ports_gfx_source(project_root)
        fake_python = _write_fake_system_python(tmp_path / "fake-python-pygame", has_pygame=True)
        ports_dir = tmp_path / "ports"
        ports_dir.mkdir()

        kwargs = dict(
            romcloud_home=romcloud_home,
            project_root=project_root,
            ports_dir=ports_dir,
            system_python=str(fake_python),
        )
        inst.reconcile_install(**kwargs)
        gamelist_path = kwargs["ports_dir"] / "gamelist.xml"
        content_after_first = gamelist_path.read_text()
        second = inst.reconcile_install(**kwargs)

        assert second.core.cli_wrapper.exists()
        assert second.ports_ui.installed is True
        assert second.ports_gamelist is True
        assert gamelist_path.read_text() == content_after_first
