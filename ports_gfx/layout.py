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

_SAFE_AREA_FRACTION = 0.035  # overscan-safe margin, each side
_MIN_SAFE_MARGIN_PX = 16
_MAX_SAFE_MARGIN_PX = 64

_MIN_CARD_WIDTH_PX = 260
_CARD_SPACING_FRACTION = 0.02
_CARD_HEIGHT_FRACTION = 0.14
_MIN_CARD_HEIGHT_PX = 56
_MAX_CARD_HEIGHT_PX = 140

_MESSAGE_AREA_FRACTION = 0.14  # bottom slice of the safe area, above the hint line

_COMPACT_BREAKPOINT_PX = 960
_ACTIVITY_MIN_WIDTH_PX = 320
_PANE_GAP_FRACTION = 0.016
_BUTTON_HEIGHT_FRACTION = 0.075
MIN_CONTROL_HEIGHT_PX = 44
_MAX_BUTTON_HEIGHT_PX = 84
_OSK_HEIGHT_FRACTION = 0.42


@dataclass(frozen=True)
class Rect:
    x: int
    y: int
    w: int
    h: int

    @property
    def center(self) -> tuple[float, float]:
        return (self.x + self.w / 2, self.y + self.h / 2)

    @property
    def right(self) -> int:
        return self.x + self.w

    @property
    def bottom(self) -> int:
        return self.y + self.h

    def intersects(self, other: "Rect") -> bool:
        return (
            self.x < other.right
            and other.x < self.right
            and self.y < other.bottom
            and other.y < self.bottom
        )


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
    header_rect: Rect
    content_rect: Rect
    navigation_rect: Rect
    activity_rect: Rect | None
    footer_rect: Rect
    wizard_content_rect: Rect
    osk_rect: Rect
    compact: bool


@dataclass(frozen=True)
class WizardRegions:
    content: Rect
    help: Rect
    options: Rect
    footer: Rect
    back_button: Rect
    continue_button: Rect
    osk: Rect | None


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


def compute_vertical_control_rects(
    area: Rect,
    item_count: int,
    *,
    content_height: int | None = None,
) -> list[Rect]:
    """Single-column controls with room for two text lines and padding."""
    if item_count <= 0:
        return []
    gap = int(_clamp(area.h * 0.012, 6, 14))
    button_h = int(
        _clamp(
            area.h * _BUTTON_HEIGHT_FRACTION,
            MIN_CONTROL_HEIGHT_PX,
            _MAX_BUTTON_HEIGHT_PX,
        )
    )
    if content_height is not None:
        button_h = max(button_h, content_height)
    # If a long submenu needs more room, compress to the accessible minimum;
    # callers can scroll when it still does not fit.
    if item_count * button_h + (item_count - 1) * gap > area.h:
        button_h = max(
            MIN_CONTROL_HEIGHT_PX,
            (area.h - (item_count - 1) * gap) // item_count,
        )
    return [
        Rect(area.x, area.y + index * (button_h + gap), area.w, button_h)
        for index in range(item_count)
    ]


def compute_layout(screen_w: int, screen_h: int, item_count: int) -> Layout:
    safe_area = compute_safe_area(screen_w, screen_h)
    fonts = compute_font_sizes(screen_h)
    gap = int(_clamp(min(screen_w, screen_h) * _PANE_GAP_FRACTION, 10, 28))
    header_h = max(fonts.title + 8, int(safe_area.h * 0.075))
    hint_h = max(MIN_CONTROL_HEIGHT_PX, fonts.hint + 18)
    header_rect = Rect(safe_area.x, safe_area.y, safe_area.w, header_h)
    footer_rect = Rect(
        safe_area.x,
        safe_area.bottom - hint_h,
        safe_area.w,
        hint_h,
    )
    content_rect = Rect(
        safe_area.x,
        header_rect.bottom + gap,
        safe_area.w,
        max(1, footer_rect.y - gap - (header_rect.bottom + gap)),
    )

    compact = content_rect.w < _COMPACT_BREAKPOINT_PX
    activity_rect: Rect | None = None
    if not compact:
        nav_w = int(content_rect.w * 0.58)
        activity_w = content_rect.w - gap - nav_w
        if activity_w < _ACTIVITY_MIN_WIDTH_PX:
            activity_w = _ACTIVITY_MIN_WIDTH_PX
            nav_w = content_rect.w - gap - activity_w
        navigation_rect = Rect(content_rect.x, content_rect.y, max(1, nav_w), content_rect.h)
        activity_rect = Rect(
            navigation_rect.right + gap,
            content_rect.y,
            max(1, activity_w),
            content_rect.h,
        )
    else:
        navigation_rect = content_rect

    message_h = max(fonts.body + 8, int(navigation_rect.h * _MESSAGE_AREA_FRACTION * 0.5))
    message_rect = Rect(
        x=navigation_rect.x,
        y=navigation_rect.bottom - message_h,
        w=navigation_rect.w,
        h=message_h,
    )
    hint_rect = footer_rect
    controls_area = Rect(
        navigation_rect.x,
        navigation_rect.y,
        navigation_rect.w,
        max(1, message_rect.y - gap - navigation_rect.y),
    )
    two_line_height = fonts.body + fonts.hint + max(20, fonts.hint)
    card_rects = compute_vertical_control_rects(
        controls_area,
        item_count,
        content_height=two_line_height,
    )

    wizard_content_rect = Rect(
        navigation_rect.x,
        navigation_rect.y,
        navigation_rect.w,
        navigation_rect.h,
    )
    osk_h = int(wizard_content_rect.h * _OSK_HEIGHT_FRACTION)
    osk_rect = Rect(
        wizard_content_rect.x,
        wizard_content_rect.bottom - osk_h,
        wizard_content_rect.w,
        max(1, osk_h),
    )

    return Layout(
        screen_w=screen_w,
        screen_h=screen_h,
        safe_area=safe_area,
        fonts=fonts,
        columns=1,
        card_rects=card_rects,
        message_rect=message_rect,
        hint_rect=hint_rect,
        header_rect=header_rect,
        content_rect=content_rect,
        navigation_rect=navigation_rect,
        activity_rect=activity_rect,
        footer_rect=footer_rect,
        wizard_content_rect=wizard_content_rect,
        osk_rect=osk_rect,
        compact=compact,
    )


def compute_wizard_regions(layout: Layout, *, osk_visible: bool = False) -> WizardRegions:
    """Anchored wizard geometry shared by every setup page.

    ``content`` starts below the global title/header, while ``help`` and
    ``options`` reserve separate vertical bands inside it. Renderers must use
    these regions instead of guessing where wrapped instructions end, keeping
    the first option row clear on 720p TVs as well as compact displays.
    """
    gap = int(_clamp(min(layout.screen_w, layout.screen_h) * 0.012, 8, 18))
    button_gap = gap
    button_w = max(1, (layout.navigation_rect.w - button_gap) // 2)
    footer = layout.footer_rect
    back = Rect(footer.x, footer.y, button_w, footer.h)
    continue_button = Rect(
        back.right + button_gap,
        footer.y,
        max(1, footer.right - (back.right + button_gap)),
        footer.h,
    )
    osk = layout.osk_rect if osk_visible else None
    content_bottom = (osk.y - gap) if osk is not None else layout.wizard_content_rect.bottom
    content = Rect(
        layout.wizard_content_rect.x,
        layout.wizard_content_rect.y,
        layout.wizard_content_rect.w,
        max(1, content_bottom - layout.wizard_content_rect.y),
    )
    help_h = min(
        max(layout.fonts.body * 5, int(content.h * 0.34)),
        max(1, content.h - MIN_CONTROL_HEIGHT_PX - gap),
    )
    help = Rect(content.x, content.y, content.w, help_h)
    options = Rect(
        content.x,
        min(content.bottom, help.bottom + gap),
        content.w,
        max(1, content.bottom - (help.bottom + gap)),
    )
    return WizardRegions(
        content=content,
        help=help,
        options=options,
        footer=footer,
        back_button=back,
        continue_button=continue_button,
        osk=osk,
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
