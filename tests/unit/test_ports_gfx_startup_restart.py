from ports_gfx.startup_restart import (
    LATER,
    RESTART_NOW,
    StartupRestartPromptState,
)


def test_restart_now_is_default_and_later_is_selectable():
    state = StartupRestartPromptState()
    assert state.activate() == RESTART_NOW

    state.move(1)
    assert state.activate() == LATER
    assert state.actions == ("Restart Now", "Later")


def test_restart_choice_wraps_and_direction_clears_error():
    state = StartupRestartPromptState(error="failed")
    state.move(-1)
    assert state.selected_index == 1
    assert state.error is None
