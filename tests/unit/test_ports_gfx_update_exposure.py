from __future__ import annotations

from ports_gfx.app import ROOT_MENU_ITEMS, MENU_CATEGORIES
from ports_gfx.menu import NavigationState


def test_settings_exposes_update_install() -> None:
    nav = NavigationState(ROOT_MENU_ITEMS, MENU_CATEGORIES)
    assert nav.open_category("Settings", action="update-install")
    assert nav.selected_item.action == "update-install"
