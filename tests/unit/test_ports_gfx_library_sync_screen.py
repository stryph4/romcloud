from __future__ import annotations

import json

from ports_gfx.actions import Action
from ports_gfx.input_manager import InputEvent
from ports_gfx.library_sync_screen import (
    CONFIRMING,
    IMPORTING,
    PREFLIGHT,
    PREFLIGHTING,
    RESULT,
    LibrarySyncScreenState,
)


class _Stdin:
    def write(self, text: str) -> None:
        self.text = text

    def close(self) -> None:
        pass


class _Stream:
    def __init__(self, text: str) -> None:
        self.lines = iter(text.splitlines(keepends=True) + [""])

    def readline(self) -> str:
        return next(self.lines, "")

    def close(self) -> None:
        pass


class _Process:
    def __init__(self, payload: dict | None, *, stderr: str = "", running=False):
        stdout = "" if payload is None else json.dumps(payload) + "\n"
        self.stdout = _Stream(stdout)
        self.stderr = _Stream(stderr)
        self.stdin = _Stdin()
        self.returncode = None if running else (0 if payload and payload.get("ok") else 1)
        self.terminated = False

    def poll(self):
        return self.returncode

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = -15


PREVIEW_PAYLOAD = {
    "ok": True,
    "games_eligible": 18200,
    "systems": ["ps2", "snes"],
    "gamelist_files": 2,
    "gamelist_bytes": 4096,
    "artwork_references": 8000,
    "video_references": 240,
    "other_media_references": 10,
    "duration_note": "Duration depends on referenced media size and storage/network speed.",
}


def _drain(state: LibrarySyncScreenState, limit: int = 50) -> None:
    for _ in range(limit):
        state.poll()
        if state._runner is None:  # noqa: SLF001
            return


def test_preview_is_shown_before_quick_sync_starts():
    actions = []

    def popen(argv, **kwargs):
        actions.append(argv[-1])
        return _Process(PREVIEW_PAYLOAD)

    state = LibrarySyncScreenState("romcloud", popen=popen)
    state.start_preview()
    assert state.step == PREFLIGHTING
    _drain(state)

    assert state.step == PREFLIGHT
    assert state.preview["games_eligible"] == 18200
    assert actions == ["library-sync-preview"]

    state.start_sync()
    assert state.step == IMPORTING
    assert actions == ["library-sync-preview", "library-sync"]


def test_full_long_press_starts_import_and_reports_real_progress():
    actions = []
    progress = (
        '@romcloud-progress {"timestamp":"12:00","operation":"library_sync",'
        '"stage":"media","status":"running","message":"ps2: media file 4 / 10",'
        '"detail":"4.0 GB / 10.0 GB","current":4,"total":10,'
        '"metadata":{"bytes_current":4294967296,"bytes_total":10737418240}}\n'
    )

    def popen(argv, **kwargs):
        actions.append(argv[-1])
        if argv[-1] == "library-sync-preview":
            return _Process(PREVIEW_PAYLOAD)
        return _Process({"ok": True, "metadata_added": 2, "media_transferred": 4, "rendered": 8}, stderr=progress)

    state = LibrarySyncScreenState("romcloud", sync_mode="full", popen=popen)
    state.start_preview()
    _drain(state)
    state.begin_confirm()
    state.handle_confirm_event(InputEvent(action=Action.CONFIRM))
    state.update_confirm(3.0)
    assert state.step == IMPORTING
    assert actions == ["library-sync-preview", "library-sync-full"]

    _drain(state)
    assert state.step == RESULT
    assert state.error == ""
    assert state.result["media_transferred"] == 4
    assert state.latest_progress.current == 4
    assert state.latest_progress.total == 10


def test_cancel_is_retryable_and_terminates_only_the_import_process():
    processes = []

    def popen(argv, **kwargs):
        process = (
            _Process(PREVIEW_PAYLOAD)
            if argv[-1] == "library-sync-preview"
            else _Process(None, running=True)
        )
        processes.append(process)
        return process

    state = LibrarySyncScreenState("romcloud", popen=popen)
    state.start_preview()
    _drain(state)
    state.start_sync()
    state.cancel_import()

    assert state.step == RESULT
    assert state.cancelled is True
    assert processes[-1].terminated is True
    state.retry()
    _drain(state)
    assert state.step == PREFLIGHT


def test_failure_returns_retryable_result():
    responses = iter(
        [
            _Process(PREVIEW_PAYLOAD),
            _Process({"ok": False, "error": "remote media unavailable"}),
            _Process(PREVIEW_PAYLOAD),
        ]
    )
    state = LibrarySyncScreenState("romcloud", popen=lambda argv, **kwargs: next(responses))
    state.start_preview()
    _drain(state)
    state.start_sync()
    _drain(state)

    assert state.step == RESULT
    assert "unavailable" in state.error
    state.retry()
    _drain(state)
    assert state.step == PREFLIGHT
