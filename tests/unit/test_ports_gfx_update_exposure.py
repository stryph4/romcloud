from __future__ import annotations

from ports_gfx.app import ROOT_MENU_ITEMS, MENU_CATEGORIES
from ports_gfx.menu import NavigationState


def test_settings_exposes_update_install() -> None:
    nav = NavigationState(ROOT_MENU_ITEMS, MENU_CATEGORIES)
    assert nav.open_category("Maintenance", action="update-install")
    assert nav.selected_item.action == "update-install"


def test_maintenance_exposes_repair_installation_with_data_safe_description() -> None:
    nav = NavigationState(ROOT_MENU_ITEMS, MENU_CATEGORIES)
    assert nav.open_category("Maintenance", action="repair-install")
    assert nav.selected_item.label == "Repair Installation"
    assert "without deleting user data" in nav.selected_item.description


def test_repair_uses_long_running_operation_and_gui_relaunch() -> None:
    from ports_gfx.app import _OPERATIONS

    spec = _OPERATIONS["repair-install"]
    assert spec.args == ("uidata", "repair-install")
    assert spec.arms_gui_relaunch is True
    assert spec.relaunch_operation == "repair"
