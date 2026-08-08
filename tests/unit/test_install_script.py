"""Tests for scripts/install.sh.

Focused on the shell-generation issues that matter most for a real Batocera
install: the generated CLI wrapper must preserve runtime `"$@"` (not expand it
during installation), the `custom.sh` PATH line must preserve runtime `$PATH`,
missing `python3`/`pip3` must fail the install clearly, and re-running the
installer must remain idempotent.
"""

from __future__ import annotations

import os
import stat
import subprocess
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


def test_wrapper_preserves_at_sign_literally(installed: InstalledLayout) -> None:
    """The generated wrapper must contain a literal "$@", not an expansion of it."""
    wrapper = (installed.home / "bin" / "romcloud").read_text()
    assert 'exec python3 -m romcloud.cli.main "$@"' in wrapper


def test_wrapper_argv_passthrough(installed: InstalledLayout, tmp_path: Path) -> None:
    """`romcloud refresh` must invoke `python3 -m romcloud.cli.main refresh`."""
    captured = tmp_path / "argv.txt"
    stub_bin = tmp_path / "stub_bin"
    stub_bin.mkdir()
    stub_python3 = stub_bin / "python3"
    stub_python3.write_text(
        "#!/usr/bin/env bash\n"
        f'printf \'%s\\n\' "$@" > "{captured}"\n'
    )
    stub_python3.chmod(stub_python3.stat().st_mode | stat.S_IEXEC)

    env = os.environ.copy()
    env["PATH"] = f"{stub_bin}:{env['PATH']}"

    result = subprocess.run(
        [str(installed.home / "bin" / "romcloud"), "refresh", "--foo", "bar baz"],
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
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
    expected = f'export PATH="{installed.home / "bin"}:$PATH"'
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
    assert result.stdout == f"{installed.home / 'bin'}:/some/other/dir"


@pytest.fixture(scope="module")
def path_without_python() -> str:
    """A PATH directory tree with every executable except python3/pip3/python symlinked in."""
    import tempfile

    fake_bin = Path(tempfile.mkdtemp(prefix="romcloud-fakebin-"))
    excluded_prefixes = ("python3", "python", "pip3", "pip")
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


def test_missing_python_dependencies_fails_clearly(path_without_python: str, tmp_path: Path) -> None:
    """Install must fail non-zero and must not claim success when python3/pip3 are absent."""
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
    assert "missing required dependencies" in result.stderr
    assert "installed successfully" not in result.stdout
    assert not home.exists()


def test_rerun_is_idempotent(installed: InstalledLayout) -> None:
    """Re-running the installer must not touch existing config or duplicate the PATH line."""
    config_file = installed.home / "config" / "romcloud.toml"
    config_file.write_text("# user customized\n")

    result = _run_install(
        {
            "ROMCLOUD_HOME": str(installed.home),
            "CACHE_ROOT": str(installed.home.parent / "cache"),
            "LOCAL_ROMS": str(installed.home.parent / "roms"),
            "CUSTOM_SH": str(installed.custom_sh),
        }
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert config_file.read_text() == "# user customized\n"
    assert installed.custom_sh.read_text().count("romcloud/bin") == 1
