"""Unit tests for `ports_gfx.menu` — pure selection state, no pygame.

Navigation itself (which index a directional key press should select) is
tested separately in `test_ports_gfx_layout.py`, since it's derived from
actual rendered rects, not array order — see `layout.find_next_focus_index`.
"""

from __future__ import annotations

import pytest

from ports_gfx.menu import EXIT_ACTION, MenuItem, MenuState


def _items():
    return [
        MenuItem("Catalog Status", "status"),
        MenuItem("Refresh Catalog", "refresh"),
        MenuItem("Health Check", "healthcheck"),
        MenuItem("Cache Status", "cache-status"),
        MenuItem("Exit", EXIT_ACTION),
    ]


class TestInitialState:
    def test_starts_at_first_item(self):
        state = MenuState(_items())
        assert state.selected_index == 0
        assert state.selected_item.label == "Catalog Status"

    def test_requires_at_least_one_item(self):
        with pytest.raises(ValueError):
            MenuState([])


class TestSelect:
    def test_select_changes_current_item(self):
        state = MenuState(_items())
        state.select(2)
        assert state.selected_index == 2
        assert state.selected_item.label == "Health Check"

    def test_select_clamps_below_zero(self):
        state = MenuState(_items())
        state.select(-5)
        assert state.selected_index == 0

    def test_select_clamps_above_last_index(self):
        state = MenuState(_items())
        state.select(999)
        assert state.selected_index == len(_items()) - 1

    def test_items_returns_a_copy(self):
        state = MenuState(_items())
        items = state.items
        items.append(MenuItem("Extra", "extra"))
        assert len(state.items) == len(_items())

