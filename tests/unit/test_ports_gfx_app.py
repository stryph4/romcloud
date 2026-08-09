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
    classify_message_kind,
    format_result,
    initial_screen_for_status,
    operation_summary_message,
    start_operation,
)
from ports_gfx.actions import Action
from ports_gfx.client import BackendResult
from ports_gfx.input_manager import InputEvent
from ports_gfx.layout import compute_layout
from ports_gfx.menu import CONTROLLER_TEST_ACTION, EXIT_ACTION, MenuState
from ports_gfx.operation import OperationState
from ports_gfx.operation_screen import OPERATION_SCREEN


class TestMenuItems:
    def test_contains_expected_actions_in_order(self):
        actions = [item.action for item in MENU_ITEMS]
        assert actions == [
            "status",
            "refresh",
            "healthcheck",
            "cache-status",
            "update-check",
            CONTROLLER_TEST_ACTION,
            EXIT_ACTION,
        ]

    def test_exit_is_the_last_item(self):
        assert MENU_ITEMS[-1].action == EXIT_ACTION


class TestInitialScreen:
    def test_fresh_install_opens_wizard(self):
        status = BackendResult(ok=True, data={"state": "fresh"})
        assert initial_screen_for_status(status) == "wizard"

    def test_configured_install_opens_unchanged_dashboard(self):
        status = BackendResult(ok=True, data={"state": "configured"})
        assert initial_screen_for_status(status) == "menu"

    def test_partial_or_broken_install_opens_repair_wizard(self):
        partial = BackendResult(ok=True, data={"state": "partial"})
        failed = BackendResult(ok=False, error="malformed response")
        assert initial_screen_for_status(partial) == "wizard"
        assert initial_screen_for_status(failed) == "wizard"


class TestFormatResult:
    def test_success_includes_action_and_data(self):
        result = BackendResult(ok=True, data={"ok": True, "games_total": 5})
        line = format_result("status", result)
        assert line.startswith("status:")
        assert "games=5" in line
        assert "cached=0" in line
        assert "pinned=0" in line

    def test_failure_shows_error_message(self):
        result = BackendResult(ok=False, error="connection refused")
        line = format_result("healthcheck", result)
        assert line == "Error: connection refused"

    def test_status_result_formats_source_summary(self):
        result = BackendResult(
            ok=True,
            data={
                "source_type": "SMB",
                "source_description": "nas.local:ROMs",
                "games_total": 12,
                "cached": 3,
                "pinned": 1,
            },
        )
        line = format_result("status", result)
        assert "SMB" in line
        assert "nas.local:ROMs" in line
        assert "games=12" in line
        assert "cached=3" in line
        assert "pinned=1" in line

    def test_healthcheck_result_formats_source_summary(self):
        result = BackendResult(
            ok=True,
            data={
                "source_type": "Local filesystem",
                "source_description": "/userdata/roms",
                "source_reachable": True,
            },
        )
        line = format_result("healthcheck", result)
        assert "Local filesystem" in line
        assert "/userdata/roms" in line
        assert "reachable" in line


class TestClassifyMessageKind:
    def test_failed_call_is_error(self):
        result = BackendResult(ok=False, error="boom")
        assert classify_message_kind("status", result) == "error"

    def test_successful_status_call_is_success(self):
        result = BackendResult(ok=True, data={"ok": True, "games_total": 3})
        assert classify_message_kind("status", result) == "success"

    def test_healthcheck_unreachable_source_is_warning_not_error(self):
        result = BackendResult(ok=True, data={"ok": True, "source_reachable": False})
        assert classify_message_kind("healthcheck", result) == "warning"

    def test_healthcheck_reachable_source_is_success(self):
        result = BackendResult(ok=True, data={"ok": True, "source_reachable": True})
        assert classify_message_kind("healthcheck", result) == "success"


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

        running, screen, message, kind, operation = _handle_menu_event(
            InputEvent(action=Action.CONFIRM), state, layout, "/opt/romcloud/bin/romcloud", True, None, "info",
        )
        assert running is False
        assert screen == "menu"
        assert operation is None

    def test_confirm_on_controller_test_item_switches_screen(self):
        state = self._state()
        layout = self._layout(state)
        idx = next(i for i, item in enumerate(state.items) if item.action == CONTROLLER_TEST_ACTION)
        state.select(idx)

        running, screen, message, kind, operation = _handle_menu_event(
            InputEvent(action=Action.CONFIRM), state, layout, "/opt/romcloud/bin/romcloud", True, None, "info",
        )
        assert running is True
        assert screen == "controller_test"
        assert operation is None

    def test_back_action_quits_the_app(self):
        state = self._state()
        layout = self._layout(state)
        running, screen, message, kind, operation = _handle_menu_event(
            InputEvent(action=Action.BACK), state, layout, "/opt/romcloud/bin/romcloud", True, None, "info",
        )
        assert running is False
        assert operation is None

    def test_directional_action_moves_selection_via_layout(self):
        state = self._state()
        layout = self._layout(state)
        running, screen, message, kind, operation = _handle_menu_event(
            InputEvent(action=Action.RIGHT), state, layout, "/opt/romcloud/bin/romcloud", True, None, "info",
        )
        assert running is True
        assert screen == "menu"
        assert message is None
        assert state.selected_index == 1
        assert operation is None

    def test_touch_index_focuses_before_dispatch(self):
        state = self._state()
        layout = self._layout(state)
        exit_index = next(i for i, item in enumerate(state.items) if item.action == EXIT_ACTION)

        running, screen, message, kind, operation = _handle_menu_event(
            InputEvent(action=Action.CONFIRM, touch_index=exit_index),
            state, layout, "/opt/romcloud/bin/romcloud", True, None, "info",
        )
        assert state.selected_index == exit_index
        assert running is False
        assert operation is None

    def test_confirm_on_refresh_item_starts_an_operation_and_switches_screen(self, monkeypatch):
        import sys

        from ports_gfx import app as app_module

        state = self._state()
        layout = self._layout(state)
        refresh_index = next(i for i, item in enumerate(state.items) if item.action == "refresh")
        state.select(refresh_index)

        def fake_popen(argv, **kwargs):
            import subprocess

            return subprocess.Popen(
                [sys.executable, "-c", "pass"],
                stdout=kwargs.get("stdout"),
                stderr=kwargs.get("stderr"),
                text=True,
            )

        def fake_start_operation(action, romcloud_bin):
            spec = app_module._OPERATIONS[action]
            runner = app_module.OperationRunner([romcloud_bin, *spec.args], popen=fake_popen)
            runner.start()
            return app_module.OperationScreenState(title=spec.title, runner=runner)

        monkeypatch.setattr(app_module, "start_operation", fake_start_operation)

        running, screen, message, kind, operation = app_module._handle_menu_event(
            InputEvent(action=Action.CONFIRM), state, layout, "/opt/romcloud/bin/romcloud", True, None, "info",
        )

        assert running is True
        assert screen == OPERATION_SCREEN
        assert operation is not None
        assert operation.title == "Refresh Catalog"


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


class TestStartOperation:
    def test_builds_argv_from_spec_and_starts_the_runner(self):
        import subprocess
        import sys

        captured = {}

        def fake_popen(argv, **kwargs):
            captured["argv"] = argv
            return subprocess.Popen(
                [sys.executable, "-c", "pass"], stdout=kwargs.get("stdout"), stderr=kwargs.get("stderr"), text=True,
            )

        operation = start_operation("refresh", "/opt/romcloud/bin/romcloud", popen=fake_popen)

        assert operation.title == "Refresh Catalog"
        assert captured["argv"] == ["/opt/romcloud/bin/romcloud", "refresh"]
        assert operation.runner.state in (OperationState.RUNNING, OperationState.SUCCEEDED)

    def test_unknown_action_raises_key_error(self):
        import pytest

        with pytest.raises(KeyError):
            start_operation("not-a-real-operation", "/opt/romcloud/bin/romcloud")


class _FakeFinishedRunner:
    def __init__(self, state: OperationState, error: str = "") -> None:
        self.state = state
        self.error = error


class TestOperationSummaryMessage:
    def _operation(self, *, state: OperationState, error: str = ""):
        from ports_gfx.operation_screen import OperationScreenState

        return OperationScreenState(title="Refresh Catalog", runner=_FakeFinishedRunner(state, error))

    def test_succeeded_operation_reports_success(self):
        operation = self._operation(state=OperationState.SUCCEEDED)
        message, kind = operation_summary_message(operation)
        assert message == "Refresh Catalog: succeeded"
        assert kind == "success"

    def test_failed_operation_reports_error_with_detail(self):
        operation = self._operation(state=OperationState.FAILED, error="exited with code 1")
        message, kind = operation_summary_message(operation)
        assert message == "Refresh Catalog: failed (exited with code 1)"
        assert kind == "error"

    def test_failed_operation_without_detail_still_reports_error(self):
        operation = self._operation(state=OperationState.FAILED, error="")
        message, kind = operation_summary_message(operation)
        assert message == "Refresh Catalog: failed"
        assert kind == "error"
