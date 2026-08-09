"""Unit tests for ports_gfx.input_repeat — held-direction repeat timing."""

from __future__ import annotations

from ports_gfx.input_repeat import HeldDirectionRepeater


class TestHeldDirectionRepeater:
    def test_press_new_direction_fires_immediately(self):
        repeater = HeldDirectionRepeater()
        assert repeater.press((0, -1)) is True

    def test_press_same_direction_again_does_not_re_fire(self):
        repeater = HeldDirectionRepeater()
        repeater.press((0, -1))
        assert repeater.press((0, -1)) is False

    def test_press_different_direction_fires_immediately(self):
        repeater = HeldDirectionRepeater()
        repeater.press((0, -1))
        assert repeater.press((1, 0)) is True

    def test_update_before_initial_delay_does_not_repeat(self):
        repeater = HeldDirectionRepeater(initial_delay=0.4, repeat_interval=0.12)
        repeater.press((0, -1))
        assert repeater.update(0.1) is False
        assert repeater.update(0.1) is False

    def test_update_reaches_initial_delay_fires_once(self):
        repeater = HeldDirectionRepeater(initial_delay=0.4, repeat_interval=0.12)
        repeater.press((0, -1))
        assert repeater.update(0.4) is True
        # Immediately again (same tick's worth of time) must not double-fire.
        assert repeater.update(0.0) is False

    def test_holding_produces_periodic_repeats_not_a_flood(self):
        repeater = HeldDirectionRepeater(initial_delay=0.4, repeat_interval=0.1)
        repeater.press((0, -1))
        fires = sum(1 for _ in range(10) if repeater.update(0.1))
        # 1.0s total held: first fire at 0.4s, then every 0.1s -> 0.5..1.0 = 6 more => 7
        assert fires == 7

    def test_release_stops_repeating(self):
        repeater = HeldDirectionRepeater(initial_delay=0.1, repeat_interval=0.1)
        repeater.press((0, -1))
        repeater.update(0.1)
        repeater.release()
        assert repeater.held_direction is None
        assert repeater.update(1.0) is False

    def test_update_with_nothing_held_is_a_no_op(self):
        repeater = HeldDirectionRepeater()
        assert repeater.update(5.0) is False

    def test_negative_dt_never_raises_or_goes_backwards(self):
        repeater = HeldDirectionRepeater(initial_delay=0.2, repeat_interval=0.1)
        repeater.press((0, -1))
        repeater.update(-1.0)
        assert repeater.update(0.2) is True
