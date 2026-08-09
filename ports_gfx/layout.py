"""Responsive layout — pure geometry, no pygame, no I/O.

Batocera Ports run across wildly different screens: 720p/1080p TVs, 4K
TVs, Steam Deck-class handhelds (1280x800, and smaller in some modes), and
unusual aspect ratios (ultrawide monitors, 4:3 CRTs via scalers). Nothing
in this module is a fixed pixel coordinate or a fixed design canvas that
gets letterboxed — every position and size is derived from the *actual*
screen dimensions passed in, so the same code path is exercised whether
called with a real detected resolution or an arbitrary one in tests.

``app.py`` calls :func:`compute_layout` once at startup and again whenever
the window/display size changes, then renders purely from the returned
:class:`Layout` — it never hardcodes a position itself.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

# Reference height used only to scale font sizes proportionally — NOT a
# fixed design canvas; the safe area always spans the real screen.
_REFERENCE_HEIGHT = 1080

_MIN_FONT_PX = 14
_MAX_FONT_PX = 56
_BASE_BODY_FONT_PX = 28
_TITLE_FONT_SCALE = 1.6
_HINT_FONT_SCALE = 0.7

_SAFE_AREA_FRACTION = 0.06  # overscan-safe margin, each side
_MIN_SAFE_MARGIN_PX = 16
_MAX_SAFE_MARGIN_PX = 120

_MIN_CARD_WIDTH_PX = 260
_CARD_SPACING_FRACTION = 0.02
_CARD_HEIGHT_FRACTION = 0.14
_MIN_CARD_HEIGHT_PX = 56
_MAX_CARD_HEIGHT_PX = 140

_MESSAGE_AREA_FRACTION = 0.14  # bottom slice of the safe area, above the hint line


@dataclass(frozen=True)
class Rect:
    x: int
    y: int
    w: int
    h: int

    @property
    def center(self) -> tuple[float, float]:
        return (self.x + self.w / 2, self.y + self.h / 2)


@dataclass(frozen=True)
class FontSizes:
    title: int
    body: int
    hint: int


@dataclass(frozen=True)
class Layout:
    screen_w: int
    screen_h: int
    safe_area: Rect
    fonts: FontSizes
    columns: int
    card_rects: list[Rect]
    message_rect: Rect
    hint_rect: Rect


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def compute_safe_area(screen_w: int, screen_h: int) -> Rect:
    """Overscan-safe drawable area.

    Margin scales with screen size but is clamped so it never eats an
    unreasonable fraction of a tiny screen nor a negligible sliver of a
    huge one.
    """
    margin = int(_clamp(min(screen_w, screen_h) * _SAFE_AREA_FRACTION, _MIN_SAFE_MARGIN_PX, _MAX_SAFE_MARGIN_PX))
    return Rect(
        x=margin,
        y=margin,
        w=max(1, screen_w - 2 * margin),
        h=max(1, screen_h - 2 * margin),
    )


def compute_font_sizes(screen_h: int) -> FontSizes:
    """Scale font sizes with screen height, clamped to a readable range —
    never microscopic on 4K, never oversized/clipped on 720p or smaller."""
    scale = screen_h / _REFERENCE_HEIGHT
    body = int(_clamp(_BASE_BODY_FONT_PX * scale, _MIN_FONT_PX, _MAX_FONT_PX))
    title = int(_clamp(body * _TITLE_FONT_SCALE, _MIN_FONT_PX, _MAX_FONT_PX))
    hint = int(_clamp(body * _HINT_FONT_SCALE, _MIN_FONT_PX, _MAX_FONT_PX))
    return FontSizes(title=title, body=body, hint=hint)


def compute_columns(safe_area: Rect, item_count: int) -> int:
    """How many card columns fit the safe area's width, given a minimum
    readable card width — never more columns than there are items."""
    if item_count <= 0:
        return 1
    max_columns_by_width = max(1, safe_area.w // _MIN_CARD_WIDTH_PX)
    return max(1, min(item_count, max_columns_by_width))


def compute_card_rects(safe_area: Rect, item_count: int, columns: int) -> list[Rect]:
    """Grid of card rects, centered in the (upper portion of the) safe
    area, evenly spaced. Purely a function of geometry + counts, so focus
    navigation can reason about real widget positions without a renderer.
    """
    if item_count <= 0:
        return []

    rows = -(-item_count // columns)  # ceil division

    spacing = int(_clamp(safe_area.h * _CARD_SPACING_FRACTION, 4, 40))
    card_h = int(_clamp(safe_area.h * _CARD_HEIGHT_FRACTION, _MIN_CARD_HEIGHT_PX, _MAX_CARD_HEIGHT_PX))
    card_w = max(1, (safe_area.w - spacing * (columns - 1)) // columns)

    grid_area_h = int(safe_area.h * (1.0 - _MESSAGE_AREA_FRACTION))
    grid_h = rows * card_h + (rows - 1) * spacing
    start_y = safe_area.y + max(0, (grid_area_h - grid_h) // 2)

    rects: list[Rect] = []
    for index in range(item_count):
        row, col = divmod(index, columns)
        items_in_row = min(columns, item_count - row * columns)
        row_w = items_in_row * card_w + (items_in_row - 1) * spacing
        start_x = safe_area.x + max(0, (safe_area.w - row_w) // 2)
        x = start_x + col * (card_w + spacing)
        y = start_y + row * (card_h + spacing)
        rects.append(Rect(x=x, y=y, w=card_w, h=card_h))
    return rects


def compute_layout(screen_w: int, screen_h: int, item_count: int) -> Layout:
    safe_area = compute_safe_area(screen_w, screen_h)
    columns = compute_columns(safe_area, item_count)
    card_rects = compute_card_rects(safe_area, item_count, columns)
    fonts = compute_font_sizes(screen_h)

    message_h = int(safe_area.h * _MESSAGE_AREA_FRACTION * 0.6)
    hint_h = fonts.hint + 8
    message_rect = Rect(
        x=safe_area.x,
        y=safe_area.y + safe_area.h - hint_h - message_h,
        w=safe_area.w,
        h=message_h,
    )
    hint_rect = Rect(
        x=safe_area.x,
        y=safe_area.y + safe_area.h - hint_h,
        w=safe_area.w,
        h=hint_h,
    )

    return Layout(
        screen_w=screen_w,
        screen_h=screen_h,
        safe_area=safe_area,
        fonts=fonts,
        columns=columns,
        card_rects=card_rects,
        message_rect=message_rect,
        hint_rect=hint_rect,
    )


def find_next_focus_index(rects: Sequence[Rect], current_index: int, dx: int, dy: int) -> int:
    """Return the widget index to focus when moving in direction *(dx,
    dy)* from *current_index*, based on actual rendered rect positions —
    never array order. Falls back to wrapping to the extreme opposite end
    along the movement axis if nothing lies in that direction, preserving
    the familiar "wrap around" convention for D-pad/controller navigation.
    """
    if not rects:
        return current_index
    if len(rects) == 1:
        return 0
    if not (dx or dy):
        return current_index

    cx, cy = rects[current_index].center

    best_index = None
    best_score = None
    for i, rect in enumerate(rects):
        if i == current_index:
            continue
        ox, oy = rect.center
        vx, vy = ox - cx, oy - cy
        primary = vx * dx + vy * dy
        if primary <= 1e-6:
            continue  # not in the requested direction at all
        perpendicular = abs(vx * dy - vy * dx)
        score = primary + perpendicular * 2.0  # favor closely-aligned neighbors
        if best_score is None or score < best_score:
            best_score = score
            best_index = i

    if best_index is not None:
        return best_index

    def proj_opposite(rect: Rect) -> float:
        ox, oy = rect.center
        return (ox - cx) * (-dx) + (oy - cy) * (-dy)

    return max(range(len(rects)), key=lambda i: proj_opposite(rects[i]))
