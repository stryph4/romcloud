from __future__ import annotations

import json

from ports_gfx.actions import Action
from ports_gfx.app import (
    LIBRARY_MANAGER_ACTION,
    _handle_library_manager_event,
    _library_manager_body_lines,
    menu_categories_for_state,
)
from ports_gfx.input_manager import InputEvent
from ports_gfx.library_manager_screen import FAILED, READY, STARTING, LibraryManagerScreenState


class _Stream:
    def __init__(self, text: str) -> None:
        self.lines = iter(text.splitlines(keepends=True) + [""])

    def readline(self) -> str:
        return next(self.lines, "")

    def close(self) -> None:
        pass


class _Process:
    def __init__(self, payload: dict):
        self.stdout = _Stream(json.dumps(payload) + "\n")
        self.stderr = _Stream("")
        self.stdin = None
        self.returncode = 0 if payload.get("ok") else 1

    def poll(self):
        return self.returncode

    def terminate(self) -> None:
        self.returncode = -15


def _drain(state: LibraryManagerScreenState) -> None:
    for _ in range(50):
        state.poll()
        if state._runner is None:  # noqa: SLF001
            return


def test_library_menu_exposes_manager_in_every_operating_mode() -> None:
    for mode in ("connected", "cache", "offline"):
        state = {
            "operating_mode": mode,
            "capabilities": {"catalog_refresh": mode != "offline"},
        }
        items = menu_categories_for_state(state)["Library"]
        manager = next(item for item in items if item.action == LIBRARY_MANAGER_ACTION)
        assert manager.label == "Library Manager"
        assert "browser" in manager.description.lower()


def test_screen_starts_existing_manager_action_and_displays_connection_details() -> None:
    actions = []

    def popen(argv, **kwargs):
        actions.append(argv[-1])
        return _Process(
            {
                "ok": True,
                "running": True,
                "url": "https://batocera.local:8765/",
                "token": "access-token",
                "started": True,
            }
        )

    state = LibraryManagerScreenState("romcloud", popen=popen)
    state.start_or_refresh()
    assert state.step == STARTING
    _drain(state)

    assert actions == ["manager-start"]
    assert state.step == READY and state.running
    lines = _library_manager_body_lines(state)
    assert "State: Running" in lines
    assert "https://batocera.local:8765/" in lines
    assert "access-token" not in lines
    assert "> Open Here" in lines


def test_failure_can_retry_and_back_always_returns_to_menu() -> None:
    state = LibraryManagerScreenState("romcloud", step=FAILED, error="startup failed")
    assert _handle_library_manager_event(InputEvent(action=Action.BACK), state) == "menu"
    assert "startup failed" in " ".join(_library_manager_body_lines(state))


def test_open_here_surfaces_unavailable_browser_runtime() -> None:
    actions = []

    def popen(argv, **kwargs):
        actions.append(argv[-1])
        return _Process(
            {
                "ok": False,
                "error": "Open Here requires a Chromium-compatible local browser runtime",
            }
        )

    state = LibraryManagerScreenState(
        "romcloud", step=READY, details={"running": True}, popen=popen
    )
    state.activate()
    assert state.step == "opening"
    _drain(state)
    assert actions == ["manager-open-local"]
    assert state.step == FAILED
    assert "Chromium-compatible" in state.error
