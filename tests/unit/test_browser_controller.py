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
    spatial = (STATIC / "spatial_navigation.js").read_text(encoding="utf-8")
    app = (STATIC / "app.js").read_text(encoding="utf-8")
    diagnostics = (STATIC / "diagnostics.js").read_text(encoding="utf-8")
    css = (STATIC / "app.css").read_text(encoding="utf-8")
    server = (ROOT / "src" / "romcloud" / "web" / "server.py").read_text(encoding="utf-8")

    assert html.index('/controller.js') < html.index('/spatial_navigation.js') < html.index('/app.js')
    assert '"/spatial_navigation.js"' in server
    assert "chooseSpatialTarget" in spatial
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
    assert "compatibleGamepad" in javascript
    assert "pushContext" in javascript and "popContext" in javascript
    for zone in ("diagnostic-nav", "diagnostic-filters", "diagnostic-list", "diagnostic-actions"):
        assert f'data-controller-zone="{zone}"' in html or zone in diagnostics
    assert "romcloudGamepad.pushContext" in diagnostics
    assert "romcloudGamepad.popContext" in diagnostics
    assert 'get("view") === "diagnostics"' in app
    # Shared LAN navigation between Library and Diagnostics (touch + controller).
    assert 'id="nav-library"' in html and 'id="nav-diagnostics"' in html
    assert 'data-controller-zone="global"' in html
    assert "openDiagnostics" in app and "showLibrary" in app
    assert '$("nav-library").addEventListener("click", showLibrary)' in app
    assert '$("nav-diagnostics").addEventListener("click", openDiagnostics)' in app
    assert "DiagnosticsBrowser" in app
    assert "close()" in diagnostics
    assert 'typeof this.exitLocal === "function"' in diagnostics


def test_game_and_diagnostics_are_consumers_of_the_shared_browser_navigator() -> None:
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    app = (STATIC / "app.js").read_text(encoding="utf-8")
    diagnostics = (STATIC / "diagnostics.js").read_text(encoding="utf-8")
    controller = (STATIC / "controller.js").read_text(encoding="utf-8")

    assert 'row.dataset.controllerZone = "games"' in app
    assert 'dataset.controllerZone = "diagnostic-list"' in diagnostics
    assert "class BrowserGamepadNavigator" in controller
    assert "class DiagnosticsBrowser" in diagnostics
    assert "keydown" not in diagnostics or "event.key === \"Enter\"" in diagnostics
    assert "scrollIntoView" in controller
    assert "RepeatButton" in controller


def test_downloads_view_renders_durable_states_controls_and_controller_zones() -> None:
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    app = (STATIC / "app.js").read_text(encoding="utf-8")
    css = (STATIC / "app.css").read_text(encoding="utf-8")

    assert 'id="nav-downloads"' in html and 'id="downloads-main"' in html
    assert 'id="job"' in html and 'id="partial-usage"' in html
    assert 'data-controller-zone="download-bulk"' in html
    assert '"download-bulk", "downloads"' in (
        STATIC / "controller.js"
    ).read_text(encoding="utf-8")
    assert 'id="download-selected"' in html and 'data-action="download_selected"' in html
    assert 'id="cancel-all-dialog"' in html and 'id="cancel-all-confirm"' in html
    assert '$("cancel-all-dialog").showModal()' in app
    assert 'button.dataset.controllerZone = "downloads"' in app
    for endpoint in (
        "/api/downloads", "/api/downloads/cancel-all",
        "/api/downloads/retry-all-failed", "/api/downloads/cleanup",
    ):
        assert endpoint in app
    for state in (
        "running", "verifying", "queued", "paused", "interrupted",
        "failed", "cancelled", "complete",
    ):
        assert state in app
    for control in ("pause", "resume", "cancel", "retry", "discard", "remove"):
        assert f'item, "{control}"' in app
    assert "retained_files" in app and "remaining_files" in app
    assert "staging_bytes" in app and "active_reserved_growth" in app
    assert ".downloads-list" in css and ".download-item" in css


def test_remote_navigation_and_diagnostics_reuse_single_manager_view() -> None:
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    app = (STATIC / "app.js").read_text(encoding="utf-8")
    css = (STATIC / "app.css").read_text(encoding="utf-8")

    # One shell hosts both views; navigation never reloads the page or server.
    assert 'id="library-main"' in html and 'id="diagnostics-main"' in html
    assert "location.assign" not in app and "location.href =" not in app
    # Diagnostics opens for any authenticated session, not only controller-first.
    assert 'get("view") === "diagnostics"' in app
    assert 'state.controllerFirst && new URLSearchParams(location.search).get("view")' not in app
    # Nav buttons are real buttons (mouse/touch) and controller focus targets.
    assert html.index('id="nav-library"') < html.index('id="nav-diagnostics"')
    assert 'classList.toggle("active"' in app
    # Active-view affordance exists and both buttons share the header zone grid.
    assert "header .icon-button.active" in css
    assert html.count('data-controller-zone="global"') >= 3


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


def test_diagnostics_navigation_executes_in_chromium_when_available(tmp_path: Path) -> None:
    candidates = [
        shutil.which("chromium"), shutil.which("google-chrome"), shutil.which("chrome"),
        Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
        Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"),
    ]
    browser = next((str(path) for path in candidates if path and Path(path).is_file()), None)
    if browser is None:
        pytest.skip("Chromium is not installed on this development host")
    harness = (ROOT / "tests" / "js" / "diagnostics_browser_harness.html").resolve().as_uri()
    result = subprocess.run(
        [
            browser, "--headless=new", "--disable-gpu", "--no-sandbox",
            f"--user-data-dir={tmp_path / 'diagnostics-profile'}",
            "--virtual-time-budget=1000", "--dump-dom", harness,
        ],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=20, check=False,
    )
    diagnostic = result.stdout + result.stderr
    if result.returncode and "crashpad" in diagnostic and "Operation not permitted" in diagnostic:
        pytest.skip("Chromium crash reporter is blocked by this test sandbox")
    assert result.returncode == 0, diagnostic
    assert 'data-result="passed"' in result.stdout, diagnostic
