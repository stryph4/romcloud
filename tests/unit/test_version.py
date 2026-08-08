"""Tests for the single canonical version source (requirement: no duplicate
hardcoded version constants across the codebase)."""

from __future__ import annotations

import re
import tomllib
from importlib.metadata import version as pkg_version
from pathlib import Path

import romcloud


def _pyproject_version() -> str:
    root = Path(__file__).resolve().parents[2]
    with (root / "pyproject.toml").open("rb") as fh:
        data = tomllib.load(fh)
    return data["project"]["version"]


class TestSingleCanonicalVersionSource:
    def test_dunder_version_matches_installed_package_metadata(self):
        assert romcloud.__version__ == pkg_version("romcloud")

    def test_dunder_version_matches_pyproject(self):
        assert romcloud.__version__ == _pyproject_version()

    def test_init_module_does_not_hardcode_a_version_string(self):
        """`__init__.py` must derive `__version__` dynamically, not
        hardcode a second literal semver string that could drift from
        pyproject.toml. The uninstalled-package fallback sentinel
        ("0.0.0+unknown") is not a real version and is exempt."""
        source = Path(romcloud.__file__).read_text(encoding="utf-8")
        matches = re.findall(r'__version__\s*=\s*["\'][^"\']+["\']', source)
        assert all("0.0.0+unknown" in m for m in matches)

    def test_version_is_semver_shaped(self):
        assert re.match(r"^\d+\.\d+\.\d+", romcloud.__version__)
