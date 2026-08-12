from __future__ import annotations

from ports_gfx.client import BackendResult
from ports_gfx.operation import OperationState
from ports_gfx.update_state import UpdateCheckState, update_controls_disabled


def test_current_version_has_no_intrusive_banner():
    state = UpdateCheckState.completed(
        BackendResult(ok=True, data={"update_available": False, "current_version": "1.0.0"})
    )
    assert state.status == "current"
    assert state.banner == ""


def test_newer_version_produces_compact_update_banner():
    state = UpdateCheckState.completed(
        BackendResult(ok=True, data={"update_available": True, "available_version": "1.1.0"})
    )
    assert state.status == "available"
    assert state.banner == "Update available v1.1.0"


def test_check_failure_is_non_disruptive_state():
    state = UpdateCheckState.completed(BackendResult(ok=False, error="offline"))
    assert state.status == "error"
    assert state.update_available is False
    assert state.error == "offline"


def test_only_active_install_disables_conflicting_controls():
    assert update_controls_disabled(OperationState.RUNNING)
    assert update_controls_disabled(OperationState.STARTING)
    assert not update_controls_disabled(OperationState.SUCCEEDED)
    assert not update_controls_disabled(None)


def test_shutdown_cancels_pending_update_check():
    class PendingRunner:
        cancelled = False

        def cancel(self):
            self.cancelled = True

    state = UpdateCheckState()
    state.runner = PendingRunner()
    state.status = "checking"

    state.cancel()

    assert state.runner.cancelled is True
    assert state.status == "cancelled"
    assert state.error == "Update check cancelled"
