from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from romcloud.web import lifecycle


def test_unavailable_browser_reports_clear_error(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        lifecycle,
        "discover_local_browser",
        lambda **kwargs: {"browser": None, "diagnostics": []},
    )
    with pytest.raises(RuntimeError, match="Chromium-compatible"):
        lifecycle.launch_local_browser(tmp_path)


def test_local_back_exit_terminates_browser_but_not_manager(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        lifecycle,
        "manager_status",
        lambda data_path: {"local_url": "https://127.0.0.1:8765/"},
    )
    monkeypatch.setattr(
        "romcloud.web.tls.manager_certificate_spki_pin", lambda data_path: "pin"
    )
    requests = []
    def request(data_path, path, **kwargs):
        requests.append(path)
        if path == "/api/auth/local-launch":
            return {"launch_id": "launch-one"}
        return {"exit_requested": True}

    monkeypatch.setattr(lifecycle, "_manager_request", request)

    class Process:
        returncode = None
        terminated = False

        def poll(self):
            return self.returncode

        def terminate(self):
            self.terminated = True
            self.returncode = 0

        def wait(self, timeout):
            return self.returncode

    process = Process()
    captured = {}

    def popen(argv, **kwargs):
        captured["argv"] = argv
        captured.update(kwargs)
        return process

    result = lifecycle.launch_local_browser(
        tmp_path, browser="/usr/bin/chromium", popen=popen, sleep=lambda _: None
    )
    assert process.terminated
    assert requests == ["/api/auth/local-launch", "/api/local-session-status/launch-one"]
    assert "--kiosk" in captured["argv"]
    assert "--no-sandbox" not in captured["argv"]
    assert "permanent" not in " ".join(captured["argv"])
    assert captured["argv"][-1].endswith("?interaction=controller")
    assert captured["env"] == dict(captured["env"])
    assert result["closed"] is True


def test_known_batocera_appimage_is_capability_validated(tmp_path: Path) -> None:
    appimage = tmp_path / "GoogleChrome.AppImage"
    appimage.write_text("browser")
    appimage.chmod(0o755)
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        return SimpleNamespace(returncode=0, stdout="Google Chrome 140.0.1", stderr="")

    result = lifecycle.discover_local_browser(
        data_path=None,
        which=lambda _: None,
        run=run,
        configured="",
        persistent_paths=[appimage],
    )
    assert result["browser"]["path"] == str(appimage)
    assert result["browser"]["ownership"] == "user-installed"
    assert calls == [[str(appimage), "--version"]]


def test_explicit_browser_precedes_path_and_persistent_candidates(tmp_path: Path) -> None:
    configured = tmp_path / "configured-chrome"
    configured.write_text("browser")
    configured.chmod(0o755)
    probed = []

    def run(argv, **kwargs):
        probed.append(argv[0])
        return SimpleNamespace(returncode=0, stdout="Google Chrome 140", stderr="")

    result = lifecycle.discover_local_browser(
        data_path=None,
        which=lambda _: "/unused/path-browser",
        run=run,
        configured=str(configured),
        persistent_paths=[tmp_path / "unused.AppImage"],
    )
    assert result["browser"]["path"] == str(configured)
    assert probed == [str(configured)]


def test_invalid_candidate_falls_through_in_documented_order(tmp_path: Path) -> None:
    path_browser = tmp_path / "chromium"
    appimage = tmp_path / "GoogleChrome.AppImage"
    for item in (path_browser, appimage):
        item.write_text("browser")
        item.chmod(0o755)

    def run(argv, **kwargs):
        if argv[0] == str(path_browser):
            return SimpleNamespace(returncode=1, stdout="", stderr="broken runtime")
        return SimpleNamespace(returncode=0, stdout="Chromium 140.0", stderr="")

    result = lifecycle.discover_local_browser(
        data_path=None,
        which=lambda name: str(path_browser) if name == "chromium" else None,
        run=run,
        configured="",
        persistent_paths=[appimage],
    )
    assert result["browser"]["path"] == str(appimage)
    assert "exited 1" in result["diagnostics"][0]["reason"]
    assert result["diagnostics"][-1]["compatible"] is True


def test_user_browser_precedes_managed_runtime(tmp_path: Path) -> None:
    data = tmp_path / "data"
    user_browser = tmp_path / "chromium"
    user_browser.write_text("browser")
    user_browser.chmod(0o755)
    managed = tmp_path / "browser" / "versions" / "1" / "chrome"
    managed.parent.mkdir(parents=True)
    managed.write_text("browser")
    managed.chmod(0o755)
    (tmp_path / "browser" / "current.json").write_text(
        '{"version":"1","executable":"chrome"}'
    )

    result = lifecycle.discover_local_browser(
        data_path=data,
        which=lambda name: str(user_browser) if name == "chromium" else None,
        run=lambda *args, **kwargs: SimpleNamespace(
            returncode=0, stdout="Chromium 140", stderr=""
        ),
        configured="",
        persistent_paths=[],
    )
    assert result["browser"]["path"] == str(user_browser)
    assert result["browser"]["ownership"] == "user-installed"


def test_repeated_local_launch_is_singleton(tmp_path: Path) -> None:
    with lifecycle._local_browser_lock(tmp_path):
        with pytest.raises(RuntimeError, match="already running"):
            with lifecycle._local_browser_lock(tmp_path):
                pass


def test_browser_start_failure_is_persistently_logged_and_surfaced(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(
        lifecycle,
        "manager_status",
        lambda data_path: {"local_url": "https://127.0.0.1:8765/"},
    )
    monkeypatch.setattr(
        lifecycle,
        "_manager_request",
        lambda *args, **kwargs: {"launch_id": "launch-failed"},
    )
    monkeypatch.setattr(
        "romcloud.web.tls.manager_certificate_spki_pin", lambda data_path: "pin"
    )

    class FailedProcess:
        def poll(self):
            return 23

    with pytest.raises(RuntimeError, match="status 23"):
        lifecycle.launch_local_browser(
            tmp_path / "data",
            browser="/usr/bin/chromium",
            popen=lambda *args, **kwargs: FailedProcess(),
            sleep=lambda _: None,
        )
    log = tmp_path / "logs" / "browser-open.log"
    assert "browser exited status=23" in log.read_text()


def test_root_sandbox_refusal_requires_explicit_user_browser_opt_in(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(
        lifecycle,
        "discover_local_browser",
        lambda **kwargs: {
            "browser": {
                "path": "/userdata/system/add-ons/google-chrome/GoogleChrome.AppImage",
                "source": "Batocera persistent add-on",
                "ownership": "user-installed",
                "compatible": True,
            },
            "diagnostics": [],
        },
    )
    monkeypatch.setattr(
        lifecycle,
        "manager_status",
        lambda data_path: {"local_url": "https://127.0.0.1:8765/"},
    )
    monkeypatch.setattr(
        lifecycle,
        "_manager_request",
        lambda *args, **kwargs: {"launch_id": "launch-failed"},
    )
    monkeypatch.setattr(
        "romcloud.web.tls.manager_certificate_spki_pin", lambda data_path: "pin"
    )

    class FailedProcess:
        def poll(self):
            return 1

    def popen(argv, **kwargs):
        kwargs["stdout"].write(
            b"Running as root without --no-sandbox is not supported.\n"
        )
        kwargs["stdout"].flush()
        return FailedProcess()

    with pytest.raises(RuntimeError, match="process isolation"):
        lifecycle.launch_local_browser(
            tmp_path / "data", popen=popen, sleep=lambda _: None
        )


def test_no_sandbox_flag_is_limited_to_explicit_user_browser_fallback(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(
        lifecycle,
        "discover_local_browser",
        lambda **kwargs: {
            "browser": {
                "path": "/userdata/user-chrome.AppImage",
                "source": "Batocera persistent add-on",
                "ownership": "user-installed",
            },
            "diagnostics": [],
        },
    )
    monkeypatch.setattr(
        lifecycle,
        "manager_status",
        lambda data_path: {"local_url": "https://127.0.0.1:8765/"},
    )
    requests = []

    def request(data_path, path, **kwargs):
        requests.append(path)
        return {"launch_id": "launch"} if path == "/api/auth/local-launch" else {"exit_requested": True}

    monkeypatch.setattr(lifecycle, "_manager_request", request)
    monkeypatch.setattr(
        "romcloud.web.tls.manager_certificate_spki_pin", lambda data_path: "pin"
    )

    class Process:
        returncode = None

        def poll(self):
            return self.returncode

        def terminate(self):
            self.returncode = 0

        def wait(self, timeout):
            return self.returncode

    captured = {}
    lifecycle.launch_local_browser(
        tmp_path / "data",
        allow_no_sandbox=True,
        popen=lambda argv, **kwargs: (captured.setdefault("argv", argv), Process())[1],
        sleep=lambda _: None,
    )
    assert captured["argv"][1] == "--no-sandbox"
