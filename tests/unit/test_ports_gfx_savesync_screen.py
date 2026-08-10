"""Unit tests for ports_gfx.savesync_screen — the SaveSync GUI screen state
(pure logic; no pygame). Backend calls are faked at the subprocess boundary
(same convention as test_ports_gfx_operation.py's `fake_popen`), so these
tests exercise the real polling/parsing path without a real `romcloud`
binary."""

from __future__ import annotations

import json

from ports_gfx.actions import Action
from ports_gfx.input_manager import InputEvent
from ports_gfx.savesync_screen import (
    APPLYING_SETTINGS,
    COMMITTING,
    CONFIRMING,
    DASHBOARD,
    PREVIEW,
    PREVIEWING,
    RESULT,
    SETTINGS,
    SaveSyncScreenState,
)


class _FakeStdin:
    def write(self, text: str) -> None:
        pass

    def close(self) -> None:
        pass


class _FakeProcess:
    def __init__(self, stdout: str, stderr: str = "") -> None:
        self._stdout_lines = iter(stdout.splitlines(keepends=True) + [""])
        self._stderr_lines = iter(stderr.splitlines(keepends=True) + [""])
        self.stdout = self
        self.stderr = _FakeStderr(stderr)
        self.stdin = _FakeStdin()
        self.returncode = 0

    def readline(self) -> str:
        return next(self._stdout_lines, "")

    def close(self) -> None:
        pass

    def poll(self):
        return self.returncode


class _FakeStderr:
    def __init__(self, text: str) -> None:
        self._lines = iter(text.splitlines(keepends=True) + [""])

    def readline(self) -> str:
        return next(self._lines, "")

    def close(self) -> None:
        pass


def _fake_popen_returning(payload: dict):
    def fake_popen(argv, **kwargs):
        return _FakeProcess(json.dumps(payload) + "\n")

    return fake_popen


def _drain(state: SaveSyncScreenState, *, max_iterations: int = 50) -> None:
    for _ in range(max_iterations):
        state.poll()
        if state._runner is None:  # noqa: SLF001 - test-only introspection
            return


class TestDashboardSelection:
    def test_select_clamps_to_valid_range(self):
        state = SaveSyncScreenState(romcloud_bin="romcloud")
        state.select(-5)
        assert state.selected_index == 0
        state.select(999)
        assert state.selected_index == 3

    def test_back_returns_sentinel(self):
        state = SaveSyncScreenState(romcloud_bin="romcloud", selected_index=3)
        assert state.confirm_dashboard_selection() == "back"

    def test_settings_switches_step(self):
        state = SaveSyncScreenState(romcloud_bin="romcloud", selected_index=2)
        state.confirm_dashboard_selection()
        assert state.step == SETTINGS

    def test_upload_starts_preview(self):
        state = SaveSyncScreenState(romcloud_bin="romcloud", selected_index=0)
        state.popen = _fake_popen_returning({"ok": True, "diff": {"direction": "upload", "entries": []}})
        state.confirm_dashboard_selection()
        assert state.step == PREVIEWING
        assert state.direction == "upload"


class TestPreviewFlow:
    def test_successful_preview_moves_to_preview_step(self):
        state = SaveSyncScreenState(romcloud_bin="romcloud")
        state.popen = _fake_popen_returning(
            {
                "ok": True,
                "diff": {"direction": "upload", "entries": [{"relative_path": "psx/Game.srm", "change": "added", "local": None, "remote": None}]},
                "added": 1, "changed": 0, "removed": 0, "unchanged": 0, "transfer_bytes": 100,
            }
        )
        state.start_preview("upload")
        assert state.step == PREVIEWING

        _drain(state)

        assert state.step == PREVIEW
        assert state.preview_summary["added"] == 1
        assert state.diff["entries"][0]["relative_path"] == "psx/Game.srm"

    def test_failed_preview_returns_to_dashboard_with_error(self):
        state = SaveSyncScreenState(romcloud_bin="romcloud")
        state.popen = _fake_popen_returning({"ok": False, "error": "Remote save location is not reachable"})
        state.start_preview("upload")

        _drain(state)

        assert state.step == DASHBOARD
        assert "not reachable" in state.error

    def test_poll_without_pending_operation_is_a_no_op(self):
        state = SaveSyncScreenState(romcloud_bin="romcloud")
        state.poll()  # must not raise
        assert state.step == DASHBOARD


class TestConfirmFlow:
    def test_begin_confirm_resets_hold_state(self):
        state = SaveSyncScreenState(romcloud_bin="romcloud")
        state.begin_confirm()
        assert state.step == CONFIRMING
        assert state.confirm.confirmed is False
        assert state.confirm.progress == 0.0

    def test_holding_confirm_for_full_duration_starts_commit(self):
        state = SaveSyncScreenState(romcloud_bin="romcloud", diff={"direction": "upload", "entries": []})
        state.direction = "upload"
        state.popen = _fake_popen_returning({"ok": True, "record": {"revision": "abc", "timestamp": "t", "device_id": "d", "artifact_count": 0, "total_bytes": 0}})
        state.begin_confirm()

        state.handle_confirm_event(InputEvent(action=Action.CONFIRM))
        state.update_confirm(1.5)
        assert state.step == CONFIRMING
        state.update_confirm(1.5)

        assert state.step == COMMITTING

    def test_releasing_before_duration_does_not_commit(self):
        state = SaveSyncScreenState(romcloud_bin="romcloud")
        state.begin_confirm()
        state.handle_confirm_event(InputEvent(action=Action.CONFIRM))
        state.update_confirm(2.0)
        state.handle_confirm_event(InputEvent(action=Action.CONFIRM_RELEASED))
        state.update_confirm(2.0)
        assert state.step == CONFIRMING

    def test_cancel_returns_to_preview_without_committing(self):
        state = SaveSyncScreenState(romcloud_bin="romcloud")
        state.begin_confirm()
        state.handle_confirm_event(InputEvent(action=Action.CONFIRM))
        state.update_confirm(1.0)
        state.handle_confirm_event(InputEvent(action=Action.BACK))
        state.update_confirm(0.1)
        assert state.step == PREVIEW


class TestCommitResult:
    def test_successful_commit_populates_result(self):
        state = SaveSyncScreenState(romcloud_bin="romcloud", direction="upload", diff={"direction": "upload", "entries": []})
        state.popen = _fake_popen_returning(
            {"ok": True, "record": {"revision": "rev-1", "timestamp": "t", "device_id": "d", "artifact_count": 3, "total_bytes": 900}}
        )
        state._start_operation("savesync-commit", {"direction": "upload", "diff": state.diff})  # noqa: SLF001
        state.step = COMMITTING

        _drain(state)

        assert state.step == RESULT
        assert state.result["revision"] == "rev-1"
        assert state.error == ""

    def test_failed_commit_records_error(self):
        state = SaveSyncScreenState(romcloud_bin="romcloud", direction="upload", diff={"direction": "upload", "entries": []})
        state.popen = _fake_popen_returning({"ok": False, "error": "Content changed since preview"})
        state._start_operation("savesync-commit", {"direction": "upload", "diff": state.diff})  # noqa: SLF001
        state.step = COMMITTING

        _drain(state)

        assert state.step == RESULT
        assert "changed since preview" in state.error


class TestSettings:
    def test_set_xbox_enabled_applies_and_returns_to_settings(self):
        state = SaveSyncScreenState(romcloud_bin="romcloud")
        state.popen = _fake_popen_returning({"ok": True, "xbox_enabled": True})
        state.set_xbox_enabled(True)
        assert state.step == APPLYING_SETTINGS

        _drain(state)

        assert state.step == SETTINGS
        assert state.status["xbox_enabled"] is True

    def test_return_to_dashboard_resets_selection(self):
        state = SaveSyncScreenState(romcloud_bin="romcloud", step=SETTINGS, selected_index=2)
        state.return_to_dashboard()
        assert state.step == DASHBOARD
        assert state.selected_index == 0
