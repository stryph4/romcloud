"""Unit tests for the pygame-free parts of `ports_gfx.app`.

`ports_gfx.app` defers its `import pygame` to inside `run_app`/`_run`, so
importing the module itself — and exercising `MENU_ITEMS` / `format_result`
— never requires pygame to be installed. The actual render/event loop
(`_run`, `_render`) needs a real display and is not covered here, mirroring
how `romcloud.ui.progress`/`romcloud.ui.maintenance` leave their curses
render loops untested.
"""

from __future__ import annotations

from ports_gfx.app import MENU_ITEMS, format_result
from ports_gfx.client import BackendResult
from ports_gfx.menu import EXIT_ACTION


class TestMenuItems:
    def test_contains_expected_actions_in_order(self):
        actions = [item.action for item in MENU_ITEMS]
        assert actions == ["status", "refresh", "healthcheck", "cache-status", EXIT_ACTION]

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
