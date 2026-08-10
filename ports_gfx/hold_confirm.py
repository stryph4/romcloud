"""Centralized, input-agnostic destructive-action confirmation widget.

Reusable for SaveSync upload/download, uninstall, purge, and any future
dangerous operation — never re-implemented per-feature. Pure state, no
pygame: consumes only the semantic :class:`~ports_gfx.actions.Action`
vocabulary, so it behaves identically regardless of whether Confirm came
from a keyboard Enter press or a controller A press.

Usage: call :func:`handle_hold_to_confirm_event` for every input event
while this screen is active, and :meth:`HoldToConfirmState.update` once
per frame with the elapsed time. ``confirmed`` becomes True the instant
Confirm has been held continuously for ``duration_seconds``; releasing
Confirm before then resets progress to zero. ``cancelled`` becomes True
immediately on Back/Cancel — no hold required to cancel.
"""

from __future__ import annotations

from dataclasses import dataclass

from ports_gfx.actions import Action
from ports_gfx.input_manager import InputEvent

DEFAULT_HOLD_DURATION_SECONDS = 3.0


@dataclass
class HoldToConfirmState:
    duration_seconds: float = DEFAULT_HOLD_DURATION_SECONDS
    held_seconds: float = 0.0
    pressed: bool = False
    confirmed: bool = False
    cancelled: bool = False

    @property
    def progress(self) -> float:
        """0.0..1.0 fraction of the hold duration completed so far."""
        if self.duration_seconds <= 0:
            return 1.0
        return min(1.0, max(0.0, self.held_seconds / self.duration_seconds))

    @property
    def is_settled(self) -> bool:
        return self.confirmed or self.cancelled

    def press(self) -> None:
        if not self.is_settled:
            self.pressed = True

    def release(self) -> None:
        self.pressed = False
        self.held_seconds = 0.0

    def cancel(self) -> None:
        if not self.confirmed:
            self.cancelled = True
            self.pressed = False

    def update(self, dt: float) -> None:
        if not self.pressed or self.is_settled:
            return
        self.held_seconds += max(0.0, dt)
        if self.held_seconds >= self.duration_seconds:
            self.confirmed = True
            self.pressed = False

    def reset(self) -> None:
        self.held_seconds = 0.0
        self.pressed = False
        self.confirmed = False
        self.cancelled = False


def handle_hold_to_confirm_event(ievent: InputEvent, state: HoldToConfirmState) -> None:
    """Translate one semantic input event into hold-to-confirm state
    changes. Controller B and keyboard Escape both already map to
    ``Action.BACK`` upstream — cancelling never requires a hold."""
    if ievent.action == Action.CONFIRM:
        state.press()
    elif ievent.action == Action.CONFIRM_RELEASED:
        state.release()
    elif ievent.action == Action.BACK:
        state.cancel()
