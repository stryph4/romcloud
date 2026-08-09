"""Held-direction repeat timing — shared by D-pad and analog-stick
navigation so both behave identically and neither fires rapid accidental
repeats while held.

Pure Python, no pygame — a plain frame-time state machine so it is fully
unit-testable with synthetic ``dt`` values.
"""

from __future__ import annotations

from typing import Optional

DEFAULT_INITIAL_DELAY = 0.4
"""Seconds a direction must be held before the first repeat fires."""

DEFAULT_REPEAT_INTERVAL = 0.12
"""Seconds between repeats once repeating has started."""


class HeldDirectionRepeater:
    """Tracks a single "currently held direction" and decides when it
    should fire again.

    Usage: call :meth:`press` whenever a direction is newly detected (a
    fresh button-down, or an analog stick crossing the deadzone into a new
    direction) — this always fires immediately (the initial navigation
    step). Call :meth:`update` once per frame with the elapsed time while
    the direction is still held; it returns ``True`` at most once per
    repeat interval. Call :meth:`release` when the direction stops (button
    up, or the stick returns to/through the deadzone).
    """

    def __init__(
        self,
        *,
        initial_delay: float = DEFAULT_INITIAL_DELAY,
        repeat_interval: float = DEFAULT_REPEAT_INTERVAL,
    ) -> None:
        self._initial_delay = initial_delay
        self._repeat_interval = repeat_interval
        self._held_direction: Optional[tuple[int, int]] = None
        self._held_time = 0.0
        self._next_repeat_at: Optional[float] = None

    @property
    def held_direction(self) -> Optional[tuple[int, int]]:
        return self._held_direction

    def press(self, direction: tuple[int, int]) -> bool:
        """Register *direction* as newly held. Returns ``True`` (fire
        immediately) if this is a new direction, ``False`` if the same
        direction was already held (no duplicate immediate fire)."""
        if direction == self._held_direction:
            return False
        self._held_direction = direction
        self._held_time = 0.0
        self._next_repeat_at = self._initial_delay
        return True

    def release(self) -> None:
        self._held_direction = None
        self._held_time = 0.0
        self._next_repeat_at = None

    def update(self, dt: float) -> bool:
        """Advance the held-time clock by *dt* seconds. Returns ``True``
        exactly when a repeat should fire this tick."""
        if self._held_direction is None or self._next_repeat_at is None:
            return False
        self._held_time += max(0.0, dt)
        if self._held_time >= self._next_repeat_at:
            self._next_repeat_at += self._repeat_interval
            return True
        return False
