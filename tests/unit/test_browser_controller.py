from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).parents[2]
STATIC = ROOT / "src" / "romcloud" / "web" / "static"


def test_controller_assets_wire_all_required_inputs_and_focus_scopes() -> None:
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    javascript = (STATIC / "controller.js").read_text(encoding="utf-8")
    app = (STATIC / "app.js").read_text(encoding="utf-8")
    css = (STATIC / "app.css").read_text(encoding="utf-8")

    assert html.index('/controller.js') < html.index('/app.js')
    for zone in ("systems", "tabs", "controls", "games", "dialog", "pager"):
        assert zone in javascript or f'data-controller-zone="{zone}"' in html
    assert "navigator.getGamepads" in javascript
    assert 'mapping === "standard"' in javascript
    for slot in (0, 1, 4, 5, 9, 12, 13, 14, 15):
        assert f"button: {slot}" in javascript
    for action in (
        "up", "down", "left", "right", "confirm", "back",
        "previous_page", "next_page", "menu",
    ):
        assert action in javascript
    assert 'gamepaddisconnected' in javascript
    assert 'dialog[open]' in javascript
    assert 'romcloud:page-jump' in javascript and 'romcloud:page-jump' in app
    assert 'romcloud:controller-menu' in javascript
    assert 'romcloud:controller-text' in javascript
    assert 'BrowserControllerDiagnostics' in javascript
    assert '/api/controller-diagnostics' in app
    assert 'state.controllerFirst ? "/api/controller-diagnostics" : ""' in app
    assert 'id="controller-osk"' in html
    assert 'id="osk-submit"' in html and 'id="osk-cancel"' in html
    assert 'get("interaction") === "controller"' in app
    assert 'state.localSession &&' in app
    assert 'window.romcloudGamepad.focusZone("games")' in app
    assert 'id="exit-open-here"' in html
    assert "standard mapping unavailable" in app
    assert 'controller-focus' in css and 'controller-editing' in css


def test_browser_and_native_share_one_logical_action_contract() -> None:
    from ports_gfx.actions import Action

    javascript = (STATIC / "controller.js").read_text(encoding="utf-8")
    shared = {
        Action.UP, Action.DOWN, Action.LEFT, Action.RIGHT, Action.CONFIRM,
        Action.BACK, Action.PREVIOUS_PAGE, Action.NEXT_PAGE, Action.MENU,
    }
    assert {action.value for action in shared} == {
        "up", "down", "left", "right", "confirm", "back",
        "previous_page", "next_page", "menu",
    }
    for action in shared:
        assert f'"{action.value}"' in javascript


def test_controller_focus_and_repeat_state_machine_when_node_is_available() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is not installed on this development host")
    result = subprocess.run(
        [
            node,
            str(ROOT / "tests" / "js" / "controller_state.test.js"),
            str(STATIC / "controller.js"),
        ],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "controller state tests passed" in result.stdout


def test_controller_core_executes_in_chromium_when_available(tmp_path: Path) -> None:
    candidates = [
        shutil.which("chromium"),
        shutil.which("google-chrome"),
        shutil.which("chrome"),
        Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
        Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"),
    ]
    browser = next((str(path) for path in candidates if path and Path(path).is_file()), None)
    if browser is None:
        pytest.skip("Chromium is not installed on this development host")
    harness = (ROOT / "tests" / "js" / "controller_harness.html").resolve().as_uri()
    result = subprocess.run(
        [
            browser,
            "--headless=new",
            "--disable-gpu",
            "--no-sandbox",
            f"--user-data-dir={tmp_path / 'core-profile'}",
            "--dump-dom",
            harness,
        ],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    diagnostic = result.stdout + result.stderr
    if (
        result.returncode
        and "crashpad" in diagnostic
        and "Operation not permitted" in diagnostic
    ):
        pytest.skip("Chromium crash reporter is blocked by this test sandbox")
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'data-result="passed"' in result.stdout, result.stdout + result.stderr


def test_gamepad_navigation_executes_in_chromium_when_available(tmp_path: Path) -> None:
    candidates = [
        shutil.which("chromium"),
        shutil.which("google-chrome"),
        shutil.which("chrome"),
        Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
        Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"),
    ]
    browser = next((str(path) for path in candidates if path and Path(path).is_file()), None)
    if browser is None:
        pytest.skip("Chromium is not installed on this development host")
    harness = (ROOT / "tests" / "js" / "controller_browser_harness.html").resolve().as_uri()
    result = subprocess.run(
        [
            browser,
            "--headless=new",
            "--disable-gpu",
            "--no-sandbox",
            f"--user-data-dir={tmp_path / 'navigation-profile'}",
            "--dump-dom",
            harness,
        ],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    diagnostic = result.stdout + result.stderr
    if (
        result.returncode
        and "crashpad" in diagnostic
        and "Operation not permitted" in diagnostic
    ):
        pytest.skip("Chromium crash reporter is blocked by this test sandbox")
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'data-result="passed"' in result.stdout, result.stdout + result.stderr
