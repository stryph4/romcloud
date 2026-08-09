"""Unit tests for ports_gfx.osk — the on-screen keyboard foundation."""

from __future__ import annotations

from ports_gfx.layout import compute_safe_area, find_next_focus_index
from ports_gfx.osk import MASK_CHAR, OskState, compute_layout_rects, compute_osk_layout


class TestBasicTextEntry:
    def test_char_key_inserts_lowercase_by_default(self):
        state = OskState()
        q_index = next(i for i, k in enumerate(state.keys) if k.kind == "char" and k.char == "q")
        state.activate(q_index)
        assert state.text == "q"

    def test_shift_capitalizes_next_char(self):
        state = OskState()
        shift_index = next(i for i, k in enumerate(state.keys) if k.kind == "shift")
        q_index = next(i for i, k in enumerate(state.keys) if k.kind == "char" and k.char == "q")
        state.activate(shift_index)
        state.activate(q_index)
        assert state.text == "Q"

    def test_shift_is_a_toggle(self):
        state = OskState()
        shift_index = next(i for i, k in enumerate(state.keys) if k.kind == "shift")
        state.activate(shift_index)
        assert state.shift is True
        state.activate(shift_index)
        assert state.shift is False

    def test_space_inserts_a_space(self):
        state = OskState()
        space_index = next(i for i, k in enumerate(state.keys) if k.kind == "space")
        state.activate(space_index)
        assert state.text == " "

    def test_backspace_removes_last_char(self):
        state = OskState(initial_text="hello")
        backspace_index = next(i for i, k in enumerate(state.keys) if k.kind == "backspace")
        state.activate(backspace_index)
        assert state.text == "hell"

    def test_backspace_on_empty_text_is_a_no_op(self):
        state = OskState()
        backspace_index = next(i for i, k in enumerate(state.keys) if k.kind == "backspace")
        state.activate(backspace_index)
        assert state.text == ""


class TestSymbolsPage:
    def test_symbols_toggle_switches_page(self):
        state = OskState()
        symbols_index = next(i for i, k in enumerate(state.keys) if k.kind == "symbols")
        state.activate(symbols_index)
        assert state.page == "symbols"
        assert any(k.kind == "char" and k.char == "1" for k in state.keys)

    def test_toggle_back_to_letters(self):
        state = OskState()
        symbols_index = next(i for i, k in enumerate(state.keys) if k.kind == "symbols")
        state.activate(symbols_index)
        symbols_index_2 = next(i for i, k in enumerate(state.keys) if k.kind == "symbols")
        state.activate(symbols_index_2)
        assert state.page == "letters"

    def test_symbols_key_label_reflects_current_page(self):
        state = OskState()
        symbols_key = next(k for k in state.keys if k.kind == "symbols")
        assert state.key_label(symbols_key) == "123"
        state.toggle_symbols()
        symbols_key2 = next(k for k in state.keys if k.kind == "symbols")
        assert state.key_label(symbols_key2) == "ABC"

    def test_digit_key_on_symbols_page_inserts_digit(self):
        state = OskState()
        state.toggle_symbols()
        one_index = next(i for i, k in enumerate(state.keys) if k.kind == "char" and k.char == "1")
        state.activate(one_index)
        assert state.text == "1"


class TestConfirmCancel:
    def test_confirm_key_sets_confirmed_flag(self):
        state = OskState()
        confirm_index = next(i for i, k in enumerate(state.keys) if k.kind == "confirm")
        state.activate(confirm_index)
        assert state.confirmed is True
        assert state.cancelled is False

    def test_cancel_key_sets_cancelled_flag(self):
        state = OskState()
        cancel_index = next(i for i, k in enumerate(state.keys) if k.kind == "cancel")
        state.activate(cancel_index)
        assert state.cancelled is True
        assert state.confirmed is False


class TestMaskedPassword:
    def test_unmasked_field_shows_text_directly(self):
        state = OskState(initial_text="secret")
        assert state.displayed_text == "secret"

    def test_masked_field_hides_text_by_default(self):
        state = OskState(initial_text="secret", masked=True)
        assert state.displayed_text == MASK_CHAR * len("secret")

    def test_mask_toggle_key_present_only_when_masked(self):
        masked_state = OskState(masked=True)
        assert any(k.kind == "mask" for k in masked_state.keys)
        plain_state = OskState(masked=False)
        assert not any(k.kind == "mask" for k in plain_state.keys)

    def test_mask_reveal_toggle_shows_and_hides(self):
        state = OskState(initial_text="secret", masked=True)
        mask_index = next(i for i, k in enumerate(state.keys) if k.kind == "mask")
        state.activate(mask_index)
        assert state.displayed_text == "secret"
        state.activate(mask_index)
        assert state.displayed_text == MASK_CHAR * len("secret")

    def test_mask_toggle_is_a_no_op_on_unmasked_field(self):
        state = OskState(initial_text="hello", masked=False)
        state.toggle_mask_reveal()
        assert state.displayed_text == "hello"


class TestPhysicalKeyboardWhileOskActive:
    def test_insert_text_appends_regardless_of_onscreen_shift_state(self):
        state = OskState()
        shift_index = next(i for i, k in enumerate(state.keys) if k.kind == "shift")
        state.activate(shift_index)  # on-screen shift active
        state.insert_text("Hello")  # physical keyboard already applied its own case
        assert state.text == "Hello"

    def test_physical_and_onscreen_input_share_the_same_buffer(self):
        state = OskState()
        state.insert_text("abc")
        space_index = next(i for i, k in enumerate(state.keys) if k.kind == "space")
        state.activate(space_index)
        state.insert_text("def")
        assert state.text == "abc def"


class TestOskLayoutGeometry:
    def test_produces_one_rect_per_key(self):
        state = OskState()
        rects = compute_layout_rects(state, 1920, 1080)
        assert len(rects) == len(state.keys)

    def test_all_rects_within_safe_area(self):
        state = OskState()
        safe_area = compute_safe_area(1920, 1080)
        rects = compute_osk_layout(safe_area, state.keys)
        for rect in rects:
            assert rect.x >= safe_area.x
            assert rect.y >= safe_area.y
            assert rect.x + rect.w <= safe_area.x + safe_area.w + 1
            assert rect.y + rect.h <= safe_area.y + safe_area.h + 1

    def test_layout_responsive_across_resolutions(self):
        state = OskState()
        small = compute_layout_rects(state, 1280, 720)
        large = compute_layout_rects(state, 3840, 2160)
        assert small != large


class TestOskControllerNavigation:
    def test_dpad_down_moves_focus_to_next_row(self):
        state = OskState()
        rects = compute_layout_rects(state, 1920, 1080)
        q_index = next(i for i, k in enumerate(state.keys) if k.kind == "char" and k.char == "q")
        state.select(q_index)
        new_index = find_next_focus_index(rects, state.selected_index, 0, 1)
        state.select(new_index)
        assert state.keys[state.selected_index].row == 1

    def test_navigation_stays_in_bounds(self):
        state = OskState()
        rects = compute_layout_rects(state, 1920, 1080)
        state.select(0)
        # Repeatedly moving up from the top row should not crash or go
        # out of range — wraps per find_next_focus_index's convention.
        for _ in range(5):
            new_index = find_next_focus_index(rects, state.selected_index, 0, -1)
            state.select(new_index)
        assert 0 <= state.selected_index < len(state.keys)


class TestOskTouchInput:
    def test_tap_resolves_to_a_key_and_activates_it(self):
        from ports_gfx.input_touch import TouchPoint, resolve_hit

        state = OskState()
        rects = compute_layout_rects(state, 1920, 1080)
        q_index = next(i for i, k in enumerate(state.keys) if k.kind == "char" and k.char == "q")
        rect = rects[q_index]
        point = TouchPoint(x=rect.x + 1, y=rect.y + 1)
        hit_index = resolve_hit(rects, point)
        assert hit_index == q_index
        state.activate(hit_index)
        assert state.text == "q"
