"""Unit tests for ui/progress.py — non-curses components only.

The curses rendering functions (_draw_static, _draw_dynamic, _render_progress)
require a real PTY and are not tested here.  Everything else — _SpeedTracker,
_compute_layout, _ProgressState, the formatters, and the plain-text fallback —
is pure Python and fully testable without a terminal.
"""

from __future__ import annotations

import threading
import time

import pytest

from romcloud.ui.progress import (
    _SpeedTracker,
    _ProgressState,
    _compute_layout,
    _fmt_bytes,
    _fmt_eta,
    _FRAME_INTERVAL,
    _INPUT_INTERVAL,
)
from romcloud.core.models.game import Game, GameAsset


# ── fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture
def dummy_game() -> Game:
    return Game.create(
        system="ps2",
        title="Test Game",
        source_provider="local",
        source_root="/roms",
        assets=[GameAsset("Test Game.iso", "ps2/Test Game.iso", size_bytes=1000, is_primary=True)],
    )


# ── _SpeedTracker ─────────────────────────────────────────────────────────────


class TestSpeedTracker:
    """_SpeedTracker uses an injected clock so time-dependent tests are
    deterministic and run at full speed."""

    def _tracker(self) -> tuple[_SpeedTracker, list]:
        """Return a tracker wired to a mutable clock."""
        t: list[float] = [0.0]
        tracker = _SpeedTracker(_clock=lambda: t[0])
        return tracker, t

    def test_initial_speed_is_zero(self):
        tracker, _ = self._tracker()
        assert tracker.bps == 0.0

    def test_returns_false_before_sample_interval(self):
        tracker, t = self._tracker()
        t[0] = _SpeedTracker.SAMPLE_INTERVAL * 0.9  # just short of threshold
        updated = tracker.update(1000)
        assert updated is False
        assert tracker.bps == 0.0

    def test_returns_true_after_sample_interval(self):
        tracker, t = self._tracker()
        t[0] = _SpeedTracker.SAMPLE_INTERVAL + 0.01
        updated = tracker.update(1000)
        assert updated is True

    def test_speed_first_sample(self):
        """First sample: instant = bytes / elapsed; smooth = ALPHA * instant."""
        tracker, t = self._tracker()
        elapsed = _SpeedTracker.SAMPLE_INTERVAL  # exactly 0.5 s
        t[0] = elapsed
        tracker.update(1000)  # 1000 bytes in 0.5 s → instant = 2000 B/s
        expected = _SpeedTracker.ALPHA * 2000.0
        assert abs(tracker.bps - expected) < 1.0

    def test_smoothing_converges_to_constant_rate(self):
        """After repeated samples at a constant rate the estimate converges."""
        rate_bps = 1024 * 1024  # 1 MB/s
        tracker, t = self._tracker()

        for i in range(1, 20):
            t[0] = i * _SpeedTracker.SAMPLE_INTERVAL
            bytes_so_far = int(i * rate_bps * _SpeedTracker.SAMPLE_INTERVAL)
            tracker.update(bytes_so_far)

        # After 19 samples should be within 5% of the true rate.
        assert abs(tracker.bps - rate_bps) / rate_bps < 0.05

    def test_ignores_backward_bytes(self):
        """Negative delta (e.g. resumed from 0) must not produce negative speed."""
        tracker, t = self._tracker()
        t[0] = _SpeedTracker.SAMPLE_INTERVAL + 0.01
        tracker.update(5000)
        t[0] *= 2
        tracker.update(0)  # bytes reset to 0 — delta clamped to 0
        assert tracker.bps >= 0.0

    def test_multiple_samples_do_not_skip_unnecessarily(self):
        """Each call past the interval threshold should register an update."""
        tracker, t = self._tracker()
        updates = 0
        for i in range(1, 6):
            t[0] = i * _SpeedTracker.SAMPLE_INTERVAL
            if tracker.update(i * 1000):
                updates += 1
        assert updates == 5

    def test_update_does_not_mutate_on_skip(self):
        """A skipped update must leave bps unchanged."""
        tracker, t = self._tracker()
        t[0] = _SpeedTracker.SAMPLE_INTERVAL + 0.01
        tracker.update(1000)
        speed_after_first = tracker.bps

        # Advance time by less than one interval — should be skipped
        t[0] += _SpeedTracker.SAMPLE_INTERVAL * 0.3
        tracker.update(2000)
        assert tracker.bps == speed_after_first


# ── _compute_layout ───────────────────────────────────────────────────────────


class TestComputeLayout:
    def test_typical_terminal(self):
        centre_row, bar_width = _compute_layout(24, 80)
        assert centre_row == 7      # 24 // 2 - 5 = 7
        assert bar_width == 60      # min(60, 80 - 10) = 60

    def test_wide_terminal_caps_bar(self):
        _, bar_width = _compute_layout(24, 200)
        assert bar_width == 60      # capped at 60

    def test_narrow_terminal_floors_bar(self):
        _, bar_width = _compute_layout(24, 12)
        assert bar_width == 10      # max(10, min(60, 2)) = 10

    def test_narrow_terminal_exact_floor(self):
        _, bar_width = _compute_layout(24, 20)
        assert bar_width == 10      # max(10, min(60, 10)) = 10

    def test_very_short_terminal_clamps_row(self):
        centre_row, _ = _compute_layout(4, 80)
        assert centre_row == 0      # max(0, 4//2 - 5) = max(0, -3) = 0

    def test_tall_terminal(self):
        centre_row, _ = _compute_layout(50, 80)
        assert centre_row == 20     # 50 // 2 - 5 = 20

    def test_bar_width_matches_available_space(self):
        _, bar_width = _compute_layout(24, 45)
        # w - 10 = 35, min(60, 35) = 35, max(10, 35) = 35
        assert bar_width == 35


# ── _ProgressState ────────────────────────────────────────────────────────────


class TestProgressState:
    def test_initial_defaults(self, dummy_game):
        state = _ProgressState(game=dummy_game)
        assert state.phase == "Connecting"
        assert state.bytes_done == 0
        assert state.bytes_total == 0
        assert state.speed_bps == 0.0
        assert state.cancelled is False
        assert state.error is None
        assert state.result is None

    def test_update_sets_bytes(self, dummy_game):
        state = _ProgressState(game=dummy_game)
        state.update(500, 1000)
        assert state.bytes_done == 500
        assert state.bytes_total == 1000

    def test_update_nonzero_sets_downloading(self, dummy_game):
        state = _ProgressState(game=dummy_game)
        state.update(1, 1000)
        assert state.phase == "Downloading"

    def test_update_zero_bytes_does_not_set_downloading(self, dummy_game):
        state = _ProgressState(game=dummy_game)
        state.update(0, 1000)
        assert state.phase == "Connecting"

    def test_update_is_thread_safe(self, dummy_game):
        """Concurrent updates from multiple threads must not raise or corrupt."""
        state = _ProgressState(game=dummy_game)
        errors: list[Exception] = []

        def updater(start: int, n: int) -> None:
            for i in range(start, start + n):
                try:
                    state.update(i, 100_000)
                except Exception as exc:  # noqa: BLE001
                    errors.append(exc)

        threads = [
            threading.Thread(target=updater, args=(i * 1000, 1000))
            for i in range(6)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == []
        assert 0 <= state.bytes_done <= 6_000


# ── formatters ────────────────────────────────────────────────────────────────


class TestFmtBytes:
    def test_bytes(self):
        assert _fmt_bytes(500) == "500 B"

    def test_kilobytes(self):
        assert _fmt_bytes(1024) == "1 KB"
        assert _fmt_bytes(2048) == "2 KB"

    def test_megabytes(self):
        assert _fmt_bytes(1024 ** 2) == "1 MB"
        assert _fmt_bytes(10 * 1024 ** 2) == "10 MB"

    def test_gigabytes(self):
        assert _fmt_bytes(1024 ** 3) == "1.0 GB"
        assert _fmt_bytes(int(3.5 * 1024 ** 3)) == "3.5 GB"

    def test_zero(self):
        assert _fmt_bytes(0) == "0 B"

    def test_boundary_kb(self):
        # 1023 B → "B"; 1024 B → "KB"
        assert "B" in _fmt_bytes(1023)
        assert "KB" in _fmt_bytes(1024)

    def test_boundary_mb(self):
        assert "KB" in _fmt_bytes(1024 ** 2 - 1)
        assert "MB" in _fmt_bytes(1024 ** 2)


class TestFmtEta:
    def test_seconds(self):
        assert _fmt_eta(45.0) == "45s"
        assert _fmt_eta(1.0) == "1s"

    def test_minutes(self):
        assert _fmt_eta(60.0) == "1m"
        assert _fmt_eta(150.0) == "2m"

    def test_hours(self):
        assert _fmt_eta(3600.0) == "1h"
        assert _fmt_eta(7200.0) == "2h"

    def test_boundary_minutes(self):
        assert "s" in _fmt_eta(59.9)
        assert "m" in _fmt_eta(60.0)

    def test_boundary_hours(self):
        assert "m" in _fmt_eta(3599.0)
        assert "h" in _fmt_eta(3600.0)


# ── timing constants ──────────────────────────────────────────────────────────


class TestTimingConstants:
    def test_frame_interval_in_expected_range(self):
        """Display update should be between 5 and 10 FPS."""
        fps = 1.0 / _FRAME_INTERVAL
        assert 5 <= fps <= 10, f"FPS {fps:.1f} outside 5–10 range"

    def test_input_interval_faster_than_frame(self):
        """Input must be polled more frequently than display is refreshed."""
        assert _INPUT_INTERVAL < _FRAME_INTERVAL

    def test_input_interval_reasonable(self):
        """Input poll should be at least 20 Hz (50 ms) and no faster than 200 Hz."""
        hz = 1.0 / _INPUT_INTERVAL
        assert 20 <= hz <= 200, f"Input poll {hz:.0f} Hz outside reasonable range"
