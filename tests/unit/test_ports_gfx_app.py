"""Unit tests for the pygame-free parts of `ports_gfx.app`.

`ports_gfx.app` defers its `import pygame` to inside `run_app`/`_run`, so
importing the module itself — and exercising `MENU_ITEMS` / `format_result`
— never requires pygame to be installed. The actual render/event loop
(`_run`, `_render`) needs a real display and is not covered here, mirroring
how `romcloud.ui.progress`/`romcloud.ui.maintenance` leave their curses
render loops untested.
"""

from __future__ import annotations

from ports_gfx.app import (
    MENU_ITEMS,
    _ControllerTestScreenState,
    _apply_direction,
    _handle_controller_test_event,
    _handle_menu_event,
    format_result,
)
from ports_gfx.actions import Action
from ports_gfx.client import BackendResult
from ports_gfx.input_manager import InputEvent
from ports_gfx.layout import compute_layout
from ports_gfx.menu import CONTROLLER_TEST_ACTION, EXIT_ACTION, MenuState


class TestMenuItems:
    def test_contains_expected_actions_in_order(self):
        actions = [item.action for item in MENU_ITEMS]
        assert actions == [
            "status",
            "refresh",
            "healthcheck",
            "cache-status",
            CONTROLLER_TEST_ACTION,
            EXIT_ACTION,
        ]

    def test_exit_is_the_last_item(self):
        assert MENU_ITEMS[-1].action == EXIT_ACTION


class TestFormatResult:
    def test_success_includes_action_and_data(self):
        result = BackendResult(ok=True, data={"ok": True, "games_total": 5})
        line = format_result("status", result)
        assert line.startswith("status:")
        assert "games_total" in line

    def test_failure_shows_error_message(self):
        result = BackendResult(ok=False, error="connection refused")
        line = format_result("healthcheck", result)
        assert line == "Error: connection refused"


class TestRunAppHandlesMissingPygame:
    def test_returns_nonzero_and_prints_clear_message_without_pygame(self, monkeypatch, capsys):
        import builtins

        import ports_gfx.app as app_module

        orig_import = builtins.__import__

        def _blocked(name, globals=None, locals=None, fromlist=(), level=0):
            if name == "pygame":
                raise ImportError("No module named 'pygame'")
            return orig_import(name, globals, locals, fromlist, level)

        monkeypatch.setattr(builtins, "__import__", _blocked)

        exit_code = app_module.run_app("/opt/romcloud/bin/romcloud")

        assert exit_code == 1
        captured = capsys.readouterr()
        assert "pygame is not available" in captured.err


class TestApplyDirection:
    def test_moves_selection_toward_next_widget(self):
        state = MenuState(list(MENU_ITEMS))
        layout = compute_layout(1920, 1080, len(state.items))
        _apply_direction(state, layout, Action.RIGHT)
        assert state.selected_index == 1

    def test_non_directional_action_is_a_no_op(self):
        state = MenuState(list(MENU_ITEMS))
        layout = compute_layout(1920, 1080, len(state.items))
        before = state.selected_index
        _apply_direction(state, layout, Action.CONFIRM)
        assert state.selected_index == before


class TestHandleMenuEvent:
    def _state(self):
        return MenuState(list(MENU_ITEMS))

    def _layout(self, state):
        return compute_layout(1920, 1080, len(state.items))

    def test_confirm_on_exit_item_stops_running(self):
        state = self._state()
        layout = self._layout(state)
        exit_index = next(i for i, item in enumerate(state.items) if item.action == EXIT_ACTION)
        state.select(exit_index)

        running, screen, message, is_error = _handle_menu_event(
            InputEvent(action=Action.CONFIRM), state, layout, "/opt/romcloud/bin/romcloud", True, None, False,
        )
        assert running is False
        assert screen == "menu"

    def test_confirm_on_controller_test_item_switches_screen(self):
        state = self._state()
        layout = self._layout(state)
        idx = next(i for i, item in enumerate(state.items) if item.action == CONTROLLER_TEST_ACTION)
        state.select(idx)

        running, screen, message, is_error = _handle_menu_event(
            InputEvent(action=Action.CONFIRM), state, layout, "/opt/romcloud/bin/romcloud", True, None, False,
        )
        assert running is True
        assert screen == "controller_test"

    def test_back_action_quits_the_app(self):
        state = self._state()
        layout = self._layout(state)
        running, screen, message, is_error = _handle_menu_event(
            InputEvent(action=Action.BACK), state, layout, "/opt/romcloud/bin/romcloud", True, None, False,
        )
        assert running is False

    def test_directional_action_moves_selection_via_layout(self):
        state = self._state()
        layout = self._layout(state)
        running, screen, message, is_error = _handle_menu_event(
            InputEvent(action=Action.RIGHT), state, layout, "/opt/romcloud/bin/romcloud", True, None, False,
        )
        assert running is True
        assert screen == "menu"
        assert message is None
        assert state.selected_index == 1

    def test_touch_index_focuses_before_dispatch(self):
        state = self._state()
        layout = self._layout(state)
        exit_index = next(i for i, item in enumerate(state.items) if item.action == EXIT_ACTION)

        running, screen, message, is_error = _handle_menu_event(
            InputEvent(action=Action.CONFIRM, touch_index=exit_index),
            state, layout, "/opt/romcloud/bin/romcloud", True, None, False,
        )
        assert state.selected_index == exit_index
        assert running is False


class TestHandleControllerTestEvent:
    def test_directional_action_moves_slot_selection(self):
        controller_test = _ControllerTestScreenState()
        from ports_gfx.input_manager import InputManager
        from tests.unit._pygame_fakes import make_fake_pygame

        manager = InputManager(make_fake_pygame(), "/opt/romcloud/bin/romcloud")
        before = controller_test.selected_index
        screen = _handle_controller_test_event(InputEvent(action=Action.DOWN), controller_test, manager)
        assert screen == "controller_test"
        assert controller_test.selected_index != before

    def test_back_returns_to_menu_and_cancels_remap(self):
        controller_test = _ControllerTestScreenState()
        from ports_gfx.input_manager import InputManager
        from tests.unit._pygame_fakes import make_fake_pygame

        manager = InputManager(make_fake_pygame(), "/opt/romcloud/bin/romcloud")
        controller_test.remap_instance_id = 1
        screen = _handle_controller_test_event(InputEvent(action=Action.BACK), controller_test, manager)
        assert screen == "menu"

    def test_confirm_with_no_controller_connected_is_a_no_op(self):
        controller_test = _ControllerTestScreenState()
        from ports_gfx.input_manager import InputManager
        from tests.unit._pygame_fakes import make_fake_pygame

        manager = InputManager(make_fake_pygame(), "/opt/romcloud/bin/romcloud")
        screen = _handle_controller_test_event(InputEvent(action=Action.CONFIRM), controller_test, manager)
        assert screen == "controller_test"
        assert controller_test.remap_instance_id is None
