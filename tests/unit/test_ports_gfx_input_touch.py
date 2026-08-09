"""Unit tests for ports_gfx.input_touch — touch/mouse hit-testing against
responsive widget bounds."""

from __future__ import annotations

from ports_gfx.input_touch import (
    PointerDebouncer,
    point_from_finger_event,
    point_from_mouse_event,
    resolve_hit,
)
from ports_gfx.layout import Rect
from tests.unit._pygame_fakes import FakeEvent


class TestPointNormalization:
    def test_mouse_event_uses_pixel_pos_directly(self):
        event = FakeEvent(pos=(120, 80))
        point = point_from_mouse_event(event)
        assert (point.x, point.y) == (120.0, 80.0)

    def test_finger_event_scales_normalized_coords_to_screen_size(self):
        event = FakeEvent(x=0.5, y=0.25)
        point = point_from_finger_event(event, screen_w=1920, screen_h=1080)
        assert point.x == 960.0
        assert point.y == 270.0

    def test_finger_event_scaling_follows_actual_responsive_screen_size(self):
        """The same normalized event resolves differently at a different
        resolution — never a hardcoded design resolution."""
        event = FakeEvent(x=1.0, y=1.0)
        assert point_from_finger_event(event, 1280, 720) == point_from_finger_event(event, 1280, 720)
        big = point_from_finger_event(event, 3840, 2160)
        small = point_from_finger_event(event, 1280, 720)
        assert (big.x, big.y) != (small.x, small.y)


class TestResolveHit:
    def _rects(self):
        return [Rect(x=0, y=0, w=100, h=100), Rect(x=100, y=0, w=100, h=100)]

    def test_hit_inside_first_widget(self):
        from ports_gfx.input_touch import TouchPoint

        assert resolve_hit(self._rects(), TouchPoint(x=50, y=50)) == 0

    def test_hit_inside_second_widget(self):
        from ports_gfx.input_touch import TouchPoint

        assert resolve_hit(self._rects(), TouchPoint(x=150, y=50)) == 1

    def test_miss_outside_every_widget(self):
        from ports_gfx.input_touch import TouchPoint

        assert resolve_hit(self._rects(), TouchPoint(x=500, y=500)) is None

    def test_empty_rects_never_crashes(self):
        from ports_gfx.input_touch import TouchPoint

        assert resolve_hit([], TouchPoint(x=1, y=1)) is None


class TestPointerDebouncer:
    def test_first_pointer_down_is_accepted(self):
        debouncer = PointerDebouncer(window_seconds=0.15)
        assert debouncer.should_handle(1.0) is True

    def test_second_pointer_down_within_window_is_rejected(self):
        debouncer = PointerDebouncer(window_seconds=0.15)
        debouncer.should_handle(1.0)
        assert debouncer.should_handle(1.05) is False

    def test_pointer_down_after_window_is_accepted_again(self):
        debouncer = PointerDebouncer(window_seconds=0.15)
        debouncer.should_handle(1.0)
        assert debouncer.should_handle(1.2) is True

    def test_collapses_synthetic_mouse_event_following_a_real_finger_event(self):
        """Regression: some SDL builds fire both FINGERDOWN and a
        synthesized MOUSEBUTTONDOWN for the same physical tap."""
        debouncer = PointerDebouncer()
        assert debouncer.should_handle(10.0) is True
        assert debouncer.should_handle(10.01) is False
