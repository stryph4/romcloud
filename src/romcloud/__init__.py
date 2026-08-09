"""ROMCloud — browse and launch ROMs from remote/external sources on Batocera."""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _pkg_version

# Single canonical version source: the `[project] version` field in
# pyproject.toml, read via the installed package's own metadata — the same
# source `click.version_option(package_name="romcloud")` already uses for
# `romcloud --version`. No second hardcoded version string is maintained
# here; `version.json` (see romcloud.lifecycle.update) is a separate,
# generated build/commit *record*, not a second source of truth.
try:
    __version__ = _pkg_version("romcloud")
except PackageNotFoundError:  # pragma: no cover - only when running uninstalled
    __version__ = "0.0.0+unknown"
