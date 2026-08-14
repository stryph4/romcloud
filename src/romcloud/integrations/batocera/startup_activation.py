"""Persist activation state for ROMCloud's owned Batocera startup service."""

from __future__ import annotations

import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

STATE_FILENAME = "startup-integration.json"
REBOOT_COMMAND = "/usr/bin/batocera-es-swissknife"
BOOT_ID_PATH = Path("/proc/sys/kernel/random/boot_id")


def state_path(romcloud_home: str | Path) -> Path:
    return Path(romcloud_home) / "state" / STATE_FILENAME


def activation_status(path: str | Path) -> dict[str, object]:
    target = Path(path)
    try:
        value = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"startup_restart_required": False}
    if not isinstance(value, dict) or not value.get("restart_required"):
        return {"startup_restart_required": False}
    return {
        "startup_restart_required": True,
        "startup_integration_activated": False,
        "startup_integration_changed_at": value.get("changed_at"),
        "startup_restart_message": (
            "Restart Batocera before automatic Library Browser availability "
            "at future boots is considered active. The manager remains "
            "available in this session."
        ),
    }


def _read_boot_id(path: Path) -> str | None:
    try:
        value = path.read_text(encoding="ascii").strip()
    except OSError:
        return None
    return value or None


def mark_restart_required(
    path: str | Path, *, boot_id_path: Path = BOOT_ID_PATH
) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.tmp")
    temporary.write_text(
        json.dumps(
            {
                "version": 1,
                "restart_required": True,
                "changed_at": datetime.now(timezone.utc).isoformat(),
                "changed_boot_id": _read_boot_id(boot_id_path),
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    try:
        temporary.chmod(0o600)
    except OSError:
        pass
    temporary.replace(target)
    return target


def mark_activated(
    path: str | Path, *, boot_id_path: Path = BOOT_ID_PATH
) -> bool:
    """Clear pending state only after a successful start on a later boot."""
    target = Path(path)
    try:
        value = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    changed_boot_id = value.get("changed_boot_id") if isinstance(value, dict) else None
    current_boot_id = _read_boot_id(boot_id_path)
    if not changed_boot_id or not current_boot_id or changed_boot_id == current_boot_id:
        return False
    target.unlink(missing_ok=True)
    return True


def request_reboot(*, popen=subprocess.Popen) -> dict[str, object]:
    """Ask Batocera to reboot without claiming activation before next boot."""
    try:
        popen(
            [REBOOT_COMMAND, "--reboot"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            close_fds=True,
        )
    except OSError as exc:
        raise RuntimeError("Batocera restart is unavailable on this system.") from exc
    return {"restart_requested": True, "startup_restart_required": True}
