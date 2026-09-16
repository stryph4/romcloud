from __future__ import annotations

from ports_gfx.actions import Action
from ports_gfx.app import MENU_CATEGORIES, _OPERATIONS
from ports_gfx.operation import OperationLine, OperationState
from ports_gfx.operation_screen import (
    OperationScreenState,
    troubleshoot_quick_repair_requested,
)


class _Runner:
    state = OperationState.SUCCEEDED
    is_finished = True

    def __init__(self, payload: str) -> None:
        self.lines = [OperationLine("stdout", payload)]


def test_maintenance_replaces_healthcheck_with_troubleshoot() -> None:
    actions = [item.action for item in MENU_CATEGORIES["Maintenance"]]
    assert "troubleshoot" in actions
    assert "healthcheck" not in actions


def test_quick_repair_requires_explicit_confirm() -> None:
    screen = OperationScreenState(
        title="Troubleshoot ROMCloud",
        runner=_Runner('{"ok":true,"quick_repair_available":true}'),
    )
    assert troubleshoot_quick_repair_requested(screen, Action.CONFIRM) is True
    assert troubleshoot_quick_repair_requested(screen, Action.BACK) is False


def test_no_quick_repair_action_when_no_safe_fix_exists() -> None:
    screen = OperationScreenState(
        title="Troubleshoot ROMCloud",
        runner=_Runner('{"ok":true,"quick_repair_available":false}'),
    )
    assert screen.quick_repair_available is False
    assert troubleshoot_quick_repair_requested(screen, Action.CONFIRM) is False


def test_troubleshoot_never_arms_gui_relaunch() -> None:
    assert _OPERATIONS["troubleshoot"].arms_gui_relaunch is False
    assert _OPERATIONS["troubleshoot-fix"].arms_gui_relaunch is False
