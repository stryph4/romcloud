"""Unit tests for `ports_gfx.savesync_progress_popup` — pure protocol/state
parts only (no pygame), matching the existing precedent for
`ports_gfx.launch_progress`."""

from __future__ import annotations

from ports_gfx.savesync_progress_popup import (
    DismissTimer,
    SaveSyncProgressState,
    _FAILURE_DISMISS_SECONDS,
    _STREAM_CLOSED_GRACE_SECONDS,
    _SUCCESS_DISMISS_SECONDS,
    card_rect,
    activity_segment,
    parse_event,
    read_events,
    spinner_angle_degrees,
)


class TestParseEvent:
    def test_parses_stage_event(self):
        assert parse_event('{"stage": "Checking save changes\\u2026"}\n') == {
            "stage": "Checking save changes…"
        }

    def test_ignores_blank_lines(self):
        assert parse_event("\n") is None
        assert parse_event("   ") is None

    def test_ignores_malformed_json(self):
        assert parse_event("not json{{{") is None

    def test_ignores_non_object_json(self):
        assert parse_event("[1, 2, 3]") is None


class TestSaveSyncProgressStateApply:
    def test_initial_stage_text(self):
        state = SaveSyncProgressState()
        assert state.stage_text == "Checking save changes…"
        assert not state.done

    def test_stage_updates_are_applied_in_order(self):
        state = SaveSyncProgressState()
        state.apply({"stage": "Waiting for save data to settle…"})
        assert state.stage_text == "Waiting for save data to settle…"
        state.apply({"stage": "Preparing save sync…"})
        assert state.stage_text == "Preparing save sync…"

    def test_stage_with_byte_progress_selects_determinate_mode(self):
        state = SaveSyncProgressState()
        state.apply({"stage": "Downloading save…", "current": 4096, "total": 8192})
        assert state.current == 4096
        assert state.total == 8192

    def test_stage_without_complete_byte_progress_selects_indeterminate_mode(self):
        state = SaveSyncProgressState()
        state.apply({"stage": "Downloading save…", "current": 4096})
        assert state.current is None
        assert state.total is None

    def test_done_event_sets_done_and_ok(self):
        state = SaveSyncProgressState()
        state.apply({"event": "done", "ok": True})
        assert state.done
        assert state.ok

    def test_done_event_with_message_overrides_stage_text(self):
        state = SaveSyncProgressState()
        state.apply({"stage": "Uploading changed save…"})
        state.apply({"event": "done", "ok": False, "message": "Save sync failed."})
        assert state.done
        assert not state.ok
        assert state.stage_text == "Save sync failed."

    def test_done_event_without_message_preserves_last_stage_text(self):
        state = SaveSyncProgressState()
        state.apply({"stage": "No save changes detected."})
        state.apply({"event": "done", "ok": True})
        assert state.done
        assert state.ok
        assert state.stage_text == "No save changes detected."

    def test_unknown_event_kind_is_ignored(self):
        state = SaveSyncProgressState()
        state.apply({"event": "something-unexpected"})
        assert not state.done
        assert state.stage_text == "Checking save changes…"

    def test_snapshot_returns_consistent_tuple(self):
        state = SaveSyncProgressState()
        state.apply({"stage": "Finalizing sync…"})
        assert state.snapshot() == (
            "Finalizing sync…",
            False,
            True,
            False,
            None,
            None,
        )

    def test_mark_stream_closed_is_reflected_in_snapshot(self):
        state = SaveSyncProgressState()
        state.mark_stream_closed()
        _, _, _, stream_closed, _, _ = state.snapshot()
        assert stream_closed


class _FakeStream:
    def __init__(self, lines):
        self._lines = iter(lines)

    def readline(self):
        try:
            return next(self._lines)
        except StopIteration:
            return ""


class TestReadEvents:
    def test_applies_every_parsed_line_and_marks_stream_closed_on_eof(self):
        state = SaveSyncProgressState()
        stream = _FakeStream(
            [
                '{"stage": "Preparing save sync\\u2026"}\n',
                "\n",
                '{"event": "done", "ok": true, "message": "Save sync complete."}\n',
                "",
            ]
        )
        read_events(stream, state)
        assert state.done
        assert state.ok
        assert state.stage_text == "Save sync complete."
        assert state.stream_closed

    def test_marks_stream_closed_even_if_reading_raises(self):
        class _BrokenStream:
            def readline(self):
                raise OSError("broken pipe")

        state = SaveSyncProgressState()
        read_events(_BrokenStream(), state)
        assert state.stream_closed
        assert not state.done


class TestDismissTimer:
    def test_never_exits_while_neither_done_nor_stream_closed(self):
        timer = DismissTimer(clock=lambda: 0.0)
        assert not timer.should_exit(done=False, ok=True, stream_closed=False)

    def test_success_dismisses_after_success_window(self):
        clock_values = iter([0.0, _SUCCESS_DISMISS_SECONDS - 0.01, _SUCCESS_DISMISS_SECONDS])
        timer = DismissTimer(clock=lambda: next(clock_values))
        assert not timer.should_exit(done=True, ok=True, stream_closed=False)
        assert not timer.should_exit(done=True, ok=True, stream_closed=False)
        assert timer.should_exit(done=True, ok=True, stream_closed=False)

    def test_failure_dismisses_after_the_longer_failure_window(self):
        clock_values = iter([0.0, _SUCCESS_DISMISS_SECONDS + 0.01, _FAILURE_DISMISS_SECONDS])
        timer = DismissTimer(clock=lambda: next(clock_values))
        # A failure must stay visible past the (shorter) success window.
        assert not timer.should_exit(done=True, ok=False, stream_closed=False)
        assert not timer.should_exit(done=True, ok=False, stream_closed=False)
        assert timer.should_exit(done=True, ok=False, stream_closed=False)

    def test_stream_closed_without_done_is_a_bounded_safety_net(self):
        clock_values = iter([0.0, _STREAM_CLOSED_GRACE_SECONDS - 0.01, _STREAM_CLOSED_GRACE_SECONDS])
        timer = DismissTimer(clock=lambda: next(clock_values))
        assert not timer.should_exit(done=False, ok=True, stream_closed=True)
        assert not timer.should_exit(done=False, ok=True, stream_closed=True)
        assert timer.should_exit(done=False, ok=True, stream_closed=True)

    def test_done_takes_priority_over_stream_closed_timing(self):
        clock_values = iter([0.0, _SUCCESS_DISMISS_SECONDS])
        timer = DismissTimer(clock=lambda: next(clock_values))
        assert not timer.should_exit(done=True, ok=True, stream_closed=True)
        assert timer.should_exit(done=True, ok=True, stream_closed=True)


class TestCardRect:
    def test_card_is_centered(self):
        x, y, w, h = card_rect(1280, 720)
        assert x + w // 2 == 1280 // 2 or abs((x + w / 2) - 640) <= 1
        assert abs((y + h / 2) - 360) <= 1

    def test_card_never_exceeds_a_small_screen(self):
        x, y, w, h = card_rect(320, 240)
        assert w <= 320 - 40
        assert x >= 0


class TestSpinnerAngle:
    def test_wraps_between_zero_and_360(self):
        assert spinner_angle_degrees(0.0) == 0.0
        assert 0.0 <= spinner_angle_degrees(1234.5) < 360.0


class TestActivitySegment:
    def test_segment_is_visible_and_stays_inside_track(self):
        for elapsed in (0.0, 0.2, 0.7, 1.34, 10.0):
            offset, width = activity_segment(elapsed, 300)
            assert width > 0
            assert offset >= 0
            assert offset + width <= 300

    def test_segment_moves_during_indeterminate_animation(self):
        assert activity_segment(0.0, 300) != activity_segment(0.3, 300)
