"""Unit tests for `romcloud.ui.savesync_progress` — the venv-side bridge
that drives the gameStop Auto SaveSync progress popup subprocess.

Uses an injectable fake Popen — no real subprocess, no real pygame.
"""

from __future__ import annotations

import json

from romcloud.ui.savesync_progress import (
    NullSaveSyncProgress,
    SaveSyncProgressReporter,
    start_savesync_progress,
)


class _FakeStdin:
    def __init__(self, *, raises: bool = False) -> None:
        self.lines: list[str] = []
        self.closed = False
        self._raises = raises

    def write(self, s: str) -> None:
        if self._raises:
            raise BrokenPipeError("broken pipe")
        self.lines.append(s)

    def flush(self) -> None:
        pass

    def close(self) -> None:
        self.closed = True

    @property
    def events(self) -> list[dict]:
        return [json.loads(line) for line in self.lines]


class _FakeProcess:
    def __init__(self, *, stdin_raises: bool = False) -> None:
        self.stdin = _FakeStdin(raises=stdin_raises)
        self.wait_calls: list[float] = []
        self.terminated = False
        self.killed = False

    def wait(self, timeout=None):
        self.wait_calls.append(timeout)
        return 0


class TestStartSavesyncProgress:
    def test_returns_null_reporter_when_launcher_is_none(self):
        assert isinstance(start_savesync_progress(None), NullSaveSyncProgress)

    def test_returns_null_reporter_when_launcher_is_missing(self, tmp_path):
        missing = tmp_path / "romcloud-ports"
        assert isinstance(start_savesync_progress(missing), NullSaveSyncProgress)

    def test_returns_null_reporter_when_subprocess_launch_fails(self, tmp_path):
        launcher = tmp_path / "romcloud-ports"
        launcher.write_text("#!/bin/bash\n")

        def fake_popen(*_a, **_k):
            raise OSError("no such file")

        reporter = start_savesync_progress(launcher, popen=fake_popen)
        assert isinstance(reporter, NullSaveSyncProgress)

    def test_returns_real_reporter_and_sends_savesync_progress_flag(self, tmp_path):
        launcher = tmp_path / "romcloud-ports"
        launcher.write_text("#!/bin/bash\n")
        proc = _FakeProcess()
        seen_argv = {}

        def fake_popen(argv, **kwargs):
            seen_argv["argv"] = argv
            return proc

        reporter = start_savesync_progress(launcher, popen=fake_popen)
        assert isinstance(reporter, SaveSyncProgressReporter)
        assert seen_argv["argv"] == [str(launcher), "--savesync-progress"]


class TestNullSaveSyncProgress:
    def test_stage_and_close_are_safe_no_ops(self):
        progress = NullSaveSyncProgress()
        progress.stage("Checking save changes…")
        progress.close(True, "Save sync complete.")
        progress.close(False, "Save sync failed.")  # idempotent-safe, no raise


class TestSaveSyncProgressReporter:
    def test_stage_sends_ndjson_event(self):
        proc = _FakeProcess()
        reporter = SaveSyncProgressReporter(proc)
        reporter.stage("Checking save changes…")
        assert proc.stdin.events == [{"stage": "Checking save changes…"}]

    def test_close_sends_done_event_and_closes_stdin(self):
        proc = _FakeProcess()
        reporter = SaveSyncProgressReporter(proc)
        reporter.close(True, "Save sync complete.")
        assert proc.stdin.events == [
            {"event": "done", "ok": True, "message": "Save sync complete."}
        ]
        assert proc.stdin.closed

    def test_successful_close_does_not_block_on_the_cosmetic_dismiss_fade(self):
        """The SaveSync transaction is durably committed before a successful
        close is reported, so the lifecycle hook (and therefore
        EmulationStation) must not additionally wait out the popup's
        purely cosmetic success fade on every single game exit."""
        proc = _FakeProcess()
        reporter = SaveSyncProgressReporter(proc)
        reporter.close(True, "Save sync complete.")
        assert proc.wait_calls == []
        assert proc.stdin.closed

    def test_failed_close_still_waits_so_the_message_is_readable(self):
        proc = _FakeProcess()
        reporter = SaveSyncProgressReporter(proc)
        reporter.close(False, "Save sync failed.")
        assert proc.wait_calls

    def test_close_without_message_omits_the_field(self):
        proc = _FakeProcess()
        reporter = SaveSyncProgressReporter(proc)
        reporter.close(True)
        assert proc.stdin.events == [{"event": "done", "ok": True}]

    def test_close_is_idempotent(self):
        proc = _FakeProcess()
        reporter = SaveSyncProgressReporter(proc)
        reporter.close(False, "Save sync failed.")
        reporter.close(True, "Save sync complete.")
        assert len(proc.stdin.events) == 1
        assert len(proc.wait_calls) == 1

    def test_broken_pipe_during_stage_never_raises(self):
        proc = _FakeProcess(stdin_raises=True)
        reporter = SaveSyncProgressReporter(proc)
        reporter.stage("Checking save changes…")  # must not raise

    def test_broken_pipe_during_close_never_raises_and_still_waits(self):
        proc = _FakeProcess(stdin_raises=True)
        reporter = SaveSyncProgressReporter(proc)
        reporter.close(False, "Save sync failed.")  # must not raise
        assert proc.wait_calls
