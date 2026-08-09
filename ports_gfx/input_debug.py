"""Temporary raw input diagnostics for the graphical Ports UI.

This module is intentionally tiny and self-contained so it can be removed
after hardware validation without touching the actual input translation
layer. It only observes raw pygame input events and writes a per-launch log
under Batocera's ROMCloud state directory.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional, Sequence

from ports_gfx.controller import ControllerSnapshot

DEBUG_ENV_VAR = "ROMCLOUD_PORTS_GFX_INPUT_DEBUG"
DEBUG_LOG_PATH = Path("/userdata/system/romcloud/logs/controller-debug.log")

_EVENT_NAMES = (
    "JOYDEVICEADDED",
    "JOYDEVICEREMOVED",
    "JOYBUTTONDOWN",
    "JOYBUTTONUP",
    "JOYAXISMOTION",
    "JOYHATMOTION",
    "CONTROLLERDEVICEADDED",
    "CONTROLLERDEVICEREMOVED",
    "CONTROLLERBUTTONDOWN",
    "CONTROLLERBUTTONUP",
    "CONTROLLERAXISMOTION",
)


def is_enabled_from_env(environ: Optional[dict[str, str]] = None) -> bool:
    if environ is None:
        from os import environ as os_environ

        environ = os_environ
    value = environ.get(DEBUG_ENV_VAR, "")
    return value not in ("", "0", "false", "False", "no", "NO")


class InputDebugLogger:
    """Best-effort raw input logger for controller diagnostics.

    Logging is disabled unless the environment variable
    ``ROMCLOUD_PORTS_GFX_INPUT_DEBUG`` is set to a truthy value. When
    enabled, the log file is truncated on startup so each launch produces a
    clean capture that is easy to inspect.
    """

    def __init__(self, pygame, *, enabled: Optional[bool] = None, log_path: Path = DEBUG_LOG_PATH) -> None:  # noqa: ANN001
        self._pygame = pygame
        self._log_path = log_path
        self._enabled = is_enabled_from_env() if enabled is None else enabled
        self._stream = None
        self._event_names = self._build_event_names()
        self._controller_button_names = self._build_constant_names("CONTROLLER_BUTTON_")
        self._controller_axis_names = self._build_constant_names("CONTROLLER_AXIS_")

        if not self._enabled:
            return

        try:
            self._log_path.parent.mkdir(parents=True, exist_ok=True)
            self._log_path.write_text("", encoding="utf-8")
            self._stream = self._log_path.open("a", encoding="utf-8", buffering=1)
        except Exception:  # noqa: BLE001 - diagnostics must never break the UI
            self._enabled = False
            self._stream = None

    def close(self) -> None:
        if self._stream is None:
            return
        try:
            self._stream.close()
        finally:
            self._stream = None

    def log_startup(self, *, joystick_count: int, controller_module_present: bool, snapshots: Sequence[ControllerSnapshot]) -> None:
        if not self._enabled:
            return
        self._write_line(
            "startup "
            f"joystick_count={joystick_count} "
            f"controller_module_present={controller_module_present} "
            f"controllers={len(snapshots)}"
        )
        if not snapshots:
            self._write_line("startup no_controllers_detected")
            return

        for snapshot in snapshots:
            identity = snapshot.identity
            self._write_line(
                "startup controller "
                f"instance_id={snapshot.instance_id} "
                f"name={identity.name!r} "
                f"guid={identity.guid!r} "
                f"game_controller={snapshot.is_game_controller} "
                f"custom_mapping={snapshot.using_custom_mapping}"
            )

    def log_event(self, event) -> None:  # noqa: ANN001
        if not self._enabled:
            return

        event_name = self._event_name(getattr(event, "type", None))
        if not event_name.startswith(("JOY", "CONTROLLER")):
            return

        fields = [f"event={event_name}"]

        for name in ("device_index", "instance_id", "which", "button", "axis", "hat"):
            value = getattr(event, name, None)
            if value is not None:
                fields.append(f"{name}={value}")

        value = getattr(event, "value", None)
        if value is not None:
            fields.append(f"value={value}")

        if event_name in {"CONTROLLERBUTTONDOWN", "CONTROLLERBUTTONUP"}:
            button = getattr(event, "button", None)
            button_name = self._controller_button_names.get(button)
            if button_name is not None:
                fields.append(f"logical_button={button_name}")

        if event_name == "CONTROLLERAXISMOTION":
            axis = getattr(event, "axis", None)
            axis_name = self._controller_axis_names.get(axis)
            if axis_name is not None:
                fields.append(f"logical_axis={axis_name}")

        self._write_line(" ".join(fields))

    def _build_event_names(self) -> dict[int, str]:
        return self._build_named_constants(_EVENT_NAMES)

    def _build_constant_names(self, prefix: str) -> dict[int, str]:
        pygame = self._pygame
        result: dict[int, str] = {}
        for name in dir(pygame):
            if not name.startswith(prefix):
                continue
            value = getattr(pygame, name, None)
            if isinstance(value, int):
                result[value] = name
        return result

    def _build_named_constants(self, names: Iterable[str]) -> dict[int, str]:
        pygame = self._pygame
        result: dict[int, str] = {}
        for name in names:
            value = getattr(pygame, name, None)
            if isinstance(value, int):
                result[value] = name
        return result

    def _event_name(self, event_type: object) -> str:
        if isinstance(event_type, int):
            return self._event_names.get(event_type, f"event_{event_type}")
        return f"event_{event_type}"

    def _write_line(self, line: str) -> None:
        if self._stream is None:
            return
        try:
            stamp = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
            self._stream.write(f"{stamp} {line}\n")
        except Exception:  # noqa: BLE001 - best effort only
            self.close()
