"""Touch/mouse → widget hit-testing.

Every normal clickable control must be directly tappable — no forced D-pad
emulation. Hit-testing always runs against the *actual* rendered widget
rects produced by ``layout.py`` for the current resolution/columns; there
is never a hardcoded interaction region for a specific screen size.

Touch and mouse events are normalized to the same pixel-space
:class:`TouchPoint` here because SDL surfaces them differently: mouse
events (`pygame.MOUSEBUTTONDOWN`/``MOUSEMOTION``) report ``event.pos`` in
screen pixels, while finger events (`pygame.FINGERDOWN`/``FINGERMOTION``)
report ``event.x``/``event.y`` as floats normalized to ``[0, 1]`` of the
window size — and some SDL builds additionally synthesize a mouse event
for every finger event (or vice versa), which is why :class:`PointerDebouncer`
exists: it collapses near-simultaneous pointer-down events into a single
logical tap regardless of which event source(s) actually fired.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Sequence

from ports_gfx.layout import Rect

DEFAULT_DEBOUNCE_SECONDS = 0.15


@dataclass(frozen=True)
class TouchPoint:
    x: float
    y: float


def point_from_mouse_event(event) -> TouchPoint:  # noqa: ANN001
    x, y = event.pos
    return TouchPoint(x=float(x), y=float(y))


def point_from_finger_event(event, screen_w: int, screen_h: int) -> TouchPoint:  # noqa: ANN001
    """``FINGERDOWN``/``FINGERMOTION`` coordinates are normalized to
    ``[0, 1]`` of the window — scale to actual pixels using the current
    (responsive) screen size, never a fixed design resolution."""
    return TouchPoint(x=float(event.x) * screen_w, y=float(event.y) * screen_h)


def resolve_hit(rects: Sequence[Rect], point: TouchPoint) -> Optional[int]:
    """Index of the widget rect containing *point*, or ``None`` if the tap
    landed outside every widget."""
    for index, rect in enumerate(rects):
        if rect.x <= point.x <= rect.x + rect.w and rect.y <= point.y <= rect.y + rect.h:
            return index
    return None


class PointerDebouncer:
    """Collapses near-simultaneous pointer-down events (e.g. a real
    ``FINGERDOWN`` plus an SDL-synthesized ``MOUSEBUTTONDOWN`` for the same
    physical tap) into a single logical activation.
    """

    def __init__(self, *, window_seconds: float = DEFAULT_DEBOUNCE_SECONDS) -> None:
        self._window_seconds = window_seconds
        self._last_accepted_at: Optional[float] = None

    def should_handle(self, now: float) -> bool:
        """Returns ``True`` (accept this pointer-down) at most once per
        debounce window; subsequent calls within the window return
        ``False``."""
        if self._last_accepted_at is not None and (now - self._last_accepted_at) < self._window_seconds:
            return False
        self._last_accepted_at = now
        return True
