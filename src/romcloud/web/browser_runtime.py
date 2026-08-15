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


def staging_version_path(data_path: str | Path, version: str) -> Path:
    if not re.fullmatch(r"[A-Za-z0-9._-]+", version):
        raise ValueError("Managed browser version contains unsafe characters.")
    return runtime_root(data_path) / "staging" / version


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


def activate_staged_runtime(
    data_path: str | Path,
    *,
    version: str,
    executable: str,
    smoke_test,
) -> dict[str, object]:
    """Smoke-test and atomically select an already extracted owned stage.

    Artifact download and extraction intentionally remain unavailable while the
    Batocera dependency/sandbox gate is open. This boundary still makes future
    activation transactional and independently testable.
    """

    relative = Path(executable)
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError("Managed browser executable must remain inside its version.")
    root = runtime_root(data_path)
    stage = staging_version_path(data_path, version)
    staged_executable = stage / relative
    if not staged_executable.is_file() or (
        os.name != "nt" and not os.access(staged_executable, os.X_OK)
    ):
        raise RuntimeError("Staged managed browser executable is missing or not executable.")
    probe = smoke_test(str(staged_executable))
    if not isinstance(probe, dict) or not probe.get("compatible"):
        reason = probe.get("reason", "unknown smoke-test failure") if isinstance(probe, dict) else "invalid smoke-test result"
        raise RuntimeError(f"Staged managed browser failed its smoke test: {reason}")

    destination = root / "versions" / version
    if destination.exists():
        raise RuntimeError(f"Managed browser version {version} already exists.")
    destination.parent.mkdir(parents=True, exist_ok=True)
    manifest = current_manifest_path(data_path)
    temporary = manifest.with_name(f".{manifest.name}.tmp")
    stage.rename(destination)
    try:
        payload = {"version": version, "executable": relative.as_posix()}
        temporary.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        temporary.replace(manifest)
    except Exception:
        temporary.unlink(missing_ok=True)
        stage.parent.mkdir(parents=True, exist_ok=True)
        destination.rename(stage)
        raise
    return {"installed": True, "version": version, "executable": str(destination / relative)}


def request_managed_install(*, accepted: bool) -> dict[str, object]:
    """Represent the disabled optional-install decision without mutating disk."""

    if not accepted:
        return {"installed": False, "declined": True, "candidate": CANDIDATE}
    if not CANDIDATE["installation_enabled"]:
        raise RuntimeError(str(CANDIDATE["blocked_reason"]))
    raise RuntimeError("Managed browser artifact metadata is unavailable.")


def remove_managed_runtime(data_path: str | Path) -> bool:
    """Remove only ROMCloud's browser directory; external browsers are untouched."""
    root = runtime_root(data_path)
    if not root.exists():
        return False
    shutil.rmtree(root)
    return True
