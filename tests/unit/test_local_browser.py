from __future__ import annotations

from pathlib import Path

import pytest

from romcloud.web import lifecycle


def test_unavailable_browser_reports_clear_error(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(lifecycle, "find_local_browser", lambda **kwargs: None)
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
        return process

    result = lifecycle.launch_local_browser(
        tmp_path, browser="/usr/bin/chromium", popen=popen, sleep=lambda _: None
    )
    assert process.terminated
    assert requests == ["/api/auth/local-launch", "/api/local-session-status/launch-one"]
    assert "--kiosk" in captured["argv"]
    assert "permanent" not in " ".join(captured["argv"])
    assert "?" not in captured["argv"][-1]
    assert result["closed"] is True
