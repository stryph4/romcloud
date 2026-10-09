from __future__ import annotations

from ports_gfx.actions import Action
from ports_gfx.app import MENU_CATEGORIES, _OPERATIONS
from ports_gfx.operation import OperationLine, OperationState
from ports_gfx.operation_screen import (
    OperationScreenState,
    troubleshoot_result_summary,
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


def test_diagnostic_summary_distinguishes_health_errors_from_process_success() -> None:
    screen = OperationScreenState(
        title="Troubleshoot ROMCloud",
        runner=_Runner(
            '{"ok":true,"summary":{"healthy":4,"fixed":0,"warning":2,'
            '"error":1,"skipped":0},"findings":['
            '{"status":"warning","fixability":"automatic","blocked_by":[]},'
            '{"status":"warning","fixability":"none","blocked_by":[]}]} '
        ),
    )

    text, kind = troubleshoot_result_summary(screen)

    assert "3 issue(s) found" in text
    assert "1 can be repaired automatically" in text
    assert kind == "error"


def test_quick_repair_summary_reports_fixed_and_remaining() -> None:
    screen = OperationScreenState(
        title="Quick Repair",
        runner=_Runner(
            '{"ok":true,"summary":{"healthy":4,"fixed":2,"warning":1,'
            '"error":0,"skipped":0}}'
        ),
    )

    text, kind = troubleshoot_result_summary(screen)

    assert text == "Quick Repair complete — 2 fixed; 1 issue(s) still need attention"
    assert kind == "warning"
