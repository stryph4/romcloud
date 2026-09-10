from __future__ import annotations

from ports_gfx.actions import Action
from ports_gfx.app import root_menu_items_for_state
from ports_gfx.input_manager import InputEvent
from ports_gfx.setup_save_conflict import SetupSaveConflictState


def test_conflict_decision_resolves_or_cancels_without_mutating_saves() -> None:
    state = SetupSaveConflictState(("one",))
    assert state.handle_event(InputEvent(action=Action.CONFIRM)) == "resolve"
    assert state.handle_event(InputEvent(action=Action.BACK)) == "cancel"


def test_setup_conflicts_offer_resolution_or_finish_without_destructive_hold() -> None:
    state = SetupSaveConflictState(("one",))
    assert state.action_labels == ("Resolve Save Conflicts", "Finish Setup")
    state.select(1)
    assert state.handle_event(InputEvent(action=Action.CONFIRM)) == "finish"
    assert state.update(10.0) is None


def test_direct_menu_always_describes_local_savesync_behavior() -> None:
    items = root_menu_items_for_state({
        "game_management_enabled": True,
        "operating_mode": "connected",
        "capabilities": {},
    })
    direct = next(item for item in items if item.label == "Direct")
    assert "Games run from the configured ROM source" in direct.description
    assert "Gameplay saves stay local" in direct.description
    assert "SaveSync" in direct.description
