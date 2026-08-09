"""Unit tests for `ports_gfx.client` — the subprocess/JSON boundary between
the graphical Ports UI (system Python) and ROMCloud's backend (venv-only).

Uses an injectable `run` callable throughout — no real subprocess, no real
`romcloud` binary — so these tests run fully offline and fast.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass

from ports_gfx.client import BackendResult, call_backend


@dataclass
class _FakeCompletedProcess:
    stdout: str = ""
    stderr: str = ""
    returncode: int = 0


class TestSuccessfulCall:
    def test_parses_ok_payload(self):
        captured = {}

        def fake_run(argv, **kwargs):
            captured["argv"] = argv
            captured["kwargs"] = kwargs
            return _FakeCompletedProcess(stdout='{"ok": true, "games_total": 3}\n')

        result = call_backend("/opt/romcloud/bin/romcloud", "status", run=fake_run)

        assert result == BackendResult(ok=True, data={"ok": True, "games_total": 3})
        assert captured["argv"] == ["/opt/romcloud/bin/romcloud", "uidata", "status"]
        assert captured["kwargs"]["capture_output"] is True
        assert captured["kwargs"]["text"] is True
        assert captured["kwargs"]["timeout"] == 20.0

    def test_uses_last_line_if_extra_output_present(self):
        def fake_run(argv, **kwargs):
            return _FakeCompletedProcess(stdout='stray line\n{"ok": true, "x": 1}\n')

        result = call_backend("romcloud", "status", run=fake_run)

        assert result.ok is True
        assert result.data == {"ok": True, "x": 1}

    def test_refresh_uses_longer_default_timeout(self):
        captured = {}

        def fake_run(argv, **kwargs):
            captured["kwargs"] = kwargs
            return _FakeCompletedProcess(stdout='{"ok": true, "added": 0}\n')

        result = call_backend("romcloud", "refresh", run=fake_run)

        assert result.ok is True
        assert captured["kwargs"]["timeout"] == 120.0


class TestBackendReportedFailure:
    def test_ok_false_payload_becomes_error_result(self):
        def fake_run(argv, **kwargs):
            return _FakeCompletedProcess(stdout='{"ok": false, "error": "boom"}\n', returncode=1)

        result = call_backend("romcloud", "refresh", run=fake_run)

        assert result.ok is False
        assert result.error == "boom"
        assert result.data == {"ok": False, "error": "boom"}


class TestTransportFailures:
    def test_missing_binary_is_reported_not_raised(self):
        def fake_run(argv, **kwargs):
            raise OSError("no such file or directory")

        result = call_backend("/nonexistent/romcloud", "status", run=fake_run)

        assert result.ok is False
        assert "no such file" in result.error

    def test_timeout_is_reported_not_raised(self):
        captured = {}

        def fake_run(argv, **kwargs):
            captured["timeout"] = kwargs.get("timeout")
            raise subprocess.TimeoutExpired(cmd=argv, timeout=kwargs.get("timeout", 1))

        result = call_backend("romcloud", "refresh", run=fake_run)

        assert result.ok is False
        assert result.data == {}
        assert captured["timeout"] == 120.0
        assert "120" in result.error

    def test_explicit_timeout_override_still_wins(self):
        captured = {}

        def fake_run(argv, **kwargs):
            captured["timeout"] = kwargs.get("timeout")
            return _FakeCompletedProcess(stdout='{"ok": true}\n')

        result = call_backend("romcloud", "status", run=fake_run, timeout=1)

        assert result.ok is True
        assert captured["timeout"] == 1

    def test_unexpected_exception_is_reported_not_raised(self):
        def fake_run(argv, **kwargs):
            raise ValueError("something else entirely")

        result = call_backend("romcloud", "status", run=fake_run)

        assert result.ok is False
        assert "something else entirely" in result.error

    def test_empty_stdout_is_reported_with_stderr_detail(self):
        def fake_run(argv, **kwargs):
            return _FakeCompletedProcess(stdout="", stderr="segfault", returncode=139)

        result = call_backend("romcloud", "status", run=fake_run)

        assert result.ok is False
        assert "segfault" in result.error

    def test_malformed_json_is_reported_not_raised(self):
        def fake_run(argv, **kwargs):
            return _FakeCompletedProcess(stdout="not json at all")

        result = call_backend("romcloud", "status", run=fake_run)

        assert result.ok is False
        assert "malformed" in result.error

    def test_non_dict_json_is_rejected(self):
        def fake_run(argv, **kwargs):
            return _FakeCompletedProcess(stdout="[1, 2, 3]")

        result = call_backend("romcloud", "status", run=fake_run)

        assert result.ok is False
        assert "unexpected response shape" in result.error
