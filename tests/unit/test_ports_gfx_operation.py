"""Unit tests for `ports_gfx.operation` — the non-blocking subprocess
runner backing the reusable long-running operation screen.

Uses real short-lived ``python -c`` subprocesses (same convention as
`tests/unit/test_mount_worker.py`'s stale-lock test) so the actual
threaded stdout/stderr pump and non-blocking poll loop are exercised for
real, not mocked away.
"""

from __future__ import annotations

import os
import signal
import sys
import time

import pytest

from ports_gfx.operation import OperationLine, OperationRunner, OperationState


def _drain_until_finished(runner: OperationRunner, *, timeout: float = 5.0) -> list[OperationLine]:
    deadline = time.monotonic() + timeout
    collected: list[OperationLine] = []
    while time.monotonic() < deadline:
        collected.extend(runner.poll())
        if runner.is_finished:
            break
        time.sleep(0.01)
    return collected


class TestStartDoesNotBlock:
    def test_state_is_running_immediately_after_start_for_a_slow_process(self):
        runner = OperationRunner([sys.executable, "-c", "import time; time.sleep(0.3)"])
        started_at = time.monotonic()
        runner.start()
        elapsed = time.monotonic() - started_at

        assert elapsed < 0.2  # start() itself must return almost immediately
        assert runner.state == OperationState.RUNNING
        assert runner.is_finished is False

        _drain_until_finished(runner)
        assert runner.state == OperationState.SUCCEEDED

    def test_calling_start_twice_is_a_no_op(self):
        runner = OperationRunner([sys.executable, "-c", "pass"])
        runner.start()
        first_process = runner._process  # noqa: SLF001 - white-box check of the no-op guard
        runner.start()
        assert runner._process is first_process  # noqa: SLF001
        _drain_until_finished(runner)


class TestIncrementalOutputCapture:
    def test_stdout_lines_are_captured_as_they_arrive(self):
        script = (
            "import time\n"
            "for i in range(3):\n"
            "    print(f'line{i}', flush=True)\n"
            "    time.sleep(0.02)\n"
        )
        runner = OperationRunner([sys.executable, "-c", script])
        runner.start()
        collected = _drain_until_finished(runner)

        stdout_texts = [line.text for line in collected if line.stream == "stdout"]
        assert stdout_texts == ["line0", "line1", "line2"]
        assert runner.state == OperationState.SUCCEEDED
        assert runner.returncode == 0

    def test_stderr_lines_are_captured_and_tagged(self):
        script = "import sys\nprint('oops', file=sys.stderr, flush=True)\n"
        runner = OperationRunner([sys.executable, "-c", script])
        runner.start()
        collected = _drain_until_finished(runner)

        stderr_lines = [line for line in collected if line.stream == "stderr"]
        assert any(line.text == "oops" for line in stderr_lines)

    def test_optional_stdin_is_sent_without_appearing_in_argv(self):
        script = "import sys; print(sys.stdin.read())"
        runner = OperationRunner(
            [sys.executable, "-c", script],
            stdin_text='{"password":"private"}',
        )
        runner.start()
        collected = _drain_until_finished(runner)
        assert [line.text for line in collected if line.stream == "stdout"] == ['{"password":"private"}']
        assert "private" not in runner._argv  # noqa: SLF001 - credential boundary assertion


class TestExitStates:
    def test_zero_exit_is_succeeded(self):
        runner = OperationRunner([sys.executable, "-c", "pass"])
        runner.start()
        _drain_until_finished(runner)
        assert runner.state == OperationState.SUCCEEDED
        assert runner.returncode == 0
        assert runner.error == ""

    def test_nonzero_exit_is_failed_with_error_detail(self):
        runner = OperationRunner([sys.executable, "-c", "import sys; sys.exit(7)"])
        runner.start()
        _drain_until_finished(runner)
        assert runner.state == OperationState.FAILED
        assert runner.returncode == 7
        assert "7" in runner.error

    def test_missing_binary_is_reported_failed_not_raised(self):
        runner = OperationRunner(["/nonexistent/romcloud-binary-xyz"])
        runner.start()  # must not raise
        assert runner.state == OperationState.FAILED
        assert runner.error != ""
        assert runner.is_finished is True


class TestNoArbitraryTimeout:
    def test_popen_is_never_called_with_a_timeout_kwarg(self):
        captured = {}

        class _FakeProcess:
            def __init__(self) -> None:
                self.stdout = None
                self.stderr = None
                self.returncode = 0

            def poll(self):
                return 0

        def fake_popen(argv, **kwargs):
            captured["kwargs"] = kwargs
            return _FakeProcess()

        runner = OperationRunner(["romcloud", "refresh"], popen=fake_popen)
        runner.start()
        runner.poll()

        assert "timeout" not in captured["kwargs"]
        assert runner.state == OperationState.SUCCEEDED

    def test_long_running_process_outlives_the_old_120_second_timeout_default(self):
        # Regression guard: previously the graphical refresh used a fixed
        # 120s subprocess timeout. A slow-but-successful process must
        # still be reported as succeeded, never as a timeout failure,
        # regardless of how long it legitimately runs.
        runner = OperationRunner([sys.executable, "-c", "import time; time.sleep(0.05)"])
        runner.start()
        _drain_until_finished(runner)
        assert runner.state == OperationState.SUCCEEDED
        assert "timeout" not in runner.error.lower()


class TestBoundedHistory:
    def test_history_is_capped_at_max_lines(self):
        script = "\n".join(f"print('line{i}')" for i in range(10))
        runner = OperationRunner([sys.executable, "-c", script], max_lines=3)
        runner.start()
        _drain_until_finished(runner)

        assert len(runner.lines) == 3
        assert [line.text for line in runner.lines] == ["line7", "line8", "line9"]


class TestPollAfterFinished:
    def test_polling_again_after_finished_returns_no_new_lines(self):
        runner = OperationRunner([sys.executable, "-c", "print('done')"])
        runner.start()
        _drain_until_finished(runner)
        assert runner.poll() == []


class TestCancellationAndDeadlines:
    def test_cancel_reaps_a_running_owned_process(self):
        runner = OperationRunner(
            [sys.executable, "-c", "import time; time.sleep(30)"]
        )
        runner.start()
        process = runner._process  # noqa: SLF001 - ownership assertion

        runner.cancel(grace_period=0.5)

        assert runner.state == OperationState.FAILED
        assert runner.error == "cancelled"
        assert process is not None
        assert process.poll() is not None

    def test_explicit_network_deadline_cancels_with_actionable_error(self):
        ticks = iter([0.0, 2.0])
        runner = OperationRunner(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            max_runtime=1.0,
            timeout_message="network operation timed out; check connectivity",
            clock=lambda: next(ticks),
        )
        runner.start()

        runner.poll()

        assert runner.state == OperationState.FAILED
        assert runner.error == "network operation timed out; check connectivity"

    @pytest.mark.skipif(os.name != "posix", reason="process groups are POSIX ownership")
    def test_cancel_signals_the_owned_process_group(self, monkeypatch):
        calls = []

        class FakeProcess:
            pid = 4242
            stdout = None
            stderr = None
            returncode = None

            def wait(self, timeout):
                self.returncode = -signal.SIGTERM

        monkeypatch.setattr("ports_gfx.operation.os.getpgid", lambda pid: pid)
        monkeypatch.setattr(
            "ports_gfx.operation.os.killpg",
            lambda pgid, sig: calls.append((pgid, sig)),
        )
        runner = OperationRunner(
            ["romcloud", "uidata", "connection-mount"],
            popen=lambda *a, **k: FakeProcess(),
        )
        runner.start()

        runner.cancel()

        assert calls == [(4242, signal.SIGTERM)]
