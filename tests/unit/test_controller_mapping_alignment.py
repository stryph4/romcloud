from __future__ import annotations

from ports_gfx.actions import Action
from ports_gfx.app import MENU_CATEGORIES, _CONTROLLER_ACTION_LABELS, _REMAPPABLE_ACTIONS
from ports_gfx.menu import CONTROLLER_TEST_ACTION


def test_setup_controller_mapping_is_the_single_native_mapping_entry() -> None:
    entries = [
        item for item in MENU_CATEGORIES["Maintenance"]
        if item.action == CONTROLLER_TEST_ACTION
    ]
    assert [item.label for item in entries] == ["Setup Controller Mapping"]


def test_setup_exposes_the_browser_logical_action_contract() -> None:
    assert _REMAPPABLE_ACTIONS == (
        Action.UP,
        Action.DOWN,
        Action.LEFT,
        Action.RIGHT,
        Action.CONFIRM,
        Action.BACK,
        Action.PREVIOUS_PAGE,
        Action.NEXT_PAGE,
        Action.MENU,
    )
    assert _CONTROLLER_ACTION_LABELS[Action.PREVIOUS_PAGE] == "Previous Page / LB"
    assert _CONTROLLER_ACTION_LABELS[Action.NEXT_PAGE] == "Next Page / RB"
    assert _CONTROLLER_ACTION_LABELS[Action.MENU] == "Menu / Start"
