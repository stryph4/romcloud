"""Tests for scripts/install.sh.

Focused on the shell-generation and dependency issues that matter most for a
real Batocera install:

- Only `python3` is required globally — there is no global `pip3` on
  Batocera 42, so the installer must create a persistent virtual environment
  under `${ROMCLOUD_HOME}/venv`, bootstrapping pip inside it via `ensurepip`
  if the venv doesn't already have one, and install ROMCloud with the venv's
  own `python -m pip` — never the system Python/pip.
- The generated `romcloud` and `romcloud-run` wrappers must exec the venv's
  python directly, and must preserve runtime `"$@"` (not expand it during
  installation).
- The installer must NEVER touch `/userdata/system/custom.sh` (foreign
  Batocera/user-addon startup state) — no append, no source, no overwrite.
  The CLI is not added to any PATH; the installer prints its full path
  clearly instead (see real-hardware findings: sourcing custom.sh manually
  triggers a large number of unrelated startup scripts).
- Missing `python3` must fail the install clearly and non-zero.
- Re-running the installer must remain idempotent (config and venv are
  preserved, not recreated/reset).
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
INSTALL_SH = REPO_ROOT / "scripts" / "install.sh"


def test_default_runtime_paths_use_single_userdata_namespace() -> None:
    content = INSTALL_SH.read_text()

    assert 'CACHE_ROOT="${CACHE_ROOT:-/userdata/romcloud/cache}"' in content
    assert "/userdata/romcloud-cache" not in content
    assert "/userdata/romcloud-source" not in content
    assert "/userdata/romcloud-saves-source" not in content
    assert 'rom_root = "/userdata/romcloud/source"' in content


def _run_install(
    env_overrides: dict[str, str], *, path: str | None = None, args: tuple[str, ...] = ()
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    # Production obtains these from the externally hosted release metadata.
    # Shell-installer tests supply protected build inputs directly so they
    # remain offline while exercising the same shared reconciler.
    env.setdefault("ROMCLOUD_GOOGLE_OAUTH_CLIENT_ID", "installer-test-client")
    env.setdefault("ROMCLOUD_GOOGLE_OAUTH_CLIENT_SECRET", "installer-test-secret")
    if path is not None:
        env["PATH"] = path
    env.update(env_overrides)
    return subprocess.run(
        ["bash", str(INSTALL_SH), *args],
        env=env,
        capture_output=True,
        text=True,
    )


@dataclass
class InstalledLayout:
    home: Path
    result: subprocess.CompletedProcess[str]

    @property
    def venv_python(self) -> Path:
        return self.home / "venv" / "bin" / "python"

    @property
    def bin_dir(self) -> Path:
        return self.home / "bin"


@pytest.fixture(scope="module")
def installed(tmp_path_factory: pytest.TempPathFactory) -> InstalledLayout:
    """Run the real installer once into an isolated prefix."""
    base = tmp_path_factory.mktemp("romcloud_install")
    # Mirrors the production layout (.../romcloud/bin) since the installer's
    # own idempotency check greps for the literal substring "romcloud/bin".
    home = base / "romcloud"
    result = _run_install(
        {
            "ROMCLOUD_HOME": str(home),
            "CACHE_ROOT": str(base / "cache"),
            "LOCAL_ROMS": str(base / "roms"),
        }
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return InstalledLayout(home=home, result=result)


def _read_version_info(home: Path) -> dict:
    return json.loads((home / "version.json").read_text(encoding="utf-8"))


def _installed_version(home: Path) -> str:
    result = subprocess.run(
        [str(home / "venv" / "bin" / "python"), "-c", "import romcloud; print(romcloud.__version__)"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


# ── venv creation & package install ──────────────────────────────────────────


def test_creates_persistent_venv(installed: InstalledLayout) -> None:
    assert installed.venv_python.exists()


def test_deploys_google_oauth_release_metadata(installed: InstalledLayout) -> None:
    path = installed.home / "runtime" / "google-oauth-client.json"
    payload = json.loads(path.read_text(encoding="utf-8"))

    assert payload == {
        "client_id": "installer-test-client",
        "client_secret": "installer-test-secret",
    }
    assert not (installed.home / "data" / "google-drive" / "token.json").exists()
    if os.name == "posix":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_romcloud_importable_inside_venv(installed: InstalledLayout) -> None:
    """ROMCloud must be installed with the venv's own python -m pip."""
    result = subprocess.run(
        [str(installed.venv_python), "-c", "import romcloud; print(romcloud.__file__)"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert "venv" in result.stdout


def test_does_not_install_into_system_python(installed: InstalledLayout) -> None:
    """Installing must never touch Batocera's system Python."""
    # Explicit minimal PATH so this checks the real system python3, not
    # whatever venv happens to be active in the invoking shell (e.g. the
    # repo's own dev .venv used to run this test suite).
    result = subprocess.run(
        ["python3", "-c", "import romcloud"],
        env={"PATH": "/usr/bin:/bin"},
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0, (
        "romcloud must not be importable from the system python3 — "
        "it belongs only in the venv."
    )


@pytest.fixture(scope="module")
def path_without_pip() -> str:
    """A PATH with every pip/pip3* executable removed, but python3 kept.

    Reproduces the confirmed Batocera 42 hardware finding: python3 exists,
    but there is no global pip3 at all.
    """
    fake_bin = Path(tempfile.mkdtemp(prefix="romcloud-fakebin-nopip-"))
    for real_dir in ("/usr/bin", "/bin"):
        real_path = Path(real_dir)
        if not real_path.is_dir():
            continue
        for entry in real_path.iterdir():
            if entry.name.startswith("pip"):
                continue
            link = fake_bin / entry.name
            if not link.exists():
                try:
                    link.symlink_to(entry)
                except OSError:
                    pass
    return str(fake_bin)


def test_install_succeeds_without_global_pip3(
    path_without_pip: str, tmp_path: Path
) -> None:
    """The installer must not require a global pip3 at all."""
    home = tmp_path / "romcloud"
    result = _run_install(
        {
            "ROMCLOUD_HOME": str(home),
            "CACHE_ROOT": str(tmp_path / "cache"),
            "LOCAL_ROMS": str(tmp_path / "roms"),
        },
        path=path_without_pip,
    )
    assert result.returncode == 0, result.stdout + result.stderr

    venv_python = home / "venv" / "bin" / "python"
    assert venv_python.exists()

    check = subprocess.run(
        [str(venv_python), "-c", "import romcloud"],
        capture_output=True,
        text=True,
    )
    assert check.returncode == 0, check.stderr


def test_bootstraps_pip_when_venv_lacks_it(tmp_path: Path) -> None:
    """If a pre-existing venv has no pip, the installer must bootstrap it via ensurepip."""
    home = tmp_path / "romcloud"
    venv_dir = home / "venv"
    venv_dir.parent.mkdir(parents=True, exist_ok=True)

    create = subprocess.run(
        ["python3", "-m", "venv", "--without-pip", str(venv_dir)],
        capture_output=True,
        text=True,
    )
    assert create.returncode == 0, create.stderr

    # Sanity: this venv genuinely has no pip yet.
    precheck = subprocess.run(
        [str(venv_dir / "bin" / "python"), "-m", "pip", "--version"],
        capture_output=True,
        text=True,
    )
    assert precheck.returncode != 0

    result = _run_install(
        {
            "ROMCLOUD_HOME": str(home),
            "CACHE_ROOT": str(tmp_path / "cache"),
            "LOCAL_ROMS": str(tmp_path / "roms"),
        }
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Bootstrapped pip inside the virtual environment" in result.stdout

    postcheck = subprocess.run(
        [str(venv_dir / "bin" / "python"), "-m", "pip", "--version"],
        capture_output=True,
        text=True,
    )
    assert postcheck.returncode == 0, postcheck.stderr

    check_romcloud = subprocess.run(
        [str(venv_dir / "bin" / "python"), "-c", "import romcloud"],
        capture_output=True,
        text=True,
    )
    assert check_romcloud.returncode == 0, check_romcloud.stderr


# ── wrapper generation ────────────────────────────────────────────────────────


def test_wrapper_execs_venv_python_and_preserves_at_sign(installed: InstalledLayout) -> None:
    """The CLI wrapper must exec the venv python and contain a literal "$@"."""
    wrapper = (installed.bin_dir / "romcloud").read_text()
    expected = f'exec "{installed.venv_python}" -m romcloud.cli.main "$@"'
    assert expected in wrapper


def test_romcloud_run_shebang_points_at_venv_python(installed: InstalledLayout) -> None:
    """romcloud-run's shebang must point directly at the venv's python."""
    first_line = (installed.bin_dir / "romcloud-run").read_text().splitlines()[0]
    assert first_line == f"#!{installed.venv_python}"


def test_wrapper_argv_passthrough(tmp_path: Path) -> None:
    """`romcloud refresh` must invoke `<venv python> -m romcloud.cli.main refresh`.

    Uses its own fresh install (rather than the shared `installed` fixture)
    so the venv's python executable can be safely replaced with a stub that
    records argv without affecting other tests.
    """
    home = tmp_path / "romcloud"
    result = _run_install(
        {
            "ROMCLOUD_HOME": str(home),
            "CACHE_ROOT": str(tmp_path / "cache"),
            "LOCAL_ROMS": str(tmp_path / "roms"),
        }
    )
    assert result.returncode == 0, result.stdout + result.stderr

    venv_python = home / "venv" / "bin" / "python"
    captured = tmp_path / "argv.txt"
    venv_python.unlink()
    venv_python.write_text(
        "#!/usr/bin/env bash\n" f'printf \'%s\\n\' "$@" > "{captured}"\n'
    )
    venv_python.chmod(venv_python.stat().st_mode | stat.S_IEXEC)

    run_result = subprocess.run(
        [str(home / "bin" / "romcloud"), "refresh", "--foo", "bar baz"],
        capture_output=True,
        text=True,
    )
    assert run_result.returncode == 0, run_result.stdout + run_result.stderr
    assert captured.read_text().splitlines() == [
        "-m",
        "romcloud.cli.main",
        "refresh",
        "--foo",
        "bar baz",
    ]


def test_custom_sh_is_never_touched(installed: InstalledLayout) -> None:
    """Regression: real-hardware testing showed sourcing custom.sh manually
    triggers a large number of unrelated Batocera/user-addon startup scripts
    (and a missing-file error) — it must be treated as foreign-owned state.
    The installer must never append to, source, or overwrite it."""
    content = INSTALL_SH.read_text()
    for line in content.splitlines():
        stripped = line.strip()
        if "custom.sh" not in stripped:
            continue
        assert stripped.startswith("#") or stripped.startswith('echo "'), (
            f"Unexpected non-comment/echo reference to custom.sh: {line!r}"
        )


def test_custom_sh_env_var_is_ignored_and_file_left_untouched(tmp_path: Path) -> None:
    """Even if a stray CUSTOM_SH env var is set (e.g. leftover from an old
    shell session), the installer must not read it or touch any file."""
    fake_custom_sh = tmp_path / "custom.sh"
    fake_custom_sh.write_text("# unrelated batocera/user-addon startup content\n")

    home = tmp_path / "romcloud"
    result = _run_install(
        {
            "ROMCLOUD_HOME": str(home),
            "CACHE_ROOT": str(tmp_path / "cache"),
            "LOCAL_ROMS": str(tmp_path / "roms"),
            "CUSTOM_SH": str(fake_custom_sh),
        }
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert fake_custom_sh.read_text() == "# unrelated batocera/user-addon startup content\n"


def test_cli_not_added_to_any_path(installed: InstalledLayout) -> None:
    """No PATH-modification mechanism is used at all."""
    assert "export PATH" not in installed.result.stdout


def test_summary_shows_full_cli_path_clearly(installed: InstalledLayout) -> None:
    """Since the CLI isn't on PATH, the installer must make the full path
    to invoke it obvious in its own output."""
    output = installed.result.stdout
    romcloud_bin = str(installed.bin_dir / "romcloud")
    assert romcloud_bin in output
    assert "was NOT added to PATH" in output
    assert "custom.sh" in output  # explains *why*, for a curious SSH user


def test_default_install_persists_stable_channel(installed: InstalledLayout) -> None:
    assert 'update_channel = "stable"' in (
        installed.home / "config" / "romcloud.toml"
    ).read_text(encoding="utf-8")
    assert _read_version_info(installed.home)["channel"] == "stable"


def test_develop_install_persists_channel_and_revision(tmp_path: Path) -> None:
    home = tmp_path / "romcloud"
    commit = "d" * 40
    result = _run_install(
        {
            "ROMCLOUD_HOME": str(home),
            "CACHE_ROOT": str(tmp_path / "cache"),
            "LOCAL_ROMS": str(tmp_path / "roms"),
            "ROMCLOUD_BUILD_COMMIT": commit,
        },
        args=("--channel", "develop"),
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert 'update_channel = "develop"' in (
        home / "config" / "romcloud.toml"
    ).read_text(encoding="utf-8")
    assert _read_version_info(home)["channel"] == "develop"
    assert _read_version_info(home)["commit"] == commit


def test_invalid_channel_fails_before_installation(tmp_path: Path) -> None:
    home = tmp_path / "romcloud"
    result = _run_install(
        {"ROMCLOUD_HOME": str(home)}, args=("--channel", "feature/foo")
    )

    assert result.returncode != 0
    assert "invalid channel" in result.stderr
    assert not home.exists()


# ── dependency checks ──────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def path_without_python() -> str:
    """A PATH directory tree with every python*/pip* executable removed."""
    fake_bin = Path(tempfile.mkdtemp(prefix="romcloud-fakebin-nopython-"))
    excluded_prefixes = ("python", "pip")
    for real_dir in ("/usr/bin", "/bin"):
        real_path = Path(real_dir)
        if not real_path.is_dir():
            continue
        for entry in real_path.iterdir():
            if entry.name.startswith(excluded_prefixes):
                continue
            link = fake_bin / entry.name
            if not link.exists():
                try:
                    link.symlink_to(entry)
                except OSError:
                    pass
    return str(fake_bin)


def test_missing_python3_fails_clearly(path_without_python: str, tmp_path: Path) -> None:
    """Install must fail non-zero and must not claim success when python3 is absent."""
    home = tmp_path / "home"
    result = _run_install(
        {
            "ROMCLOUD_HOME": str(home),
            "CACHE_ROOT": str(tmp_path / "cache"),
            "LOCAL_ROMS": str(tmp_path / "roms"),
        },
        path=path_without_python,
    )
    assert result.returncode != 0
    assert "missing required dependency: python3" in result.stderr
    assert "installed successfully" not in result.stdout
    assert not home.exists()


def test_no_global_pip3_dependency_check(installed: InstalledLayout) -> None:
    """The installer's dependency check must never mention pip3 at all."""
    assert "pip3" not in installed.result.stdout
    assert "pip3" not in installed.result.stderr


# ── idempotency ────────────────────────────────────────────────────────────────


def test_rerun_is_idempotent(installed: InstalledLayout) -> None:
    """Re-running the installer must not touch existing config or recreate
    the venv."""
    config_file = installed.home / "config" / "romcloud.toml"
    config_file.write_text("# user customized\n")

    venv_marker = installed.home / "venv" / "romcloud_test_marker"
    venv_marker.write_text("still here")

    result = _run_install(
        {
            "ROMCLOUD_HOME": str(installed.home),
            "CACHE_ROOT": str(installed.home.parent / "cache"),
            "LOCAL_ROMS": str(installed.home.parent / "roms"),
        }
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Virtual environment already exists" in result.stdout
    assert config_file.read_text() == "# user customized\n"
    assert venv_marker.exists(), "existing venv must be reused, not recreated"


@pytest.fixture(scope="module")
def path_without_git() -> str:
    """A PATH with git removed, while keeping python3 available."""
    fake_bin = Path(tempfile.mkdtemp(prefix="romcloud-fakebin-nogit-"))
    for real_dir in ("/usr/bin", "/bin"):
        real_path = Path(real_dir)
        if not real_path.is_dir():
            continue
        for entry in real_path.iterdir():
            if entry.name == "git":
                continue
            link = fake_bin / entry.name
            if not link.exists():
                try:
                    link.symlink_to(entry)
                except OSError:
                    pass
    return str(fake_bin)


def test_source_archive_install_without_git_keeps_commit_unknown(
    path_without_git: str, tmp_path: Path
) -> None:
    home = tmp_path / "romcloud"
    result = _run_install(
        {
            "ROMCLOUD_HOME": str(home),
            "CACHE_ROOT": str(tmp_path / "cache"),
            "LOCAL_ROMS": str(tmp_path / "roms"),
        },
        path=path_without_git,
    )
    assert result.returncode == 0, result.stdout + result.stderr

    info = _read_version_info(home)
    assert info["version"] == _installed_version(home)
    assert info["commit"] is None
    assert info["commit_short"] is None
    assert info["source"] == "installer:unknown"


def test_reinstall_preserves_existing_commit_metadata_without_git(
    path_without_git: str, tmp_path: Path
) -> None:
    home = tmp_path / "romcloud"
    explicit_commit = "a" * 40

    first = _run_install(
        {
            "ROMCLOUD_HOME": str(home),
            "CACHE_ROOT": str(tmp_path / "cache"),
            "LOCAL_ROMS": str(tmp_path / "roms"),
            "ROMCLOUD_BUILD_COMMIT": explicit_commit,
        },
        path=path_without_git,
    )
    assert first.returncode == 0, first.stdout + first.stderr
    assert _read_version_info(home)["commit"] == explicit_commit

    second = _run_install(
        {
            "ROMCLOUD_HOME": str(home),
            "CACHE_ROOT": str(tmp_path / "cache"),
            "LOCAL_ROMS": str(tmp_path / "roms"),
        },
        path=path_without_git,
    )
    assert second.returncode == 0, second.stdout + second.stderr

    info = _read_version_info(home)
    assert info["commit"] == explicit_commit
    assert info["commit_short"] == explicit_commit[:12]
    assert info["source"] == "installer:preserved"


# ── graphical Ports UI (runs under Batocera's system Python, not the venv) ───
#
# Real hardware fact: Batocera 42 ships pygame 2.5.2 / SDL 2.32.8 in its OWN
# system Python. ROMCloud's venv must never gain a pygame dependency (no pip
# install, no --system-site-packages) — the graphical Ports app is instead
# copied (not pip-installed) to its own directory and run directly with the
# detected system Python. These tests use small fake "system python" stub
# scripts so the outcome never depends on whether pygame actually happens to
# be installed on the machine running the test suite.


def _write_fake_system_python(tmp_path: Path, *, has_pygame: bool) -> Path:
    stub = tmp_path / ("fake-system-python-with-pygame" if has_pygame else "fake-system-python-no-pygame")
    exit_code = "0" if has_pygame else "1"
    stub.write_text(
        "#!/bin/bash\n"
        'if [[ "$1" == "-c" && "$2" == "import pygame" ]]; then\n'
        f"    exit {exit_code}\n"
        "fi\n"
        'exec /usr/bin/python3 "$@"\n'
    )
    stub.chmod(stub.stat().st_mode | stat.S_IEXEC)
    return stub


class TestGraphicalPortsUiInstall:
    def test_installed_when_system_python_has_pygame(self, tmp_path: Path) -> None:
        home = tmp_path / "romcloud"
        ports_dir = tmp_path / "ports"
        ports_dir.mkdir()
        fake_python = _write_fake_system_python(tmp_path, has_pygame=True)

        result = _run_install(
            {
                "ROMCLOUD_HOME": str(home),
                "CACHE_ROOT": str(tmp_path / "cache"),
                "LOCAL_ROMS": str(tmp_path / "roms"),
                "ROMCLOUD_SYSTEM_PYTHON": str(fake_python),
                "ROMCLOUD_PORTS_DIR": str(ports_dir),
            }
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert "Wrote graphical Ports wrapper" in result.stdout

        wrapper = home / "bin" / "romcloud-ports"
        assert wrapper.exists()
        content = wrapper.read_text()
        assert f'exec "{fake_python}" -m ports_gfx "$@"' in content
        assert f'export PYTHONPATH="{home}/ports-gfx' in content
        assert f'export ROMCLOUD_BIN="{home}/bin/romcloud"' in content

        assert (home / "ports-gfx" / "ports_gfx" / "client.py").exists()
        assert (home / "ports-gfx" / "ports_gfx" / "app.py").exists()

        ports_script = ports_dir / "ROMCloud.sh"
        assert ports_script.exists()
        assert f'exec "{wrapper}" "$@"' in ports_script.read_text()

    def test_skipped_cleanly_when_system_python_lacks_pygame(self, tmp_path: Path) -> None:
        home = tmp_path / "romcloud"
        fake_python = _write_fake_system_python(tmp_path, has_pygame=False)

        result = _run_install(
            {
                "ROMCLOUD_HOME": str(home),
                "CACHE_ROOT": str(tmp_path / "cache"),
                "LOCAL_ROMS": str(tmp_path / "roms"),
                "ROMCLOUD_SYSTEM_PYTHON": str(fake_python),
            }
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert "Skipping graphical Ports UI" in result.stdout
        assert not (home / "bin" / "romcloud-ports").exists()
        assert not (home / "ports-gfx").exists()

    def test_ports_dir_missing_skips_launcher_but_not_wrapper(self, tmp_path: Path) -> None:
        home = tmp_path / "romcloud"
        fake_python = _write_fake_system_python(tmp_path, has_pygame=True)
        missing_ports_dir = tmp_path / "does-not-exist" / "ports"

        result = _run_install(
            {
                "ROMCLOUD_HOME": str(home),
                "CACHE_ROOT": str(tmp_path / "cache"),
                "LOCAL_ROMS": str(tmp_path / "roms"),
                "ROMCLOUD_SYSTEM_PYTHON": str(fake_python),
                "ROMCLOUD_PORTS_DIR": str(missing_ports_dir),
            }
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert (home / "bin" / "romcloud-ports").exists()
        assert not missing_ports_dir.exists()
        assert "skipped Batocera Port entry" in result.stdout

    def test_ports_gfx_never_imports_romcloud_package(self) -> None:
        """Enforced boundary: the copied source tree must never import the
        `romcloud` package — it must be reachable purely via `romcloud
        uidata <action>` subprocess calls."""
        ports_gfx_dir = REPO_ROOT / "ports_gfx"
        for py_file in ports_gfx_dir.glob("*.py"):
            for line in py_file.read_text().splitlines():
                stripped = line.strip()
                assert not stripped.startswith("import romcloud"), f"{py_file} must not import romcloud"
                assert not stripped.startswith("from romcloud"), f"{py_file} must not import romcloud"

    def test_reinstall_is_idempotent_for_ports_gfx(self, tmp_path: Path) -> None:
        home = tmp_path / "romcloud"
        fake_python = _write_fake_system_python(tmp_path, has_pygame=True)
        env = {
            "ROMCLOUD_HOME": str(home),
            "CACHE_ROOT": str(tmp_path / "cache"),
            "LOCAL_ROMS": str(tmp_path / "roms"),
            "ROMCLOUD_SYSTEM_PYTHON": str(fake_python),
        }

        first = _run_install(env)
        assert first.returncode == 0, first.stdout + first.stderr

        second = _run_install(env)
        assert second.returncode == 0, second.stdout + second.stderr
        assert (home / "ports-gfx" / "ports_gfx" / "client.py").exists()
