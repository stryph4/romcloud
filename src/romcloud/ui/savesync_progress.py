"""Graphical (pygame, system-Python) progress popup for gameStop Auto SaveSync.

Auto SaveSync's gameStop path is intentionally synchronous: the Batocera
lifecycle hook waits for Quick Sync (discovery, stability, upload/download,
journal/conflict scoping) to durably finish before returning control to
EmulationStation. Hardware testing shows this can take 15+ seconds with no
visible feedback. This module makes that wait observable without changing
any of its correctness properties.

Same subprocess+NDJSON boundary already used by the cache-miss launch
progress screen (:mod:`romcloud.ui.graphical_progress` /
:mod:`ports_gfx.launch_progress`): this process (the venv, no pygame) spawns
the installed ``romcloud-ports`` wrapper (system Python + ``ports_gfx``) in
``--savesync-progress`` mode and drives it with newline-delimited JSON stage
events over its stdin. ``ports_gfx`` still never imports anything from
``romcloud``.

Fail-open by design: the popup has no channel to influence SaveSync at all
(one-way stdin, no stdout is read back, no cancellation is possible), and
every public method here swallows its own failures. If the launcher is
missing, the subprocess cannot start, or the pipe breaks mid-sync, the
caller still gets a working (no-op) reporter and Auto SaveSync proceeds
exactly as it would with no graphical feedback at all.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Optional, Protocol

SAVESYNC_PROGRESS_ARG = "--savesync-progress"

_SUBPROCESS_EXIT_GRACE_SECONDS = 3.0


class SaveSyncProgressLike(Protocol):
    def stage(self, text: str) -> None: ...

    def close(self, ok: bool, message: Optional[str] = None) -> None: ...


class NullSaveSyncProgress:
    """No-op reporter used whenever the graphical popup is unavailable."""

    def stage(self, text: str) -> None:  # noqa: ARG002
        return None

    def close(self, ok: bool, message: Optional[str] = None) -> None:  # noqa: ARG002
        return None


class SaveSyncProgressReporter:
    """Drives one already-launched ``--savesync-progress`` subprocess.

    ``close`` is idempotent — safe to call more than once (e.g. once from
    the coordinator that knows the real outcome, and once more as a
    lifecycle-safety net in the CLI command's ``finally`` block).
    """

    def __init__(self, proc: "subprocess.Popen[str]") -> None:
        self._proc = proc
        self._closed = False

    def stage(self, text: str) -> None:
        self._send({"stage": text})

    def close(self, ok: bool, message: Optional[str] = None) -> None:
        if self._closed:
            return
        self._closed = True
        event: dict = {"event": "done", "ok": bool(ok)}
        if message:
            event["message"] = message
        self._send(event)
        self._close_subprocess(ok=bool(ok))

    def _send(self, event: dict) -> None:
        try:
            if self._proc.stdin is None:
                return
            self._proc.stdin.write(json.dumps(event) + "\n")
            self._proc.stdin.flush()
        except (BrokenPipeError, ValueError, OSError):
            pass  # UI process gone — SaveSync itself must still proceed

    def _close_subprocess(self, *, ok: bool) -> None:
        try:
            if self._proc.stdin:
                self._proc.stdin.close()
        except Exception:  # noqa: BLE001
            pass
        if ok:
            # The SaveSync transaction is already durably committed by the
            # time a successful close is reported. Blocking the lifecycle hook
            # (and therefore EmulationStation) for the popup's purely cosmetic
            # success fade would add that delay to every single game exit, so
            # the popup is left to dismiss itself: it exits on its own success
            # timer, and on its stream-closed grace timer if anything goes
            # wrong. Nothing about the sync itself is deferred here.
            return
        # A failure message must actually be readable before control returns.
        try:
            self._proc.wait(timeout=_SUBPROCESS_EXIT_GRACE_SECONDS)
        except subprocess.TimeoutExpired:
            try:
                self._proc.terminate()
                self._proc.wait(timeout=2)
            except Exception:  # noqa: BLE001
                try:
                    self._proc.kill()
                except Exception:  # noqa: BLE001
                    pass
        except Exception:  # noqa: BLE001
            pass


def start_savesync_progress(
    launcher: Optional[Path],
    *,
    popen=subprocess.Popen,
) -> SaveSyncProgressLike:
    """Best-effort launch of the gameStop progress popup.

    Never raises. Returns :class:`NullSaveSyncProgress` if *launcher* is
    ``None``/missing or the subprocess cannot be started — the caller
    always gets something it can call ``stage``/``close`` on unconditionally.
    """
    if launcher is None or not Path(launcher).is_file():
        return NullSaveSyncProgress()
    try:
        proc = popen(
            [str(launcher), SAVESYNC_PROGRESS_ARG],
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            text=True,
            bufsize=1,
        )
    except OSError:
        return NullSaveSyncProgress()
    return SaveSyncProgressReporter(proc)
