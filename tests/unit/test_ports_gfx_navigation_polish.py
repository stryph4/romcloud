from __future__ import annotations

import pytest

from ports_gfx.actions import Action
from ports_gfx.app import MENU_CATEGORIES, MENU_ITEMS, ROOT_MENU_ITEMS, _wizard_option_rows
from ports_gfx.input_manager import InputEvent
from ports_gfx.layout import compute_layout, compute_wizard_regions
from ports_gfx.menu import BACK_ACTION, CATEGORY_ACTION_PREFIX, NavigationState
from ports_gfx.wizard import WizardState, WizardStep


def test_root_items_preserve_the_requested_compact_order_and_actions():
    assert [item.label for item in ROOT_MENU_ITEMS] == [
        "Library",
        "Connected Mode",
        "Cache Mode",
        "Offline Mode",
        "Storage",
        "SaveSync",
        "Maintenance",
        "Settings",
        "Exit",
    ]
    assert [item.action for item in ROOT_MENU_ITEMS] == [
        "category:Library",
        "library-connected",
        "operating-mode-active",
        "library-offline",
        "category:Storage",
        "savesync",
        "category:Maintenance",
        "category:Settings",
        "exit",
    ]


def test_every_preexisting_action_is_mapped_once_without_renaming():
    old = {(item.action, item.label) for item in MENU_ITEMS}
    mapped = [
        (item.action, item.label)
        for items in MENU_CATEGORIES.values()
        for item in items
        if item.action != "update-install"
    ]
    mapped.extend(
        (item.action, item.label)
        for item in ROOT_MENU_ITEMS
        if not item.action.startswith(CATEGORY_ACTION_PREFIX)
    )
    assert old.issubset(set(mapped))
    assert ("library-offline", "Offline Mode") in mapped
    assert ("refresh", "Refresh Catalog") in mapped
    assert ("connection-mount", "Mount / Reconnect") in mapped
    assert ("connection-unmount", "Unmount") in mapped


def test_savesync_root_item_bypasses_the_category_level():
    nav = NavigationState(ROOT_MENU_ITEMS, MENU_CATEGORIES)
    nav.select(5)

    assert nav.selected_item.label == "SaveSync"
    assert nav.selected_item.action == "savesync"
    assert not nav.enter_selected_category()
    assert nav.level == "root"
    assert nav.selected_index == 5
    assert "SaveSync" not in MENU_CATEGORIES


def test_submenu_replaces_root_and_back_restores_root_focus():
    nav = NavigationState(ROOT_MENU_ITEMS, MENU_CATEGORIES)
    nav.select(4)
    assert nav.enter_selected_category()
    assert nav.title == "Storage"
    assert [item.label for item in nav.items[:2]] == ["Storage Setup", "Connection Status"]
    assert all(item.label != "Library" for item in nav.items)
    assert nav.items[-1].action == BACK_ACTION
    assert nav.back()
    assert nav.title == "ROMCloud"
    assert nav.selected_index == 4


def test_update_banner_target_can_open_existing_update_action():
    nav = NavigationState(ROOT_MENU_ITEMS, MENU_CATEGORIES)
    assert nav.open_category("Maintenance", action="update-install")
    assert nav.selected_item.label == "Update ROMCloud"


@pytest.mark.parametrize("width,height", [(1280, 800), (1920, 1080)])
def test_control_panel_and_static_wizard_footer_are_valid(width, height):
    layout = compute_layout(width, height, 5)
    regions = compute_wizard_regions(layout, osk_visible=True)
    assert layout.content_rect.w > 0 and layout.content_rect.h > 0
    assert layout.navigation_rect.w > 0 and layout.navigation_rect.h > 0
    assert layout.activity_rect is not None
    assert layout.activity_rect.w > 0 and not layout.activity_rect.intersects(layout.navigation_rect)
    assert not regions.content.intersects(regions.footer)
    assert not regions.back_button.intersects(regions.continue_button)
    assert regions.osk is not None
    assert not regions.osk.intersects(regions.footer)
    assert regions.content.bottom <= regions.osk.y


def test_activity_degrades_to_compact_mode_without_negative_regions():
    layout = compute_layout(800, 480, 5)
    assert layout.compact is True
    assert layout.activity_rect is None
    assert layout.navigation_rect.w > 0 and layout.navigation_rect.h > 0


def test_long_wizard_option_lists_scroll_above_the_static_footer():
    layout = compute_layout(1280, 800, 5)
    wizard = WizardState()
    wizard.step = WizardStep.LOCAL_BROWSE
    wizard.runner = None
    wizard.browser_entries = [
        {"name": f"Folder {index}", "is_directory": True, "path": f"/{index}"}
        for index in range(30)
    ]
    wizard.selected_index = len(wizard.options) - 1

    rows = _wizard_option_rows(layout, wizard)

    assert len(rows) < len(wizard.options)
    assert rows[-1][0] == wizard.selected_index
    assert all(row[2].bottom <= layout.footer_rect.y for row in rows)


def test_mouse_text_activation_does_not_force_osk_but_controller_does():
    mouse = WizardState()
    mouse.step = WizardStep.SOURCE
    mouse.handle_event(
        InputEvent(action=Action.CONFIRM, source="mouse"), [], "/bin/romcloud"
    )
    assert mouse.osk is not None
    assert mouse.osk_visible is False

    controller = WizardState()
    controller.step = WizardStep.SOURCE
    controller.handle_event(
        InputEvent(action=Action.CONFIRM, source="controller"), [], "/bin/romcloud"
    )
    assert controller.osk is not None
    assert controller.osk_visible is True


def test_escape_hides_osk_preserving_value_and_physical_typing_continues():
    wizard = WizardState()
    wizard.enter_text_step(WizardStep.SERVER)
    wizard.handle_event(
        InputEvent(action=Action.TEXT_INPUT, text="nas", source="keyboard"),
        [],
        "/bin/romcloud",
    )
    wizard.handle_event(
        InputEvent(action=Action.BACK, source="keyboard"), [], "/bin/romcloud"
    )
    assert wizard.step == WizardStep.SERVER
    assert wizard.osk is not None and wizard.osk.text == "nas"
    assert wizard.osk_visible is False
    wizard.handle_event(
        InputEvent(action=Action.TEXT_INPUT, text=".local", source="keyboard"),
        [],
        "/bin/romcloud",
    )
    assert wizard.osk.text == "nas.local"
