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
    assert 'a: button(0)' in javascript and 'b: button(1)' in javascript
    assert 'lb: button(4)' in javascript and 'rb: button(5)' in javascript
    assert 'button(12)' in javascript and 'button(15)' in javascript
    assert 'pad.axes[0]' in javascript and 'pad.axes[1]' in javascript
    assert 'gamepaddisconnected' in javascript
    assert 'dialog[open]' in javascript
    assert 'romcloud:page-jump' in javascript and 'romcloud:page-jump' in app
    assert 'controller-focus' in css and 'controller-editing' in css


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


def test_controller_core_executes_in_chromium_when_available() -> None:
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
            "--dump-dom",
            harness,
        ],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'data-result="passed"' in result.stdout, result.stdout + result.stderr


def test_gamepad_navigation_executes_in_chromium_when_available() -> None:
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
        [browser, "--headless=new", "--disable-gpu", "--no-sandbox", "--dump-dom", harness],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'data-result="passed"' in result.stdout, result.stdout + result.stderr
