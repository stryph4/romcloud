"""Progress UI — shown when a game needs to be transferred before launch.

Anti-flicker design
-------------------
The previous implementation called ``stdscr.clear()`` + ``stdscr.refresh()``
on every loop iteration.  ``clear()`` marks *every* cell dirty so the
terminal driver redraws the whole screen, producing a full blank-flash each
frame.

Current approach eliminates this:

1. **Static elements drawn once.**  The outer frame (borders, title, game
   name, cancel hint) is written with :func:`_draw_static` at startup and
   again only when the terminal is resized.

2. **Dynamic rows overwritten in-place.**  :func:`_draw_dynamic` moves to
   each dynamic row, calls ``clrtoeol()`` to clear only that row, then
   writes new content.  No other cells are touched.

3. **``noutrefresh()`` + ``curses.doupdate()``.**  Instead of ``refresh()``
   (which immediately sends all changed cells), ``noutrefresh()`` copies to
   curses' internal physical buffer; ``doupdate()`` sends *only changed
   cells* to the terminal in one shot at the end of each display tick.

4. **Display throttled to ~8 FPS (``_FRAME_INTERVAL``).**  Transfer progress
   callbacks are fully decoupled — they run in the worker thread at whatever
   rate the provider fires them.

5. **Input polled at ~50 Hz (``_INPUT_INTERVAL``).**  Cancel (Q / ESC) stays
   responsive without coupling input latency to the display rate.

Design constraints:
- No game logic here; all business decisions go through CacheService
- Falls back gracefully if curses is unavailable (TTY-less / piped)
"""

from __future__ import annotations

try:
    import curses
except Exception:  # Module may be missing on minimal systems
    curses = None  # type: ignore[assignment]
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

from romcloud.core.exceptions import ROMCloudError
from romcloud.core.models.game import Game
from romcloud.services.cache import CacheService

# ── timing constants ──────────────────────────────────────────────────────────

_FRAME_INTERVAL: float = 1.0 / 8  # ~8 FPS visual refresh
_INPUT_INTERVAL: float = 0.02      # ~50 Hz input poll (keeps cancel responsive)

# ── speed tracker ─────────────────────────────────────────────────────────────


class _SpeedTracker:
    """Exponentially-weighted moving average transfer speed estimator.

    Accepts an injectable clock for deterministic testing.
    """

    ALPHA: float = 0.3           # weight for the newest sample
    SAMPLE_INTERVAL: float = 0.5  # minimum seconds between samples

    def __init__(self, _clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = _clock
        self._last_t: float = _clock()
        self._last_bytes: int = 0
        self._smooth: float = 0.0

    def update(self, current_bytes: int) -> bool:
        """Incorporate *current_bytes* into the speed estimate.

        Returns True if the estimate was recalculated (sample interval elapsed),
        False if the sample was too soon and skipped.
        """
        now = self._clock()
        elapsed = now - self._last_t
        if elapsed < self.SAMPLE_INTERVAL:
            return False
        delta = max(0, current_bytes - self._last_bytes)
        instant = delta / elapsed if elapsed > 0 else 0.0
        self._smooth = (1.0 - self.ALPHA) * self._smooth + self.ALPHA * instant
        self._last_t = now
        self._last_bytes = current_bytes
        return True

    @property
    def bps(self) -> float:
        """Current smoothed speed estimate in bytes per second."""
        return self._smooth


# ── shared state ──────────────────────────────────────────────────────────────


@dataclass
class _ProgressState:
    game: Game
    phase: str = "Connecting"
    bytes_done: int = 0
    bytes_total: int = 0
    speed_bps: float = 0.0
    cancelled: bool = False
    error: Optional[str] = None
    result: Optional[str] = None  # launch path when done
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def update(self, done: int, total: int) -> None:
        with self._lock:
            self.bytes_done = done
            self.bytes_total = total
            if done > 0:
                self.phase = "Downloading"


# ── layout helper ─────────────────────────────────────────────────────────────


def _compute_layout(h: int, w: int) -> tuple[int, int]:
    """Return ``(centre_row, bar_width)`` for a terminal of size *h* × *w*."""
    centre_row = max(0, h // 2 - 5)
    bar_width = max(10, min(60, w - 10))
    return centre_row, bar_width


# ── public API ────────────────────────────────────────────────────────────────


def run_progress_transfer(cache_service: CacheService, game: Game) -> str:
    """Transfer *game* to cache while showing a progress screen.

    Returns the launch path on success.
    Raises :class:`~romcloud.core.exceptions.ROMCloudError` on failure.
    Raises ``KeyboardInterrupt`` on user cancellation.
    """
    state = _ProgressState(game=game)

    # Total size hint from catalog (may be None).
    if game.total_size_bytes:
        state.bytes_total = game.total_size_bytes

    def on_progress(done: int, total: int) -> None:
        if state.cancelled:
            raise KeyboardInterrupt("Transfer cancelled by user")
        state.update(done, total)

    def worker() -> None:
        try:
            state.phase = "Downloading"
            result = cache_service.cache_game(game.id, on_progress=on_progress)
            state.phase = "Launching"
            state.result = result
        except KeyboardInterrupt:
            state.cancelled = True
        except Exception as exc:  # noqa: BLE001
            state.error = str(exc)

    t = threading.Thread(target=worker, daemon=True)
    t.start()

    if sys.stdout.isatty():
        try:
            if curses is not None:
                curses.wrapper(_render_progress, state)
            else:
                # curses isn't available on this platform — use plain text.
                _plain_progress(state)
        except Exception:  # noqa: BLE001
            # curses failed at runtime (e.g. TERM unset) — fall back to plain text.
            _plain_progress(state)
    else:
        _plain_progress(state)

    t.join(timeout=5)

    if state.cancelled:
        raise KeyboardInterrupt
    if state.error:
        raise ROMCloudError(state.error)
    if state.result is None:
        raise ROMCloudError("Transfer completed but launch path is unavailable")
    return state.result


# ── curses renderer ───────────────────────────────────────────────────────────


def _render_progress(stdscr: "curses.window", state: _ProgressState) -> None:
    """Main curses render loop.

    Draws static elements once; updates only dynamic rows per display tick.
    Display is throttled to ``_FRAME_INTERVAL``; input is polled every
    ``_INPUT_INTERVAL`` regardless.
    """
    curses.curs_set(0)
    stdscr.nodelay(True)

    h, w = stdscr.getmaxyx()
    centre_row, bar_width = _compute_layout(h, w)

    # Paint static frame and initial dynamic content, then flush once.
    _draw_static(stdscr, state, h, w, centre_row)
    _draw_dynamic(stdscr, state, h, w, centre_row, bar_width)
    stdscr.noutrefresh()
    curses.doupdate()

    speed_tracker = _SpeedTracker()
    last_render: float = time.monotonic()

    while True:
        # ── input poll (always fast) ──────────────────────────────────────────
        ch = stdscr.getch()
        if ch == curses.KEY_RESIZE:
            # Terminal resized — full repaint required.
            h, w = stdscr.getmaxyx()
            centre_row, bar_width = _compute_layout(h, w)
            _draw_static(stdscr, state, h, w, centre_row)
            last_render = 0.0  # force immediate dynamic repaint

        elif ch in (ord("q"), ord("Q"), 27):  # Q or ESC
            state.cancelled = True

        if state.cancelled or state.error or state.result:
            break

        # ── speed sampling (every SAMPLE_INTERVAL seconds) ───────────────────
        if speed_tracker.update(state.bytes_done):
            state.speed_bps = speed_tracker.bps

        # ── display update (throttled to ~8 FPS) ─────────────────────────────
        now = time.monotonic()
        if now - last_render >= _FRAME_INTERVAL:
            _draw_dynamic(stdscr, state, h, w, centre_row, bar_width)
            stdscr.noutrefresh()
            curses.doupdate()
            last_render = now

        time.sleep(_INPUT_INTERVAL)

    # Final frame — show completed / error / cancelled state.
    _draw_dynamic(stdscr, state, h, w, centre_row, bar_width)
    stdscr.noutrefresh()
    curses.doupdate()

    if state.result:
        time.sleep(0.4)  # brief "Launching" flash


def _draw_static(
    stdscr: "curses.window",
    state: _ProgressState,
    h: int,
    w: int,
    centre_row: int,
) -> None:
    """Draw the non-changing parts of the progress frame.

    Called once at startup and again only on terminal resize.
    Uses ``erase()`` rather than ``clear()``: ``erase()`` marks cells dirty
    in the virtual screen but does *not* immediately send a clear sequence to
    the physical terminal — the diff is resolved lazily on the next
    ``doupdate()``.
    """
    stdscr.erase()

    def put(row: int, col: int, text: str, attr: int = 0) -> None:
        if 0 <= row < h and 0 <= col < w:
            stdscr.addstr(row, col, text[: w - col], attr)

    put(centre_row,      0, "─" * w)
    put(centre_row + 1,  0, "  PREPARING GAME", curses.A_BOLD)
    put(centre_row + 2,  0, "─" * w)
    put(centre_row + 3,  0, f"  {state.game.title[: w - 4]}", curses.A_BOLD)
    put(centre_row + 4,  0, f"  {state.game.system.upper()}")
    put(centre_row + 11, 0, "─" * w)
    put(centre_row + 12, 2, "Press Q or ESC to cancel")


def _draw_dynamic(
    stdscr: "curses.window",
    state: _ProgressState,
    h: int,
    w: int,
    centre_row: int,
    bar_width: int,
) -> None:
    """Overwrite only the rows that change every frame.

    Each dynamic row is cleared with ``move()`` + ``clrtoeol()`` and then
    rewritten.  Static rows are never touched.  The caller is responsible
    for calling ``noutrefresh()`` / ``doupdate()`` when ready to flush.
    """

    def put_row(row: int, col: int, text: str, attr: int = 0) -> None:
        if 0 <= row < h:
            stdscr.move(row, 0)
            stdscr.clrtoeol()
            if text and 0 <= col < w:
                stdscr.addstr(row, col, text[: w - col], attr)

    done = state.bytes_done
    total = state.bytes_total
    pct = (done / total * 100.0) if total > 0 else 0.0
    filled = int(bar_width * pct / 100)
    bar = "[" + "█" * filled + "░" * (bar_width - filled) + f"]  {pct:>5.1f}%"
    put_row(centre_row + 6, 2, bar)

    if total > 0:
        put_row(centre_row + 7, 2, f"{_fmt_bytes(done)} / {_fmt_bytes(total)}")
    else:
        put_row(centre_row + 7, 2, _fmt_bytes(done))

    if state.speed_bps > 0:
        speed_str = f"{_fmt_bytes(int(state.speed_bps))}/s"
        eta_str = ""
        if total > done > 0:
            eta_sec = (total - done) / state.speed_bps
            eta_str = f"  ETA {_fmt_eta(eta_sec)}"
        put_row(centre_row + 8, 2, f"Speed: {speed_str}{eta_str}")
    else:
        put_row(centre_row + 8, 2, "")

    put_row(centre_row + 9,  0, "")
    put_row(centre_row + 10, 2, state.phase)

    if state.error:
        put_row(centre_row + 13, 2, f"Error: {state.error}", curses.A_BOLD)
    else:
        put_row(centre_row + 13, 2, "")


# ── plain text fallback ───────────────────────────────────────────────────────


def _plain_progress(state: _ProgressState) -> None:
    """Very simple non-curses progress for piped/headless contexts."""
    while not (state.cancelled or state.error or state.result):
        done = state.bytes_done
        total = state.bytes_total
        if total > 0:
            pct = done / total * 100
            sys.stderr.write(
                f"\r  {state.phase}: {_fmt_bytes(done)} / {_fmt_bytes(total)} ({pct:.0f}%)"
            )
        else:
            sys.stderr.write(f"\r  {state.phase}: {_fmt_bytes(done)}")
        sys.stderr.flush()
        time.sleep(0.5)
    sys.stderr.write("\n")


# ── formatters ────────────────────────────────────────────────────────────────


def _fmt_bytes(n: int) -> str:
    if n >= 1024**3:
        return f"{n / 1024**3:.1f} GB"
    if n >= 1024**2:
        return f"{n / 1024**2:.0f} MB"
    if n >= 1024:
        return f"{n / 1024:.0f} KB"
    return f"{n} B"


def _fmt_eta(seconds: float) -> str:
    if seconds >= 3600:
        return f"{seconds / 3600:.0f}h"
    if seconds >= 60:
        return f"{seconds / 60:.0f}m"
    return f"{seconds:.0f}s"
