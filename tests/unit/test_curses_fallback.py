"""Regression tests for systems without the `curses` module.

These tests simulate `curses` being unavailable (raise ModuleNotFoundError
on import) and assert that importing UI prompt utilities and invoking the
CLI (e.g. `--version`) do not require `curses` at import time.
"""

from __future__ import annotations

import builtins
import importlib
import sys
from types import ModuleType

import click.testing


def _make_import_blocker(orig_import):
    def _blocked(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "curses" or name.startswith("curses."):
            raise ModuleNotFoundError("No module named 'curses'")
        return orig_import(name, globals, locals, fromlist, level)

    return _blocked


def test_import_prompts_without_curses(monkeypatch):
    orig_import = builtins.__import__
    monkeypatch.setattr(builtins, "__import__", _make_import_blocker(orig_import))

    # Ensure a fresh import sequence, but restore the original module objects
    # afterward so later tests keep seeing the same imported package graph.
    removed = {key: sys.modules.pop(key) for key in ("romcloud.ui", "romcloud.ui.prompts") if key in sys.modules}
    try:
        mod = importlib.import_module("romcloud.ui.prompts")
        assert mod is not None
    finally:
        for key, value in removed.items():
            sys.modules[key] = value


def test_cli_version_without_curses(monkeypatch):
    orig_import = builtins.__import__
    monkeypatch.setattr(builtins, "__import__", _make_import_blocker(orig_import))

    # Ensure a fresh import, but restore the original module objects afterward
    # so subsequent tests don't end up with stale references vs. re-imported
    # modules.
    removed = {key: sys.modules.pop(key) for key in list(sys.modules.keys()) if key.startswith("romcloud")}
    try:
        # Import CLI after blocking curses
        cli_mod = importlib.import_module("romcloud.cli.main")
        runner = click.testing.CliRunner()
        result = runner.invoke(cli_mod.cli, ["--version"])
        assert result.exit_code == 0
        assert "romcloud" in result.output.lower()
    finally:
        for key, value in removed.items():
            sys.modules[key] = value
