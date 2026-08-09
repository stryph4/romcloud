"""Unit tests for `ports_gfx.menu` — pure navigation state, no pygame."""

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


class TestNavigation:
    def test_move_down_advances_selection(self):
        state = MenuState(_items())
        state.move_down()
        assert state.selected_index == 1
        assert state.selected_item.label == "Refresh Catalog"

    def test_move_up_from_first_wraps_to_last(self):
        state = MenuState(_items())
        state.move_up()
        assert state.selected_index == len(_items()) - 1
        assert state.selected_item.action == EXIT_ACTION

    def test_move_down_from_last_wraps_to_first(self):
        state = MenuState(_items())
        for _ in range(len(_items()) - 1):
            state.move_down()
        assert state.selected_item.action == EXIT_ACTION
        state.move_down()
        assert state.selected_index == 0

    def test_items_returns_a_copy(self):
        state = MenuState(_items())
        items = state.items
        items.append(MenuItem("Extra", "extra"))
        assert len(state.items) == len(_items())
