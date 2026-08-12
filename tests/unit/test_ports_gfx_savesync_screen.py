"""Unit tests for ports_gfx.savesync_screen — the SaveSync GUI screen state
(pure logic; no pygame). Backend calls are faked at the subprocess boundary
(same convention as test_ports_gfx_operation.py's `fake_popen`), so these
tests exercise the real polling/parsing path without a real `romcloud`
binary."""

from __future__ import annotations

import json

from ports_gfx.actions import Action
from ports_gfx.input_manager import InputEvent
from ports_gfx.operation import OperationLine
from ports_gfx.savesync_screen import (
    APPLYING_SETTINGS,
    COMMITTING,
    CONFIRMING,
    DASHBOARD,
    PREVIEW,
    PREVIEWING,
    REMOTE_AVAILABLE,
    REMOTE_CHECKING,
    REMOTE_UNAVAILABLE,
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


def _fake_popen_by_action(payloads: dict[str, dict], *, pending=None):
    def fake_popen(argv, **kwargs):
        action = argv[-1]
        if action in payloads:
            return _FakeProcess(json.dumps(payloads[action]) + "\n")
        if pending is not None:
            return pending
        raise AssertionError(f"Unexpected backend action: {action}")

    return fake_popen


class _PendingProcess:
    def __init__(self) -> None:
        self.stdout = _FakeStderr("")
        self.stderr = _FakeStderr("")
        self.stdin = _FakeStdin()
        self.returncode = None
        self.terminated = False
        self.killed = False

    def poll(self):
        return self.returncode

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = -15

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9

    def wait(self, timeout=None):
        return self.returncode


class _ControlledRunner:
    def __init__(self, payload: dict, *, finished: bool) -> None:
        self.lines = [OperationLine("stdout", json.dumps(payload))]
        self.is_finished = finished
        self.error = ""
        self.cancelled = False

    def poll(self):
        return []

    def cancel(self):
        self.cancelled = True


def _drain(state: SaveSyncScreenState, *, max_iterations: int = 50) -> None:
    for _ in range(max_iterations):
        state.poll()
        if all(
            runner is None
            for runner in (
                state._runner,  # noqa: SLF001 - test-only introspection
                state._status_runner,  # noqa: SLF001
                state._availability_runner,  # noqa: SLF001
            )
        ):
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
        state = SaveSyncScreenState(
            romcloud_bin="romcloud",
            selected_index=0,
            status={"remote_configured": True},
            remote_availability=REMOTE_AVAILABLE,
        )
        state.popen = _fake_popen_returning({"ok": True, "diff": {"direction": "upload", "entries": []}})
        state.confirm_dashboard_selection()
        assert state.step == PREVIEWING
        assert state.direction == "upload"

    def test_upload_is_unavailable_without_configured_remote_data(self):
        state = SaveSyncScreenState(
            romcloud_bin="romcloud",
            selected_index=0,
            status={"remote_configured": False},
        )

        state.confirm_dashboard_selection()

        assert state.step == DASHBOARD
        assert "Configure writable" in state.error
        assert state._runner is None  # noqa: SLF001

    def test_upload_is_gated_while_remote_check_is_pending(self):
        state = SaveSyncScreenState(
            romcloud_bin="romcloud",
            selected_index=0,
            status={"remote_configured": True},
            remote_availability=REMOTE_CHECKING,
        )

        state.confirm_dashboard_selection()

        assert state.step == DASHBOARD
        assert "still being checked" in state.error
        assert state._runner is None  # noqa: SLF001

    def test_download_is_gated_when_remote_is_unavailable(self):
        state = SaveSyncScreenState(
            romcloud_bin="romcloud",
            selected_index=1,
            status={"remote_configured": True},
            remote_availability=REMOTE_UNAVAILABLE,
            remote_detail="storage check timed out",
        )

        state.confirm_dashboard_selection()

        assert state.step == DASHBOARD
        assert "unavailable" in state.error
        assert "timed out" in state.error
        assert state._runner is None  # noqa: SLF001


class TestLocalFirstLoading:
    def test_status_and_availability_start_as_separate_background_operations(self):
        state = SaveSyncScreenState(romcloud_bin="romcloud")
        actions = []

        def fake_popen(argv, **kwargs):
            actions.append(argv[-1])
            return _FakeProcess('{"ok": true, "remote_configured": true}\n')

        state.popen = fake_popen
        state.start_loading()

        assert state.step == DASHBOARD
        assert state.remote_availability == REMOTE_CHECKING
        assert actions == ["savesync-status", "savesync-availability"]

    def test_local_status_is_visible_while_remote_check_remains_pending(self):
        pending = _PendingProcess()
        state = SaveSyncScreenState(romcloud_bin="romcloud")
        state.popen = _fake_popen_by_action(
            {
                "savesync-status": {
                    "ok": True,
                    "remote_configured": True,
                    "xbox_enabled": True,
                    "last_upload": {"timestamp": "now"},
                }
            },
            pending=pending,
        )

        state.start_loading()
        state.poll()

        assert state.status["xbox_enabled"] is True
        assert state.status_loading is False
        assert state.remote_availability == REMOTE_CHECKING
        assert state._availability_runner is not None  # noqa: SLF001
        state.cancel_pending()

    def test_available_result_enables_remote_actions(self):
        state = SaveSyncScreenState(romcloud_bin="romcloud")
        state.popen = _fake_popen_by_action(
            {
                "savesync-status": {"ok": True, "remote_configured": True},
                "savesync-availability": {
                    "ok": True,
                    "remote_configured": True,
                    "remote_available": True,
                },
            }
        )

        state.start_loading()
        _drain(state)

        assert state.remote_availability == REMOTE_AVAILABLE
        assert state.remote_actions_available is True

    def test_unavailable_result_preserves_local_status(self):
        state = SaveSyncScreenState(romcloud_bin="romcloud")
        state.popen = _fake_popen_by_action(
            {
                "savesync-status": {
                    "ok": True,
                    "remote_configured": True,
                    "last_download": {"timestamp": "yesterday"},
                },
                "savesync-availability": {
                    "ok": True,
                    "remote_configured": True,
                    "remote_available": False,
                    "detail": "not mounted",
                },
            }
        )

        state.start_loading()
        _drain(state)

        assert state.remote_availability == REMOTE_UNAVAILABLE
        assert state.remote_detail == "not mounted"
        assert state.status["last_download"]["timestamp"] == "yesterday"

    def test_late_local_status_cannot_restore_stale_remote_observation(self):
        state = SaveSyncScreenState(romcloud_bin="romcloud", status_loading=True)
        status_runner = _ControlledRunner(
            {
                "ok": True,
                "remote_configured": True,
                "sync_status": "remote-unavailable",
                "active_conflicts": 0,
                "xbox_enabled": False,
            },
            finished=False,
        )
        availability_runner = _ControlledRunner(
            {
                "ok": True,
                "remote_configured": True,
                "remote_available": True,
                "sync_status": "clean",
                "active_conflicts": 0,
            },
            finished=True,
        )
        state._status_runner = status_runner  # noqa: SLF001
        state._availability_runner = availability_runner  # noqa: SLF001

        state.poll()
        status_runner.is_finished = True
        state.poll()

        assert state.remote_availability == REMOTE_AVAILABLE
        assert state.status["sync_status"] == "clean"
        assert state.status["xbox_enabled"] is False

    def test_availability_deadline_is_bounded_and_does_not_retry(self):
        now = [0.0]
        pending = _PendingProcess()
        calls = []

        def fake_popen(argv, **kwargs):
            calls.append(argv[-1])
            if argv[-1] == "savesync-status":
                return _FakeProcess('{"ok": true, "remote_configured": true}\n')
            return pending

        state = SaveSyncScreenState(
            romcloud_bin="romcloud",
            popen=fake_popen,
            clock=lambda: now[0],
            availability_timeout=1.0,
        )
        state.start_loading()
        now[0] = 2.0

        state.poll()
        state.poll()

        assert state.remote_availability == REMOTE_UNAVAILABLE
        assert "timed out" in state.remote_detail
        assert pending.terminated is True
        assert calls.count("savesync-availability") == 1

    def test_cancel_pending_abandons_both_background_checks(self):
        status_process = _PendingProcess()
        availability_process = _PendingProcess()

        def fake_popen(argv, **kwargs):
            return status_process if argv[-1] == "savesync-status" else availability_process

        state = SaveSyncScreenState(romcloud_bin="romcloud", popen=fake_popen)
        state.start_loading()

        state.cancel_pending()
        state.cancel_pending()

        assert status_process.terminated is True
        assert availability_process.terminated is True
        assert state._status_runner is None  # noqa: SLF001
        assert state._availability_runner is None  # noqa: SLF001


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
    def test_auto_sync_setting_waits_for_local_status(self):
        state = SaveSyncScreenState(
            romcloud_bin="romcloud", step=SETTINGS, settings_selected_index=0
        )

        state.confirm_settings_selection()

        assert state.step == SETTINGS
        assert "still loading" in state.error
        assert state._runner is None  # noqa: SLF001

    def test_set_auto_sync_enabled_applies_and_returns_to_settings(self):
        state = SaveSyncScreenState(romcloud_bin="romcloud")
        state.popen = _fake_popen_returning(
            {"ok": True, "auto_sync_enabled": True, "xbox_enabled": False}
        )
        state.set_auto_sync_enabled(True)
        assert state.step == APPLYING_SETTINGS

        _drain(state)

        assert state.step == SETTINGS
        assert state.status["auto_sync_enabled"] is True
        assert state.settings_items[0] == "Auto Sync Saves: On"

    def test_set_xbox_enabled_applies_and_returns_to_settings(self):
        state = SaveSyncScreenState(romcloud_bin="romcloud")
        state.popen = _fake_popen_returning({"ok": True, "xbox_enabled": True})
        state.set_xbox_enabled(True)
        assert state.step == APPLYING_SETTINGS

        _drain(state)

        assert state.step == SETTINGS
        assert state.status["xbox_enabled"] is True

    def test_settings_exposes_auto_sync_xbox_and_back(self):
        state = SaveSyncScreenState(
            romcloud_bin="romcloud", step=SETTINGS, settings_selected_index=99
        )

        state.select_setting(99)
        result = state.confirm_settings_selection()

        assert result == "back"
        assert state.step == DASHBOARD

    def test_confirm_auto_sync_toggles_current_value(self):
        state = SaveSyncScreenState(
            romcloud_bin="romcloud",
            step=SETTINGS,
            status={"auto_sync_enabled": False, "xbox_enabled": False},
        )
        requested = []
        state.set_auto_sync_enabled = requested.append
        state.confirm_settings_selection()

        assert requested == [True]

    def test_return_to_dashboard_resets_selection(self):
        state = SaveSyncScreenState(romcloud_bin="romcloud", step=SETTINGS, selected_index=2)
        state.return_to_dashboard()
        assert state.step == DASHBOARD
        assert state.selected_index == 0
