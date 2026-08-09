"""Unit tests for `ports_gfx.layout` — pure responsive geometry, no pygame.

Exercises the resolutions that actually matter for Batocera Ports: 720p/
1080p/4K TVs, Steam Deck-class handhelds, and unusual aspect ratios — to
prove nothing clips outside the screen and text never becomes unreadably
tiny, without ever touching pygame or a real display.
"""

from __future__ import annotations

import pytest

from ports_gfx.layout import (
    Rect,
    compute_card_rects,
    compute_columns,
    compute_font_sizes,
    compute_layout,
    compute_safe_area,
    find_next_focus_index,
)

# (width, height, label) — representative real-world targets.
_RESOLUTIONS = [
    (1280, 720, "720p"),
    (1920, 1080, "1080p"),
    (3840, 2160, "4K"),
    (1280, 800, "steam-deck"),
    (800, 480, "steam-deck-small"),
    (3440, 1440, "ultrawide"),
    (600, 1200, "narrow-portrait-ish"),
]


class TestSafeArea:
    @pytest.mark.parametrize("w,h,label", _RESOLUTIONS)
    def test_safe_area_stays_within_screen_bounds(self, w, h, label):
        area = compute_safe_area(w, h)
        assert area.x >= 0 and area.y >= 0
        assert area.x + area.w <= w
        assert area.y + area.h <= h
        assert area.w > 0 and area.h > 0

    def test_margin_scales_up_on_larger_screens(self):
        small = compute_safe_area(1280, 720)
        large = compute_safe_area(3840, 2160)
        assert large.x >= small.x

    def test_margin_never_exceeds_max_clamp_on_huge_screens(self):
        area = compute_safe_area(7680, 4320)  # 8K
        assert area.x <= 120


class TestFontSizes:
    @pytest.mark.parametrize("w,h,label", _RESOLUTIONS)
    def test_fonts_never_below_minimum_readable_size(self, w, h, label):
        fonts = compute_font_sizes(h)
        assert fonts.body >= 14
        assert fonts.title >= 14
        assert fonts.hint >= 14

    @pytest.mark.parametrize("w,h,label", _RESOLUTIONS)
    def test_fonts_never_exceed_maximum_clamp(self, w, h, label):
        fonts = compute_font_sizes(h)
        assert fonts.body <= 56
        assert fonts.title <= 56
        assert fonts.hint <= 56

    def test_title_larger_than_body_larger_than_hint(self):
        fonts = compute_font_sizes(1080)
        assert fonts.title > fonts.body > fonts.hint

    def test_fonts_scale_up_with_screen_height(self):
        small = compute_font_sizes(720)
        large = compute_font_sizes(2160)
        assert large.body >= small.body


class TestColumns:
    def test_never_more_columns_than_items(self):
        area = compute_safe_area(3840, 2160)
        assert compute_columns(area, 2) <= 2

    def test_narrow_screen_falls_back_to_single_column(self):
        area = compute_safe_area(500, 900)
        assert compute_columns(area, 5) == 1

    def test_wide_screen_allows_multiple_columns(self):
        area = compute_safe_area(3840, 2160, )
        assert compute_columns(area, 5) > 1

    def test_zero_items_returns_at_least_one(self):
        area = compute_safe_area(1920, 1080)
        assert compute_columns(area, 0) >= 1


class TestCardRects:
    @pytest.mark.parametrize("w,h,label", _RESOLUTIONS)
    def test_no_card_clips_outside_the_safe_area(self, w, h, label):
        area = compute_safe_area(w, h)
        columns = compute_columns(area, 5)
        rects = compute_card_rects(area, 5, columns)
        for rect in rects:
            assert rect.x >= area.x
            assert rect.y >= area.y
            assert rect.x + rect.w <= area.x + area.w
            assert rect.y + rect.h <= area.y + area.h

    def test_returns_one_rect_per_item(self):
        area = compute_safe_area(1920, 1080)
        columns = compute_columns(area, 5)
        rects = compute_card_rects(area, 5, columns)
        assert len(rects) == 5

    def test_zero_items_returns_empty(self):
        area = compute_safe_area(1920, 1080)
        assert compute_card_rects(area, 0, 1) == []

    def test_cards_do_not_overlap(self):
        area = compute_safe_area(3840, 2160)
        columns = compute_columns(area, 6)
        rects = compute_card_rects(area, 6, columns)
        for i, a in enumerate(rects):
            for b in rects[i + 1 :]:
                overlap_x = a.x < b.x + b.w and b.x < a.x + a.w
                overlap_y = a.y < b.y + b.h and b.y < a.y + a.h
                assert not (overlap_x and overlap_y), f"{a} overlaps {b}"


class TestComputeLayoutIntegration:
    @pytest.mark.parametrize("w,h,label", _RESOLUTIONS)
    def test_layout_is_fully_self_consistent(self, w, h, label):
        layout = compute_layout(w, h, 5)
        assert layout.screen_w == w
        assert layout.screen_h == h
        assert len(layout.card_rects) == 5
        assert layout.columns >= 1
        # Message/hint rects must also stay within the real screen.
        assert layout.hint_rect.y + layout.hint_rect.h <= h
        assert layout.message_rect.y + layout.message_rect.h <= h


class TestFindNextFocusIndex:
    def _column_rects(self, count=5):
        return [Rect(x=0, y=i * 50, w=100, h=40) for i in range(count)]

    def test_down_moves_to_next_row(self):
        rects = self._column_rects()
        assert find_next_focus_index(rects, 0, 0, 1) == 1

    def test_up_moves_to_previous_row(self):
        rects = self._column_rects()
        assert find_next_focus_index(rects, 2, 0, -1) == 1

    def test_down_from_last_wraps_to_first(self):
        rects = self._column_rects()
        assert find_next_focus_index(rects, len(rects) - 1, 0, 1) == 0

    def test_up_from_first_wraps_to_last(self):
        rects = self._column_rects()
        assert find_next_focus_index(rects, 0, 0, -1) == len(rects) - 1

    def test_single_item_never_moves(self):
        rects = [Rect(x=0, y=0, w=100, h=40)]
        assert find_next_focus_index(rects, 0, 0, 1) == 0
        assert find_next_focus_index(rects, 0, 1, 0) == 0

    def test_no_rects_returns_current_index_unchanged(self):
        assert find_next_focus_index([], 3, 0, 1) == 3

    def test_grid_right_moves_to_adjacent_column_same_row(self):
        # 2x2 grid: (0,0) (1,0) / (0,1) (1,1)
        rects = [
            Rect(x=0, y=0, w=100, h=100),
            Rect(x=120, y=0, w=100, h=100),
            Rect(x=0, y=120, w=100, h=100),
            Rect(x=120, y=120, w=100, h=100),
        ]
        assert find_next_focus_index(rects, 0, 1, 0) == 1

    def test_grid_down_moves_to_same_column_next_row(self):
        rects = [
            Rect(x=0, y=0, w=100, h=100),
            Rect(x=120, y=0, w=100, h=100),
            Rect(x=0, y=120, w=100, h=100),
            Rect(x=120, y=120, w=100, h=100),
        ]
        assert find_next_focus_index(rects, 1, 0, 1) == 3

    def test_no_direction_is_a_no_op(self):
        rects = self._column_rects()
        assert find_next_focus_index(rects, 2, 0, 0) == 2
