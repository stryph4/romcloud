"""Unit tests for `romcloud.ui.graphical_progress` — the venv-side bridge
that drives the graphical (system-Python pygame) cache-miss progress
subprocess over stdin/stdout.

Uses injectable fakes for the subprocess and CacheService — no real
subprocess, no real pygame, no real transfer.
"""

from __future__ import annotations

import json
import subprocess
import time

import pytest

from romcloud.core.exceptions import ROMCloudError
from romcloud.core.models.game import Game, GameAsset
from romcloud.ui.graphical_progress import (
    GraphicalProgressUnavailable,
    _close_subprocess,
    graphical_progress_binary,
    run_graphical_progress_transfer,
)


@pytest.fixture
def dummy_game() -> Game:
    return Game.create(
        system="ps2",
        title="Test Game",
        source_provider="local",
        source_root="/roms",
        assets=[GameAsset("Test Game.iso", "ps2/Test Game.iso", size_bytes=500, is_primary=True)],
    )


class _FakeStdin:
    def __init__(self) -> None:
        self.lines: list[str] = []
        self.closed = False

    def write(self, s: str) -> None:
        self.lines.append(s)

    def flush(self) -> None:
        pass

    def close(self) -> None:
        self.closed = True

    @property
    def events(self) -> list[dict]:
        return [json.loads(line) for line in self.lines]


class _FakeStdout:
    def __init__(self, lines=()) -> None:
        self._lines = iter(lines)

    def readline(self) -> str:
        return next(self._lines, "")


class _FakeProcess:
    def __init__(self, stdout_lines=()) -> None:
        self.stdin = _FakeStdin()
        self.stdout = _FakeStdout(stdout_lines)
        self.wait_calls: list[float] = []
        self.terminated = False
        self.killed = False

    def wait(self, timeout=None):
        self.wait_calls.append(timeout)
        return 0


class _FakeCacheService:
    def __init__(self, *, path="/cache/ps2/game.iso", error: Exception | None = None) -> None:
        self.path = path
        self.error = error

    def cache_game(self, game_id, on_progress=None):
        for i in range(50):
            if on_progress:
                on_progress(i * 10, 500)
            time.sleep(0.005)
        if self.error:
            raise self.error
        return self.path


class TestGraphicalProgressBinary:
    def test_returns_none_when_not_installed(self, tmp_path):
        config = type("_Cfg", (), {"data_path": str(tmp_path / "romcloud" / "data")})()
        assert graphical_progress_binary(config) is None

    def test_returns_path_when_installed(self, tmp_path):
        bin_dir = tmp_path / "romcloud" / "bin"
        bin_dir.mkdir(parents=True)
        wrapper = bin_dir / "romcloud-launch-progress"
        wrapper.write_text("#!/bin/bash\n")
        config = type("_Cfg", (), {"data_path": str(tmp_path / "romcloud" / "data")})()
        assert graphical_progress_binary(config) == wrapper


class TestRunGraphicalProgressTransfer:
    def test_subprocess_launch_failure_is_reported_as_unavailable(self, dummy_game):
        def fake_popen(*a, **k):
            raise OSError("no such file")

        with pytest.raises(GraphicalProgressUnavailable):
            run_graphical_progress_transfer(
                _FakeCacheService(), dummy_game, launcher_bin="/x", popen=fake_popen
            )

    def test_successful_transfer_returns_path_and_sends_events(self, dummy_game):
        proc = _FakeProcess()

        def fake_popen(*a, **k):
            return proc

        cache_service = _FakeCacheService(path="/cache/ps2/game.iso")
        result = run_graphical_progress_transfer(
            cache_service, dummy_game, launcher_bin="/x", popen=fake_popen
        )

        assert result == "/cache/ps2/game.iso"
        events = proc.stdin.events
        assert events[0]["phase"] == "connecting"
        assert any(e.get("phase") == "downloading" for e in events)
        assert events[-1] == {"event": "launching"}
        assert proc.stdin.closed is True
        assert proc.wait_calls  # subprocess exit was waited on before returning

    def test_transfer_failure_sends_error_event_and_reraises(self, dummy_game):
        proc = _FakeProcess()

        def fake_popen(*a, **k):
            return proc

        cache_service = _FakeCacheService(error=ROMCloudError("disk full"))
        with pytest.raises(ROMCloudError, match="disk full"):
            run_graphical_progress_transfer(cache_service, dummy_game, launcher_bin="/x", popen=fake_popen)

        events = proc.stdin.events
        assert events[-1] == {"event": "error", "message": "disk full"}

    def test_cancel_from_ui_raises_keyboard_interrupt_and_sends_error(self, dummy_game):
        proc = _FakeProcess(stdout_lines=["cancel\n"])

        def fake_popen(*a, **k):
            return proc

        cache_service = _FakeCacheService()
        with pytest.raises(KeyboardInterrupt):
            run_graphical_progress_transfer(cache_service, dummy_game, launcher_bin="/x", popen=fake_popen)

        events = proc.stdin.events
        assert events[-1] == {"event": "error", "message": "Cancelled"}

    def test_broken_pipe_while_sending_does_not_crash_transfer(self, dummy_game):
        proc = _FakeProcess()

        def broken_write(_s):
            raise BrokenPipeError("gone")

        proc.stdin.write = broken_write  # type: ignore[method-assign]

        def fake_popen(*a, **k):
            return proc

        result = run_graphical_progress_transfer(
            _FakeCacheService(path="/cache/x.iso"), dummy_game, launcher_bin="/x", popen=fake_popen
        )
        assert result == "/cache/x.iso"


class TestCloseSubprocess:
    def test_waits_then_returns_when_process_exits_promptly(self):
        proc = _FakeProcess()
        _close_subprocess(proc, watcher=_finished_thread())
        assert proc.wait_calls == [5.0]
        assert proc.stdin.closed is True

    def test_terminates_when_wait_times_out(self):
        class _SlowProcess(_FakeProcess):
            def __init__(self):
                super().__init__()
                self.terminated = False
                self.killed = False
                self._calls = 0

            def wait(self, timeout=None):
                self._calls += 1
                self.wait_calls.append(timeout)
                if self._calls == 1:
                    raise subprocess.TimeoutExpired(cmd="x", timeout=timeout)
                return 0

            def terminate(self):
                self.terminated = True

            def kill(self):
                self.killed = True

        proc = _SlowProcess()
        _close_subprocess(proc, watcher=_finished_thread())
        assert proc.terminated is True
        assert proc.killed is False

    def test_kills_when_terminate_also_times_out(self):
        class _StuckProcess(_FakeProcess):
            def __init__(self):
                super().__init__()
                self.terminated = False
                self.killed = False

            def wait(self, timeout=None):
                self.wait_calls.append(timeout)
                raise subprocess.TimeoutExpired(cmd="x", timeout=timeout)

            def terminate(self):
                self.terminated = True

            def kill(self):
                self.killed = True

        proc = _StuckProcess()
        _close_subprocess(proc, watcher=_finished_thread())
        assert proc.terminated is True
        assert proc.killed is True


def _finished_thread():
    import threading

    t = threading.Thread(target=lambda: None)
    t.start()
    t.join()
    return t
