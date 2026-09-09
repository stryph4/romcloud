"""Small, centered progress overlay for synchronous gameStop Auto SaveSync.

Real hardware context: Auto SaveSync's gameStop path is intentionally
synchronous — ``configgen``'s gameStop hook blocks until Quick Sync (upload/
download/journal/conflict scoping) has durably finished, which hardware
testing shows can take 15+ seconds. Without this popup the user sees
nothing during that window and ROMCloud appears frozen.

Runs under Batocera's system Python (pygame/SDL), exactly like the existing
graphical Ports UI (see ``ports_gfx/app.py``) and the cache-miss launch
progress screen (``ports_gfx/launch_progress.py``) — never imports anything
from ``romcloud``. Driven entirely by newline-delimited JSON events read
from stdin, written by the venv-side bridge
(:mod:`romcloud.ui.savesync_progress`). This process only renders; it has
no channel back to the backend and is never able to cancel, delay, or
influence the real SaveSync operation in any way — closing this window is
purely cosmetic and is never treated as SaveSync's completion signal by
the caller.

Protocol (newline-delimited JSON, one object per line), stdin only::

    {"stage": "Checking save changes…"}
    {"event": "done", "ok": true, "message": "Save sync complete."}

This is intentionally a progress/status surface, not a menu: every input
event is discarded. There is nothing to cancel and nothing to confirm.
"""

from __future__ import annotations

import json
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import IO, Callable, Optional

from ports_gfx.theme import ACCENT, BACKGROUND, ERROR, MUTED_TEXT, SUCCESS, TEXT, system_font

_INITIAL_STAGE_TEXT = "Checking save changes…"
_TITLE_TEXT = "Auto SaveSync"

_SUCCESS_DISMISS_SECONDS = 1.2
_FAILURE_DISMISS_SECONDS = 4.0
# Safety net only: if the parent process dies without ever sending a "done"
# event, this popup must still not become a permanent orphan window.
_STREAM_CLOSED_GRACE_SECONDS = 1.5

_CARD_WIDTH_FRACTION = 0.42
_CARD_MIN_WIDTH_PX = 420
_CARD_HEIGHT_PX = 190
_SPINNER_RADIUS_PX = 16
_SPINNER_REVOLUTION_SECONDS = 1.1


@dataclass
class SaveSyncProgressState:
    """Thread-safe shared state: a background thread applies NDJSON events
    parsed from stdin while the main thread renders/polls input."""

    stage_text: str = _INITIAL_STAGE_TEXT
    done: bool = False
    ok: bool = True
    stream_closed: bool = False
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def apply(self, event: dict) -> None:
        with self._lock:
            if event.get("event") == "done":
                self.done = True
                self.ok = bool(event.get("ok", True))
                message = event.get("message")
                if message:
                    self.stage_text = str(message)
                return
            stage = event.get("stage")
            if stage:
                self.stage_text = str(stage)

    def mark_stream_closed(self) -> None:
        with self._lock:
            self.stream_closed = True

    def snapshot(self) -> tuple[str, bool, bool, bool]:
        with self._lock:
            return self.stage_text, self.done, self.ok, self.stream_closed


def parse_event(line: str) -> Optional[dict]:
    """Parse one NDJSON line from stdin. Malformed/blank lines are ignored
    — a single bad line must never crash the UI process."""
    line = line.strip()
    if not line:
        return None
    try:
        payload = json.loads(line)
    except ValueError:
        return None
    return payload if isinstance(payload, dict) else None


def read_events(stream: IO[str], state: SaveSyncProgressState) -> None:
    """Background-thread reader: apply every parsed event to *state* until
    the stream closes. Always marks the stream closed on exit so the main
    loop's safety-net dismiss timer can engage even after a broken pipe."""
    try:
        for line in iter(stream.readline, ""):
            event = parse_event(line)
            if event is not None:
                state.apply(event)
    except Exception:  # noqa: BLE001 - a broken pipe must never crash the UI
        pass
    finally:
        state.mark_stream_closed()


@dataclass
class DismissTimer:
    """Pure, injectable-clock decision of when the popup should close.

    Kept separate from rendering so the auto-dismiss timing itself is fully
    unit-tested without pygame.
    """

    clock: Callable[[], float] = time.monotonic
    _done_since: Optional[float] = field(default=None, repr=False)
    _stream_closed_since: Optional[float] = field(default=None, repr=False)

    def should_exit(self, *, done: bool, ok: bool, stream_closed: bool) -> bool:
        now = self.clock()
        if done:
            if self._done_since is None:
                self._done_since = now
            dismiss = _SUCCESS_DISMISS_SECONDS if ok else _FAILURE_DISMISS_SECONDS
            return (now - self._done_since) >= dismiss
        if stream_closed:
            if self._stream_closed_since is None:
                self._stream_closed_since = now
            return (now - self._stream_closed_since) >= _STREAM_CLOSED_GRACE_SECONDS
        return False


def card_rect(screen_w: int, screen_h: int) -> tuple[int, int, int, int]:
    """Centered card geometry — pure geometry, no pygame, fully testable."""
    width = max(_CARD_MIN_WIDTH_PX, int(screen_w * _CARD_WIDTH_FRACTION))
    width = min(width, screen_w - 40)
    height = _CARD_HEIGHT_PX
    x = (screen_w - width) // 2
    y = (screen_h - height) // 2
    return x, y, width, height


def spinner_angle_degrees(elapsed_seconds: float) -> float:
    """0..360 spinner rotation — pure, testable."""
    fraction = (elapsed_seconds % _SPINNER_REVOLUTION_SECONDS) / _SPINNER_REVOLUTION_SECONDS
    return fraction * 360.0


def _try_grab_window_input(pygame) -> bool:  # noqa: ANN001
    """Best-effort keyboard/pointer grab so input isn't left free to reach
    EmulationStation underneath. Never fatal — a failure here must not
    prevent the progress popup (or SaveSync) from proceeding."""
    try:
        pygame.event.set_grab(True)
        set_keyboard_grab = getattr(pygame.event, "set_keyboard_grab", None)
        if callable(set_keyboard_grab):
            set_keyboard_grab(True)
        return True
    except Exception:  # noqa: BLE001
        return False


def _release_window_input_grab(pygame) -> None:  # noqa: ANN001
    try:
        set_keyboard_grab = getattr(pygame.event, "set_keyboard_grab", None)
        if callable(set_keyboard_grab):
            set_keyboard_grab(False)
        pygame.event.set_grab(False)
    except Exception:  # noqa: BLE001 - cleanup is best effort
        pass


def _try_capture_controllers(pygame, romcloud_bin: str):  # noqa: ANN001
    """Best-effort exclusive controller capture (EVIOCGRAB), mirroring the
    SaveSync conflict popup's input isolation. Returns ``None`` on any
    failure — this popup has nothing to confirm/cancel, so a capture
    failure only means input *might* leak through, never that SaveSync
    itself is affected."""
    try:
        from ports_gfx.input_capture import ExclusiveControllerCapture
        from ports_gfx.input_manager import InputManager

        inputs = InputManager(pygame, romcloud_bin)
        count = pygame.joystick.get_count()
        inputs.controllers.open_existing_devices(count)
        if inputs.controllers.device_count < count:
            return None
        capture = ExclusiveControllerCapture(lambda *_a, **_k: None)
        if not capture.acquire(count):
            return None
        return capture
    except Exception:  # noqa: BLE001 - best effort only
        return None


def main(stdin: IO[str] = sys.stdin, *, romcloud_bin: str = "") -> int:
    """Entry point for ``python -m ports_gfx`` (``--savesync-progress``).

    Any failure (pygame missing, display init failure, unexpected crash) is
    caught and reported to stderr with a non-zero exit code. The venv-side
    caller never depends on this exit code for SaveSync correctness — it
    only bounds how long the subprocess is waited on before being killed.
    """
    try:
        import pygame
    except ImportError as exc:
        print(
            f"error: pygame is not available under this Python interpreter: {exc}",
            file=sys.stderr,
        )
        return 1

    state = SaveSyncProgressState()
    reader = threading.Thread(target=read_events, args=(stdin, state), daemon=True)
    reader.start()

    try:
        return _run(pygame, state, romcloud_bin)
    except Exception as exc:  # noqa: BLE001 - must never crash the lifecycle worker
        print(f"error: savesync progress UI crashed: {exc}", file=sys.stderr)
        return 1


def _detect_screen_size(pygame) -> tuple[int, int]:  # noqa: ANN001
    from ports_gfx.app import _detect_screen_size as _app_detect_screen_size

    return _app_detect_screen_size(pygame)


def _open_display(pygame, screen_w: int, screen_h: int):  # noqa: ANN001
    from ports_gfx.app import _open_display as _app_open_display

    return _app_open_display(pygame, screen_w, screen_h, None)


def _run(pygame, state: SaveSyncProgressState, romcloud_bin: str) -> int:  # noqa: ANN001
    pygame.init()
    capture = None
    grabbed = False
    try:
        screen_w, screen_h = _detect_screen_size(pygame)
        screen = _open_display(pygame, screen_w, screen_h)
        pygame.display.set_caption("ROMCloud Auto SaveSync")
        font_title = system_font(pygame, 30, strong=True)
        font_body = system_font(pygame, 22)
        grabbed = _try_grab_window_input(pygame)
        if romcloud_bin:
            capture = _try_capture_controllers(pygame, romcloud_bin)

        clock = pygame.time.Clock()
        timer = DismissTimer()
        started = time.monotonic()
        running = True
        while running:
            clock.tick(30)
            # Progress/status surface, not a menu: every input event is
            # discarded. There is nothing to confirm or cancel, and no
            # action can be triggered twice because none is ever triggered.
            try:
                pygame.event.pump()
                pygame.event.get()
            except Exception:  # noqa: BLE001
                pass
            stage_text, done, ok, stream_closed = state.snapshot()
            _render(
                pygame,
                screen,
                screen_w,
                screen_h,
                font_title,
                font_body,
                stage_text,
                done,
                ok,
                time.monotonic() - started,
            )
            if timer.should_exit(done=done, ok=ok, stream_closed=stream_closed):
                running = False
        return 0
    finally:
        if capture is not None:
            capture.release(reason="progress-popup-exit")
        if grabbed:
            _release_window_input_grab(pygame)
        pygame.quit()


def _render(  # noqa: ANN001
    pygame,
    screen,
    screen_w: int,
    screen_h: int,
    font_title,
    font_body,
    stage_text: str,
    done: bool,
    ok: bool,
    elapsed_seconds: float,
) -> None:
    screen.fill(BACKGROUND)
    x, y, w, h = card_rect(screen_w, screen_h)
    pygame.draw.rect(screen, (15, 31, 56), (x, y, w, h), border_radius=12)
    pygame.draw.rect(screen, (35, 69, 111), (x, y, w, h), width=2, border_radius=12)

    title = font_title.render(_TITLE_TEXT, True, TEXT)
    screen.blit(title, (x + 24, y + 20))

    if done:
        color = SUCCESS if ok else ERROR
    else:
        color = MUTED_TEXT
        center = (x + w - 48, y + 36)
        angle = spinner_angle_degrees(elapsed_seconds)
        rect = (
            center[0] - _SPINNER_RADIUS_PX,
            center[1] - _SPINNER_RADIUS_PX,
            _SPINNER_RADIUS_PX * 2,
            _SPINNER_RADIUS_PX * 2,
        )
        try:
            import math

            start = math.radians(angle)
            end = start + math.radians(270)
            pygame.draw.arc(screen, ACCENT, rect, start, end, width=4)
        except Exception:  # noqa: BLE001 - the spinner is purely decorative
            pass

    line_y = y + 76
    for line in str(stage_text).split("\n"):
        rendered = font_body.render(line, True, color if done else TEXT)
        screen.blit(rendered, (x + 24, line_y))
        line_y += font_body.get_height() + 6

    pygame.display.flip()


if __name__ == "__main__":
    raise SystemExit(main())
