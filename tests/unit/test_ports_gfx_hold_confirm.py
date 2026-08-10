"""Unit tests for ports_gfx.hold_confirm — the centralized, input-agnostic
destructive-action confirmation widget."""

from __future__ import annotations

from ports_gfx.actions import Action
from ports_gfx.hold_confirm import (
    DEFAULT_HOLD_DURATION_SECONDS,
    HoldToConfirmState,
    handle_hold_to_confirm_event,
)
from ports_gfx.input_manager import InputEvent


class TestDefaults:
    def test_default_duration_is_three_seconds(self):
        assert DEFAULT_HOLD_DURATION_SECONDS == 3.0

    def test_starts_unconfirmed_uncancelled_zero_progress(self):
        state = HoldToConfirmState()
        assert state.progress == 0.0
        assert state.confirmed is False
        assert state.cancelled is False


class TestHoldProgression:
    def test_confirms_after_full_duration_held(self):
        state = HoldToConfirmState(duration_seconds=3.0)
        state.press()
        state.update(1.5)
        assert state.confirmed is False
        assert 0.4 < state.progress < 0.6
        state.update(1.5)
        assert state.confirmed is True
        assert state.progress == 1.0

    def test_update_without_press_does_nothing(self):
        state = HoldToConfirmState()
        state.update(5.0)
        assert state.confirmed is False
        assert state.progress == 0.0

    def test_releasing_before_duration_resets_progress(self):
        state = HoldToConfirmState(duration_seconds=3.0)
        state.press()
        state.update(2.0)
        state.release()
        assert state.progress == 0.0
        assert state.confirmed is False

    def test_repress_after_release_starts_from_zero(self):
        state = HoldToConfirmState(duration_seconds=3.0)
        state.press()
        state.update(2.9)
        state.release()
        state.press()
        state.update(1.0)
        assert state.confirmed is False
        assert abs(state.progress - (1.0 / 3.0)) < 1e-9


class TestCancellation:
    def test_cancel_is_immediate_no_hold_required(self):
        state = HoldToConfirmState()
        state.cancel()
        assert state.cancelled is True
        assert state.confirmed is False

    def test_cancel_while_holding_stops_progress(self):
        state = HoldToConfirmState(duration_seconds=3.0)
        state.press()
        state.update(1.0)
        state.cancel()
        state.update(5.0)
        assert state.cancelled is True
        assert state.confirmed is False

    def test_cancel_after_confirmed_is_a_no_op(self):
        state = HoldToConfirmState(duration_seconds=1.0)
        state.press()
        state.update(1.0)
        assert state.confirmed is True
        state.cancel()
        assert state.cancelled is False


class TestSettledStateIsSticky:
    def test_press_after_confirmed_does_not_reset(self):
        state = HoldToConfirmState(duration_seconds=1.0)
        state.press()
        state.update(1.0)
        state.press()
        state.update(0.1)
        assert state.confirmed is True

    def test_press_after_cancelled_does_nothing(self):
        state = HoldToConfirmState()
        state.cancel()
        state.press()
        state.update(10.0)
        assert state.confirmed is False


class TestReset:
    def test_reset_clears_all_state(self):
        state = HoldToConfirmState(duration_seconds=1.0)
        state.press()
        state.update(1.0)
        state.reset()
        assert state.confirmed is False
        assert state.cancelled is False
        assert state.progress == 0.0


class TestEventHandlerIsInputAgnostic:
    """Whether Confirm came from keyboard Enter or controller A, the
    resulting state transitions must be identical — the handler only ever
    consumes the semantic Action, never a device-specific signal."""

    def test_confirm_action_presses_regardless_of_source(self):
        state = HoldToConfirmState()
        handle_hold_to_confirm_event(InputEvent(action=Action.CONFIRM), state)
        state.update(0.5)
        assert state.held_seconds == 0.5

    def test_confirm_released_action_releases(self):
        state = HoldToConfirmState()
        handle_hold_to_confirm_event(InputEvent(action=Action.CONFIRM), state)
        state.update(1.0)
        handle_hold_to_confirm_event(InputEvent(action=Action.CONFIRM_RELEASED), state)
        assert state.held_seconds == 0.0
        assert state.pressed is False

    def test_back_action_cancels(self):
        state = HoldToConfirmState()
        handle_hold_to_confirm_event(InputEvent(action=Action.BACK), state)
        assert state.cancelled is True

    def test_unrelated_action_is_ignored(self):
        state = HoldToConfirmState()
        handle_hold_to_confirm_event(InputEvent(action=Action.UP), state)
        assert state.pressed is False
        assert state.cancelled is False

    def test_full_hold_to_confirm_sequence_via_events(self):
        state = HoldToConfirmState(duration_seconds=3.0)
        handle_hold_to_confirm_event(InputEvent(action=Action.CONFIRM), state)
        for _ in range(3):
            state.update(1.0)
        assert state.confirmed is True
