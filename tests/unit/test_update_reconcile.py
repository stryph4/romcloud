"""Regression tests for `romcloud update` reconciling the full installed
application (wrappers, graphical Ports UI, previously-enabled Batocera
integrations) — not just the venv/package.

The pip-install step is always faked (never spawns a real venv); the
reconcile step is routed through the real `_reconcile-install` CLI command
in-process via Click's CliRunner, so these tests exercise the actual
reconciliation logic (romcloud.lifecycle.install) end-to-end without
needing a real venv or system Python.
"""

from __future__ import annotations

import io
import json
import stat
import subprocess
import zipfile
from pathlib import Path

import pytest
from click.testing import CliRunner

from romcloud.cli.commands.reconcile import reconcile_install_cmd
from romcloud.core.exceptions import UpdateInstallError
from romcloud.lifecycle import update as upd

_SHA = "abc123def456" + "0" * 28


class _FakeHTTPResponse:
    def __init__(self, data: bytes) -> None:
        self._buf = io.BytesIO(data)

    def read(self, n: int = -1) -> bytes:
        return self._buf.read(n)

    def __enter__(self) -> "_FakeHTTPResponse":
        return self

    def __exit__(self, *exc) -> bool:
        return False


def _make_opener(payloads: dict):
    def opener(request, timeout=None):
        url = request.full_url
        if url not in payloads:
            raise AssertionError(f"Unexpected URL requested: {url}")
        payload = payloads[url]
        if isinstance(payload, BaseException):
            raise payload
        return _FakeHTTPResponse(payload)

    return opener


def _commit_json(sha: str) -> bytes:
    return json.dumps(
        {"sha": sha, "commit": {"committer": {"date": "2026-08-08T00:00:00Z"}, "message": "msg"}}
    ).encode()


def _make_update_archive(sha: str = _SHA, version: str = "9.9.9", ports_gfx_marker: str = "fresh") -> bytes:
    top = f"romcloud-{sha}"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(f"{top}/pyproject.toml", f'[project]\nname = "romcloud"\nversion = "{version}"\n')
        zf.writestr(f"{top}/src/romcloud/__init__.py", f'__version__ = "{version}"\n')
        zf.writestr(f"{top}/src/romcloud/cli/main.py", "def cli():\n    pass\n")
        zf.writestr(f"{top}/ports_gfx/__init__.py", "# package\n")
        zf.writestr(f"{top}/ports_gfx/client.py", f"# {ports_gfx_marker}\n")
        zf.writestr(f"{top}/ports_gfx/app.py", f"# {ports_gfx_marker}\n")
        zf.writestr(f"{top}/_padding.bin", b"0" * 4096)
    return buf.getvalue()


def _full_payloads(sha: str = _SHA, **archive_kwargs) -> dict:
    return {
        upd.commit_api_url(upd.DEFAULT_REPO, upd.DEFAULT_BRANCH): _commit_json(sha),
        upd.archive_download_url(upd.DEFAULT_REPO, sha): _make_update_archive(sha=sha, **archive_kwargs),
    }


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


def _make_runner(*, pip_returncode: int = 0, pip_stderr: str = "", reconcile_argv_override=None):
    """Fakes the pip-install subprocess call; routes the reconcile
    subcommand through the real CLI command in-process."""

    def runner(argv, **kwargs):
        if len(argv) >= 4 and argv[1:3] == ["-m", "venv"]:
            candidate = Path(argv[3])
            (candidate / "bin").mkdir(parents=True, exist_ok=True)
            (candidate / "bin" / "python").write_text("candidate")
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
        if "pip" in argv:
            return subprocess.CompletedProcess(argv, pip_returncode, stdout="", stderr=pip_stderr)
        if argv[1:3] == ["-m", "romcloud.cli.main"] and "_reconcile-install" not in argv:
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
        assert argv[1:4] == ["-m", "romcloud.cli.main", "_reconcile-install"]
        args = reconcile_argv_override if reconcile_argv_override is not None else argv[4:]
        result = CliRunner().invoke(reconcile_install_cmd, args)
        return subprocess.CompletedProcess(argv, result.exit_code, stdout=result.output, stderr="")

    return runner


@pytest.fixture(autouse=True)
def _isolate_batocera_integration_paths(tmp_path, monkeypatch):
    """Every test in this module gets its own tmp_path-scoped mount service
    and ES override path, so reconciliation never touches real machine
    paths like /userdata/system/services or the real ES override file."""
    from romcloud.integrations.batocera import mount_service, es_config

    monkeypatch.setattr(mount_service, "SERVICE_SCRIPT_PATH", tmp_path / "services" / "romcloud_mount")
    monkeypatch.setattr(es_config, "ROMCLOUD_OVERRIDE_PATH", tmp_path / "es_systems_romcloud.cfg")
    monkeypatch.setattr(es_config, "STOCK_ES_SYSTEMS_PATH", tmp_path / "stock_es_systems.cfg")


def _old_install_layout(tmp_path: Path) -> Path:
    """A plausible pre-existing ROMCloud install directory: config,
    credentials, data, cache, logs, an old version.json, and an unrelated
    user file — but no venv/bin content of its own concern (pip is faked)."""
    home = tmp_path / "romcloud"
    (home / "bin").mkdir(parents=True)
    (home / "venv" / "bin").mkdir(parents=True)
    (home / "venv" / "bin" / "python").write_text("#!/bin/bash\necho fake venv python\n")
    (home / "config").mkdir(parents=True)
    (home / "config" / "romcloud.toml").write_text("# user config\n[source]\nprovider = \"local\"\n")
    (home / "config" / "credentials.toml").write_text("# secret\n")
    (home / "data").mkdir(parents=True)
    (home / "data" / "catalog.db").write_bytes(b"fake sqlite bytes")
    (home / "logs").mkdir(parents=True)
    (home / "logs" / "romcloud.log").write_text("old log line\n")
    (home / "notes.txt").write_text("unrelated user file — do not touch\n")
    upd.write_build_info(
        home,
        upd.BuildInfo(version="1.0.0", commit="old" * 13, commit_short="oldoldoldold", build_date="x", source="s"),
    )
    return home


class TestReconciliationDuringUpdate:
    def test_old_install_with_no_ports_ui_creates_it(self, tmp_path: Path) -> None:
        home = _old_install_layout(tmp_path)
        fake_python = _write_fake_system_python(tmp_path / "fake-python-pygame", has_pygame=True)
        ports_dir = tmp_path / "ports"
        ports_dir.mkdir()
        opener = _make_opener(_full_payloads())

        assert not (home / "ports-gfx").exists()

        result = upd.perform_update(
            home, home / "venv" / "bin" / "python",
            opener=opener, runner=_make_runner(),
            ports_dir=ports_dir, system_python=str(fake_python),
        )

        assert result.new.commit == _SHA
        assert (home / "ports-gfx" / "ports_gfx" / "client.py").exists()
        assert (home / "bin" / "romcloud-ports").exists()
        assert (ports_dir / "ROMCloud.sh").exists()

    def test_stale_ports_gfx_replaced_with_new_source(self, tmp_path: Path) -> None:
        home = _old_install_layout(tmp_path)
        stale_target = home / "ports-gfx" / "ports_gfx"
        stale_target.mkdir(parents=True)
        (stale_target / "client.py").write_text("# old\n")
        (stale_target / "stale_only_file.py").write_text("# stale, should be removed\n")
        fake_python = _write_fake_system_python(tmp_path / "fake-python-pygame", has_pygame=True)
        opener = _make_opener(_full_payloads())

        upd.perform_update(
            home, home / "venv" / "bin" / "python",
            opener=opener, runner=_make_runner(),
            ports_dir=tmp_path / "ports", system_python=str(fake_python),
        )

        assert "fresh" in (stale_target / "client.py").read_text()
        assert not (stale_target / "stale_only_file.py").exists()

    def test_missing_romcloud_sh_recreated(self, tmp_path: Path) -> None:
        home = _old_install_layout(tmp_path)
        ports_dir = tmp_path / "ports"
        ports_dir.mkdir()
        fake_python = _write_fake_system_python(tmp_path / "fake-python-pygame", has_pygame=True)
        opener = _make_opener(_full_payloads())

        assert not (ports_dir / "ROMCloud.sh").exists()

        upd.perform_update(
            home, home / "venv" / "bin" / "python",
            opener=opener, runner=_make_runner(),
            ports_dir=ports_dir, system_python=str(fake_python),
        )

        assert (ports_dir / "ROMCloud.sh").exists()

    def test_stale_wrappers_refreshed(self, tmp_path: Path) -> None:
        home = _old_install_layout(tmp_path)
        (home / "bin" / "romcloud").write_text("#!/bin/bash\necho stale wrapper\n")
        (home / "bin" / "romcloud-run").write_text("#!/usr/bin/env stale-python\nstale\n")
        opener = _make_opener(_full_payloads())

        upd.perform_update(
            home, home / "venv" / "bin" / "python",
            opener=opener, runner=_make_runner(),
            ports_dir=tmp_path / "ports", system_python=None,
        )

        venv_python = home / "venv" / "bin" / "python"
        assert f'exec "{venv_python}" -m romcloud.cli.main "$@"' in (home / "bin" / "romcloud").read_text()
        assert (home / "bin" / "romcloud-run").read_text().splitlines()[0] == f"#!{venv_python}"

    def test_managed_integration_files_reconciled(self, tmp_path: Path) -> None:
        from romcloud.integrations.batocera import mount_service

        home = _old_install_layout(tmp_path)
        service_path = mount_service.SERVICE_SCRIPT_PATH
        service_path.parent.mkdir(parents=True)
        service_path.write_text("stale service script content")
        opener = _make_opener(_full_payloads())

        upd.perform_update(
            home, home / "venv" / "bin" / "python",
            opener=opener, runner=_make_runner(),
            ports_dir=tmp_path / "ports", system_python=None,
        )

        content = service_path.read_text()
        assert "stale service script content" not in content
        assert str(home / "bin" / "romcloud") in content

    def test_existing_config_credentials_data_cache_unchanged(self, tmp_path: Path) -> None:
        home = _old_install_layout(tmp_path)
        config_before = (home / "config" / "romcloud.toml").read_text()
        creds_before = (home / "config" / "credentials.toml").read_text()
        data_before = (home / "data" / "catalog.db").read_bytes()
        opener = _make_opener(_full_payloads())

        upd.perform_update(
            home, home / "venv" / "bin" / "python",
            opener=opener, runner=_make_runner(),
            ports_dir=tmp_path / "ports", system_python=None,
        )

        assert (home / "config" / "romcloud.toml").read_text() == config_before
        assert (home / "config" / "credentials.toml").read_text() == creds_before
        assert (home / "data" / "catalog.db").read_bytes() == data_before

    def test_unrelated_user_file_untouched(self, tmp_path: Path) -> None:
        home = _old_install_layout(tmp_path)
        before = (home / "notes.txt").read_text()
        opener = _make_opener(_full_payloads())

        upd.perform_update(
            home, home / "venv" / "bin" / "python",
            opener=opener, runner=_make_runner(),
            ports_dir=tmp_path / "ports", system_python=None,
        )

        assert (home / "notes.txt").read_text() == before

    def test_repeated_reconciliation_is_harmless(self, tmp_path: Path) -> None:
        home = _old_install_layout(tmp_path)
        fake_python = _write_fake_system_python(tmp_path / "fake-python-pygame", has_pygame=True)
        ports_dir = tmp_path / "ports"
        ports_dir.mkdir()
        opener = _make_opener(_full_payloads())

        first = upd.perform_update(
            home, home / "venv" / "bin" / "python",
            opener=opener, runner=_make_runner(),
            ports_dir=ports_dir, system_python=str(fake_python),
        )
        second = upd.perform_update(
            home, home / "venv" / "bin" / "python",
            opener=_make_opener(_full_payloads()), runner=_make_runner(),
            ports_dir=ports_dir, system_python=str(fake_python),
        )

        assert first.new.commit == second.new.commit == _SHA
        assert (home / "ports-gfx" / "ports_gfx" / "client.py").exists()
        assert (ports_dir / "ROMCloud.sh").exists()

    def test_version_and_sha_metadata_correct_after_full_update(self, tmp_path: Path) -> None:
        home = _old_install_layout(tmp_path)
        opener = _make_opener(_full_payloads(version="5.5.5"))

        result = upd.perform_update(
            home, home / "venv" / "bin" / "python",
            opener=opener, runner=_make_runner(),
            ports_dir=tmp_path / "ports", system_python=None,
        )

        assert result.new.version == "5.5.5"
        assert result.new.commit == _SHA
        assert result.new.commit_short == _SHA[:12]
        on_disk = upd.read_build_info(home)
        assert on_disk == result.new

    def test_partial_required_artifact_failure_does_not_mark_update_successful(self, tmp_path: Path) -> None:
        home = _old_install_layout(tmp_path)
        previous = upd.read_build_info(home)
        opener = _make_opener(_full_payloads())

        # Force the reconcile subcommand itself to fail (simulating a
        # required-artifact write failure) by pointing --romcloud-home at an
        # unwritable-looking location: a file where a directory is expected.
        (tmp_path / "not-a-directory").write_text("blocking file")
        candidate_runner = _make_runner()

        def runner(argv, **kwargs):
            if "_reconcile-install" not in argv:
                return candidate_runner(argv, **kwargs)
            project_root = argv[argv.index("--project-root") + 1]
            args = [
                "--romcloud-home", str(tmp_path / "not-a-directory" / "romcloud"),
                "--project-root", project_root,
                "--ports-dir", str(tmp_path / "ports"),
                "--system-python", "",
            ]
            result = CliRunner().invoke(reconcile_install_cmd, args)
            return subprocess.CompletedProcess(argv, result.exit_code, stdout=result.output, stderr=result.output)

        with pytest.raises(UpdateInstallError, match="reconcile"):
            upd.perform_update(
                home, home / "venv" / "bin" / "python",
                opener=opener, runner=runner,
                ports_dir=tmp_path / "ports", system_python=None,
            )

        assert upd.read_build_info(home) == previous

    def test_graphical_detection_failure_does_not_damage_backend_install(self, tmp_path: Path) -> None:
        home = _old_install_layout(tmp_path)
        opener = _make_opener(_full_payloads())

        result = upd.perform_update(
            home, home / "venv" / "bin" / "python",
            opener=opener, runner=_make_runner(),
            ports_dir=tmp_path / "ports", system_python="/definitely/does/not/exist",
        )

        assert result.new.commit == _SHA
        assert (home / "bin" / "romcloud").exists()
        assert (home / "bin" / "romcloud-run").exists()
        assert not (home / "bin" / "romcloud-ports").exists()

    def test_reconciliation_works_without_git(self, tmp_path: Path, monkeypatch) -> None:
        empty_bin = tmp_path / "empty-bin"
        empty_bin.mkdir()
        import shutil as _shutil

        python3 = _shutil.which("python3")
        assert python3 is not None
        (empty_bin / "python3").symlink_to(python3)
        monkeypatch.setenv("PATH", str(empty_bin))
        assert _shutil.which("git") is None

        home = _old_install_layout(tmp_path)
        opener = _make_opener(_full_payloads())

        result = upd.perform_update(
            home, home / "venv" / "bin" / "python",
            opener=opener, runner=_make_runner(),
            ports_dir=tmp_path / "ports", system_python=None,
        )

        assert result.new.commit == _SHA
        assert (home / "bin" / "romcloud").exists()
