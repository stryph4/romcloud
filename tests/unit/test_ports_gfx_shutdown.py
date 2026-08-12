"""Focused GUI shutdown ownership tests."""

from __future__ import annotations

from ports_gfx.app import (
    cancel_owned_background_work,
    render_completed_mode_transition_exit,
)
from ports_gfx.operation import OperationLine, OperationState
from ports_gfx.operation_screen import OperationScreenState
from ports_gfx.relaunch import GuiRelaunchCoordinator


class _PendingRunner:
    state = OperationState.RUNNING
    is_finished = False
    cancelled = False

    def cancel(self):
        self.cancelled = True


class _PendingOwner:
    cancelled = False

    def cancel(self):
        self.cancelled = True


class _PendingSaveSyncOwner:
    cancelled = False

    def cancel_pending(self):
        self.cancelled = True


class _FinishedModeRunner:
    state = OperationState.SUCCEEDED
    is_finished = True
    lines = [
        OperationLine(
            "stdout",
            '{"ok":true,"mode_changed":true,"es_restart_requested":true}',
        )
    ]

    def cancel(self):
        raise AssertionError("completed mode operation must not be cancelled")


class _Splash:
    def __init__(self):
        self.frames = []

    def render(self, *args):
        self.frames.append(args)


def test_app_exit_cancels_foreground_and_background_network_work():
    operation = OperationScreenState(
        title="Mount / Reconnect", runner=_PendingRunner()
    )
    update_check = _PendingOwner()
    savesync = _PendingSaveSyncOwner()

    cancel_owned_background_work(
        update_check=update_check,
        operation_screen=operation,
        wizard=None,
        savesync_screen=savesync,
        library_sync_screen=None,
    )

    assert update_check.cancelled is True
    assert operation.runner.cancelled is True
    assert savesync.cancelled is True


def test_mode_terminal_exit_cancels_unrelated_update_without_arming_relaunch():
    operation = OperationScreenState(
        title="Direct",
        runner=_FinishedModeRunner(),
        exits_after_mode_change=True,
    )
    update_check = _PendingOwner()
    splash = _Splash()
    relaunch = GuiRelaunchCoordinator("/userdata/system/romcloud/bin/romcloud")

    assert render_completed_mode_transition_exit(operation, splash) is True
    cancel_owned_background_work(
        update_check=update_check,
        operation_screen=operation,
        wizard=None,
        savesync_screen=None,
        library_sync_screen=None,
    )

    assert update_check.cancelled is True
    assert relaunch.relaunch_pending is False
    assert relaunch.terminal is False
