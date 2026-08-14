"""Fullscreen graphical cache-miss progress screen.

Runs under Batocera's system Python (pygame/SDL) exactly like the Ports
menu (see ``ports_gfx/app.py``) — never imports anything from ``romcloud``.
Driven entirely by newline-delimited JSON events read from stdin, written
by the venv-side bridge (:mod:`romcloud.ui.graphical_progress`); the actual
cache transfer, catalog, and ``CacheService`` logic all stay in the venv
process. This process only renders and reports a cancel request back over
stdout — it has no other way to reach ROMCloud's backend.

Protocol (newline-delimited JSON, one object per line):

stdin (backend -> UI)::

    {"phase": "connecting"|"downloading", "done": int, "total": int,
     "game_title": str, "system": str}
    {"event": "launching"}
    {"event": "error", "message": str}

stdout (UI -> backend)::

    cancel

``pygame`` is imported lazily inside :func:`run`/``_run`` only, so
importing this module — or unit-testing the pure protocol/state logic
below — never requires pygame to be installed, same precedent as
``app.py``.
"""

from __future__ import annotations

import json
import sys
import threading
from dataclasses import dataclass, field
from typing import IO, Optional

from ports_gfx.layout import Layout, compute_layout
from ports_gfx.theme import (
    ACCENT,
    BACKGROUND,
    ERROR,
    MUTED_TEXT,
    PROGRESS_TRACK,
    SUCCESS,
    TEXT,
    system_font,
)

_FALLBACK_SIZE = (1280, 720)
_MIN_SANE_DIMENSION = 240

_BG_COLOR = BACKGROUND
_FG_COLOR = TEXT
_ERROR_COLOR = ERROR
_SUCCESS_COLOR = SUCCESS
_HINT_COLOR = MUTED_TEXT

_LAUNCHING_DISPLAY_SECONDS = 0.6
_ERROR_AUTO_DISMISS_SECONDS = 15.0
"""Safety net only — closes a stuck error screen so it can never hang
Batocera indefinitely. The transfer itself has no timeout; this only
bounds how long the already-finished error *display* can wait for input."""


@dataclass
class LaunchProgressState:
    """Shared, thread-safe state: one background thread applies events
    parsed from stdin while the main thread renders/polls input."""

    game_title: str = ""
    system: str = ""
    phase: str = "Connecting"
    bytes_done: int = 0
    bytes_total: int = 0
    error: Optional[str] = None
    launching: bool = False
    cancelled: bool = False
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def apply(self, event: dict) -> None:
        with self._lock:
            kind = event.get("event")
            if kind == "launching":
                self.launching = True
                return
            if kind == "error":
                self.error = str(event.get("message") or "unknown error")
                return
            if "game_title" in event:
                self.game_title = str(event["game_title"])
            if "system" in event:
                self.system = str(event["system"])
            if "phase" in event:
                self.phase = str(event["phase"])
            if "done" in event:
                self.bytes_done = int(event["done"])
            if "total" in event:
                self.bytes_total = int(event["total"])

    def request_cancel(self) -> None:
        with self._lock:
            self.cancelled = True

    @property
    def is_finished(self) -> bool:
        with self._lock:
            return self.launching or self.error is not None or self.cancelled


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


def read_events(stream: IO[str], state: LaunchProgressState) -> None:
    """Background-thread reader: apply every parsed event to *state* until
    the stream closes or the state reaches a finished/cancelled state."""
    try:
        for line in iter(stream.readline, ""):
            event = parse_event(line)
            if event is not None:
                state.apply(event)
            if state.is_finished:
                return
    except Exception:  # noqa: BLE001 — a broken pipe must never crash the UI
        return


def progress_fraction(state: LaunchProgressState) -> float:
    """0.0..1.0 fraction complete, or 0.0 if total is unknown/zero."""
    if state.bytes_total <= 0:
        return 0.0
    return max(0.0, min(1.0, state.bytes_done / state.bytes_total))


def _fmt_bytes(n: int) -> str:
    if n >= 1024**3:
        return f"{n / 1024**3:.1f} GB"
    if n >= 1024**2:
        return f"{n / 1024**2:.0f} MB"
    if n >= 1024:
        return f"{n / 1024:.0f} KB"
    return f"{n} B"


# ── entry point ────────────────────────────────────────────────────────────


def main(stdin: IO[str] = sys.stdin) -> int:
    """Entry point for ``python -m ports_gfx.launch_progress``.

    Any failure (pygame missing, display init failure, unexpected crash) is
    caught and reported to stderr with a non-zero exit code — the parent
    venv process must always regain control cleanly and fall back to its
    curses/plain-text progress path.
    """
    try:
        import pygame
    except ImportError as exc:
        print(f"error: pygame is not available under this Python interpreter: {exc}", file=sys.stderr)
        return 1

    state = LaunchProgressState()
    reader = threading.Thread(target=read_events, args=(stdin, state), daemon=True)
    reader.start()

    try:
        return _run(pygame, state)
    except Exception as exc:  # noqa: BLE001 — must never crash Batocera's launch flow
        print(f"error: launch progress UI crashed: {exc}", file=sys.stderr)
        return 1


def _detect_screen_size(pygame) -> tuple[int, int]:  # noqa: ANN001
    try:
        info = pygame.display.Info()
        w, h = info.current_w, info.current_h
    except Exception:  # noqa: BLE001
        w, h = _FALLBACK_SIZE
    if w < _MIN_SANE_DIMENSION or h < _MIN_SANE_DIMENSION:
        return _FALLBACK_SIZE
    return w, h


def _open_display(pygame, screen_w: int, screen_h: int):  # noqa: ANN001
    # Match the main GUI path: prefer a desktop-sized borderless window
    # to avoid unnecessary display-mode switches.
    try:
        return pygame.display.set_mode((screen_w, screen_h), pygame.NOFRAME)
    except Exception:  # noqa: BLE001
        try:
            return pygame.display.set_mode((screen_w, screen_h), pygame.FULLSCREEN)
        except Exception:  # noqa: BLE001
            return pygame.display.set_mode((screen_w, screen_h))


def _run(pygame, state: LaunchProgressState) -> int:  # noqa: ANN001
    import time

    pygame.init()
    try:
        screen_w, screen_h = _detect_screen_size(pygame)
        screen = _open_display(pygame, screen_w, screen_h)
        pygame.display.set_caption("ROMCloud")
        layout = compute_layout(screen_w, screen_h, 0)
        font_title = system_font(pygame, layout.fonts.title, strong=True)
        font_body = system_font(pygame, layout.fonts.body)
        font_hint = system_font(pygame, layout.fonts.hint)
        clock = pygame.time.Clock()

        launching_since: Optional[float] = None
        error_since: Optional[float] = None

        running = True
        while running:
            clock.tick(30)
            for event in pygame.event.get():
                if event.type == pygame.QUIT:
                    state.request_cancel()
                    running = False
                elif event.type == pygame.KEYDOWN and event.key in (pygame.K_ESCAPE, pygame.K_q):
                    state.request_cancel()
                elif event.type in (
                    getattr(pygame, "JOYBUTTONDOWN", object()),
                    getattr(pygame, "CONTROLLERBUTTONDOWN", object()),
                ) and getattr(event, "button", None) == 1:
                    state.request_cancel()

            if state.cancelled:
                print("cancel", flush=True)
                running = False
                continue

            if state.launching:
                if launching_since is None:
                    launching_since = time.monotonic()
                _render(pygame, screen, layout, font_title, font_body, font_hint, state)
                if time.monotonic() - launching_since >= _LAUNCHING_DISPLAY_SECONDS:
                    running = False
                continue

            if state.error is not None:
                if error_since is None:
                    error_since = time.monotonic()
                _render(pygame, screen, layout, font_title, font_body, font_hint, state)
                if time.monotonic() - error_since >= _ERROR_AUTO_DISMISS_SECONDS:
                    running = False
                continue

            _render(pygame, screen, layout, font_title, font_body, font_hint, state)

        return 0
    finally:
        pygame.quit()


def _render(pygame, screen, layout: Layout, font_title, font_body, font_hint, state: LaunchProgressState) -> None:  # noqa: ANN001
    screen.fill(_BG_COLOR)
    safe_area = layout.safe_area

    title = font_title.render("Preparing Game", True, _FG_COLOR)
    screen.blit(title, (safe_area.x, safe_area.y))

    game_line = font_body.render(state.game_title or "", True, _FG_COLOR)
    screen.blit(game_line, (safe_area.x, safe_area.y + layout.fonts.title + 12))

    system_line = font_body.render(state.system.upper() if state.system else "", True, _FG_COLOR)
    screen.blit(system_line, (safe_area.x, safe_area.y + layout.fonts.title + 12 + layout.fonts.body + 6))

    bar_y = safe_area.y + safe_area.h // 2
    bar_w = safe_area.w
    bar_h = max(18, layout.fonts.body)
    pygame.draw.rect(screen, PROGRESS_TRACK, (safe_area.x, bar_y, bar_w, bar_h), border_radius=4)
    fraction = progress_fraction(state)
    if fraction > 0:
        pygame.draw.rect(screen, ACCENT, (safe_area.x, bar_y, int(bar_w * fraction), bar_h), border_radius=4)

    bytes_line = f"{_fmt_bytes(state.bytes_done)} / {_fmt_bytes(state.bytes_total)}" if state.bytes_total else _fmt_bytes(state.bytes_done)
    bytes_text = font_body.render(bytes_line, True, _FG_COLOR)
    screen.blit(bytes_text, (safe_area.x, bar_y + bar_h + 8))

    if state.error is not None:
        status_text = font_body.render(f"Error: {state.error}", True, _ERROR_COLOR)
    elif state.launching:
        status_text = font_body.render("Launching…", True, _SUCCESS_COLOR)
    else:
        status_text = font_body.render(state.phase, True, _FG_COLOR)
    screen.blit(status_text, (safe_area.x, bar_y + bar_h + 8 + layout.fonts.body + 6))

    hint_text = "" if (state.launching or state.error) else "B / Esc: cancel"
    hint = font_hint.render(hint_text, True, _HINT_COLOR)
    screen.blit(hint, (layout.hint_rect.x, layout.hint_rect.y))

    pygame.display.flip()


if __name__ == "__main__":
    raise SystemExit(main())
