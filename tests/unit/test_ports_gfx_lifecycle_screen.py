from __future__ import annotations

from ports_gfx.actions import Action
from ports_gfx.input_manager import InputEvent
from ports_gfx.lifecycle_screen import LifecycleScreenState, launch_lifecycle_helper


def test_uninstall_and_purge_require_hold_and_back_is_zero_mutation() -> None:
    state = LifecycleScreenState()
    assert state.handle_event(InputEvent(action=Action.BACK)) == "back"
    assert not state.confirm.confirmed

    state = LifecycleScreenState()
    state.selected_index = 1
    state.handle_event(InputEvent(action=Action.CONFIRM))
    assert state.operation == "purge"
    assert state.view == "choices"
    state.handle_event(InputEvent(action=Action.CONFIRM))
    assert state.view == "confirm"
    assert not state.update(2.9)
    assert state.update(0.2)


def test_detached_helper_waits_for_the_gui_pid() -> None:
    calls = []

    def popen(argv, **kwargs):
        calls.append((argv, kwargs))
        return object()

    launch_lifecycle_helper("/owned/bin/romcloud", "uninstall", gui_pid=4321, popen=popen)

    argv, kwargs = calls[0]
    assert argv == [
        "/owned/bin/romcloud",
        "uninstall",
        "--yes",
        "--wait-for-pid",
        "4321",
    ]
    assert kwargs["start_new_session"] is True
