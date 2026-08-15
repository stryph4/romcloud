"""Persist activation state for ROMCloud's owned Batocera startup service."""

from __future__ import annotations

import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

STATE_FILENAME = "startup-integration.json"
REBOOT_COMMAND = "/usr/bin/batocera-es-swissknife"
BOOT_ID_PATH = Path("/proc/sys/kernel/random/boot_id")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _read_state(path: str | Path) -> dict[str, object]:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _write_state(path: str | Path, value: dict[str, object]) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.tmp")
    temporary.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
    try:
        temporary.chmod(0o600)
    except OSError:
        pass
    temporary.replace(target)
    return target


def state_path(romcloud_home: str | Path) -> Path:
    return Path(romcloud_home) / "state" / STATE_FILENAME


def _failure_status(detail: str, failed_at: object) -> dict[str, object]:
    return {
        "startup_restart_required": False,
        "startup_integration_activated": False,
        "startup_manager_startup_failed": True,
        "startup_manager_failure_at": failed_at,
        "startup_manager_failure_message": (
            "Automatic Library Browser startup failed after reboot: "
            f"{detail}. Retry with `batocera-services start romcloud_mount`; "
            "another reboot is not required."
        ),
    }


def activation_status(
    path: str | Path, *, boot_id_path: Path = BOOT_ID_PATH
) -> dict[str, object]:
    value = _read_state(path)
    changed_boot_id = value.get("changed_boot_id")
    attempt_boot_id = value.get("last_attempt_boot_id")
    current_boot_id = _read_boot_id(boot_id_path)
    failed = value.get("last_attempt_status") == "failed"
    failure_replaces_restart = bool(
        failed
        and (
            not value.get("restart_required")
            or (
                changed_boot_id
                and attempt_boot_id
                and changed_boot_id != attempt_boot_id
            )
        )
    )
    if failure_replaces_restart:
        detail = str(value.get("last_attempt_detail") or "Unknown startup error")
        return _failure_status(detail, value.get("last_attempt_at"))
    if not value.get("restart_required"):
        return {"startup_restart_required": False}
    later_boot = bool(
        changed_boot_id
        and current_boot_id
        and changed_boot_id != current_boot_id
    )
    if later_boot and attempt_boot_id != current_boot_id:
        return _failure_status(
            "the Batocera service did not record a manager startup attempt",
            None,
        )
    if later_boot and value.get("last_attempt_status") == "starting":
        return {
            "startup_restart_required": False,
            "startup_integration_activated": False,
            "startup_manager_starting": True,
        }
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
    existing = _read_state(target)
    current_boot_id = _read_boot_id(boot_id_path)
    if existing.get("restart_required"):
        return target
    return _write_state(
        target,
        {
            "version": 2,
            "restart_required": True,
            "changed_at": _now(),
            "changed_boot_id": current_boot_id,
        },
    )


def record_startup_attempt(
    path: str | Path, *, boot_id_path: Path = BOOT_ID_PATH
) -> None:
    """Persist proof that Batocera invoked the manager boot command."""
    value = _read_state(path)
    if not value:
        value = {"version": 2, "restart_required": False}
    value.update(
        {
            "last_attempt_boot_id": _read_boot_id(boot_id_path),
            "last_attempt_at": _now(),
            "last_attempt_status": "starting",
        }
    )
    value.pop("last_attempt_detail", None)
    _write_state(path, value)


def record_startup_failure(
    path: str | Path,
    detail: str,
    *,
    boot_id_path: Path = BOOT_ID_PATH,
) -> None:
    """Record a bounded boot-start failure without re-arming the marker."""
    value = _read_state(path)
    if not value:
        value = {"version": 2, "restart_required": False}
    value.update(
        {
            "last_attempt_boot_id": _read_boot_id(boot_id_path),
            "last_attempt_at": _now(),
            "last_attempt_status": "failed",
            "last_attempt_detail": detail,
        }
    )
    _write_state(path, value)


def mark_activated(
    path: str | Path, *, boot_id_path: Path = BOOT_ID_PATH
) -> bool:
    """Clear pending state only after a successful start on a later boot."""
    target = Path(path)
    value = _read_state(target)
    if not value:
        return False
    if not value.get("restart_required"):
        target.unlink(missing_ok=True)
        return False
    changed_boot_id = value.get("changed_boot_id")
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
