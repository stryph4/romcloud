"""ROMCloud-owned local browser runtime layout and disabled candidate metadata.

Chrome for Testing installation remains deliberately disabled until its
Batocera dependencies and Chromium sandbox are validated on real hardware.
This module establishes only the independently-owned versioned lifecycle.
"""

from __future__ import annotations

import json
import os
import re
import shutil
from pathlib import Path

CANDIDATE = {
    "name": "Chrome for Testing Stable",
    "architecture": "x86_64",
    "download_mib": 186,
    "installed_mib": 400,
    "installation_enabled": False,
    "blocked_reason": (
        "Automatic installation is disabled pending Batocera system-library "
        "and secure Chromium sandbox validation."
    ),
}


def runtime_root(data_path: str | Path) -> Path:
    return Path(data_path).parent / "browser"


def current_manifest_path(data_path: str | Path) -> Path:
    return runtime_root(data_path) / "current.json"


def managed_browser(data_path: str | Path) -> str | None:
    try:
        value = json.loads(current_manifest_path(data_path).read_text(encoding="utf-8"))
        version = str(value["version"])
        relative = Path(str(value["executable"]))
    except (OSError, ValueError, KeyError, TypeError):
        return None
    if not re.fullmatch(r"[A-Za-z0-9._-]+", version):
        return None
    if relative.is_absolute() or ".." in relative.parts:
        return None
    version_root = (runtime_root(data_path) / "versions" / version).resolve()
    executable = (version_root / relative).resolve()
    try:
        executable.relative_to(version_root)
    except ValueError:
        return None
    executable_ok = executable.is_file() and (
        os.name == "nt" or bool(executable.stat().st_mode & 0o111)
    )
    return str(executable) if executable_ok else None


def runtime_status(data_path: str | Path) -> dict[str, object]:
    executable = managed_browser(data_path)
    version = None
    if executable:
        try:
            version = json.loads(current_manifest_path(data_path).read_text(encoding="utf-8")).get("version")
        except (OSError, ValueError):
            pass
    return {"installed": bool(executable), "version": version, "executable": executable, "candidate": CANDIDATE}


def remove_managed_runtime(data_path: str | Path) -> bool:
    """Remove only ROMCloud's browser directory; external browsers are untouched."""
    root = runtime_root(data_path)
    if not root.exists():
        return False
    shutil.rmtree(root)
    return True
