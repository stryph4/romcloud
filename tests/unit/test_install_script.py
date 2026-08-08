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
- The `custom.sh` PATH line must preserve runtime `$PATH`.
- Missing `python3` must fail the install clearly and non-zero.
- Re-running the installer must remain idempotent (config, venv, and the
  custom.sh PATH entry are all preserved/deduplicated).
"""

from __future__ import annotations

import os
import stat
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
INSTALL_SH = REPO_ROOT / "scripts" / "install.sh"


def _run_install(
    env_overrides: dict[str, str], *, path: str | None = None
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    if path is not None:
        env["PATH"] = path
    env.update(env_overrides)
    return subprocess.run(
        ["bash", str(INSTALL_SH)],
        env=env,
        capture_output=True,
        text=True,
    )


@dataclass
class InstalledLayout:
    home: Path
    custom_sh: Path
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
    custom_sh = base / "custom.sh"
    result = _run_install(
        {
            "ROMCLOUD_HOME": str(home),
            "CACHE_ROOT": str(base / "cache"),
            "LOCAL_ROMS": str(base / "roms"),
            "CUSTOM_SH": str(custom_sh),
        }
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return InstalledLayout(home=home, custom_sh=custom_sh, result=result)


# ── venv creation & package install ──────────────────────────────────────────


def test_creates_persistent_venv(installed: InstalledLayout) -> None:
    assert installed.venv_python.exists()


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
            "CUSTOM_SH": str(tmp_path / "custom.sh"),
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
            "CUSTOM_SH": str(tmp_path / "custom.sh"),
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
            "CUSTOM_SH": str(tmp_path / "custom.sh"),
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


def test_custom_sh_path_line_preserves_runtime_path(installed: InstalledLayout) -> None:
    """The custom.sh PATH line must contain a literal $PATH, not the installer's PATH."""
    content = installed.custom_sh.read_text()
    expected = f'export PATH="{installed.bin_dir}:$PATH"'
    assert expected in content


def test_custom_sh_path_line_expands_at_runtime(installed: InstalledLayout, tmp_path: Path) -> None:
    """Sourcing custom.sh must prepend the bin dir to whatever PATH is active then."""
    result = subprocess.run(
        [
            "bash",
            "-c",
            f'export PATH=/some/other/dir; source "{installed.custom_sh}"; printf "%s" "$PATH"',
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == f"{installed.bin_dir}:/some/other/dir"


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
            "CUSTOM_SH": str(tmp_path / "custom.sh"),
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
    """Re-running the installer must not touch existing config, recreate the
    venv, or duplicate the PATH line."""
    config_file = installed.home / "config" / "romcloud.toml"
    config_file.write_text("# user customized\n")

    venv_marker = installed.home / "venv" / "romcloud_test_marker"
    venv_marker.write_text("still here")

    result = _run_install(
        {
            "ROMCLOUD_HOME": str(installed.home),
            "CACHE_ROOT": str(installed.home.parent / "cache"),
            "LOCAL_ROMS": str(installed.home.parent / "roms"),
            "CUSTOM_SH": str(installed.custom_sh),
        }
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Virtual environment already exists" in result.stdout
    assert config_file.read_text() == "# user customized\n"
    assert venv_marker.exists(), "existing venv must be reused, not recreated"
    assert installed.custom_sh.read_text().count("romcloud/bin") == 1
