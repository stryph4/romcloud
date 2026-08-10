"""Unit tests for ports_gfx.input_keyboard — keyboard -> semantic action."""

from __future__ import annotations

from ports_gfx.actions import Action
from ports_gfx.input_keyboard import action_for_key, action_for_key_up, text_for_input_event
from tests.unit._pygame_fakes import FakeEvent, make_fake_pygame


class TestActionForKey:
    def test_arrow_keys_map_to_directions(self):
        pygame = make_fake_pygame()
        assert action_for_key(pygame, pygame.K_UP) == Action.UP
        assert action_for_key(pygame, pygame.K_DOWN) == Action.DOWN
        assert action_for_key(pygame, pygame.K_LEFT) == Action.LEFT
        assert action_for_key(pygame, pygame.K_RIGHT) == Action.RIGHT

    def test_wasd_keys_map_to_directions(self):
        pygame = make_fake_pygame()
        assert action_for_key(pygame, pygame.K_w) == Action.UP
        assert action_for_key(pygame, pygame.K_s) == Action.DOWN
        assert action_for_key(pygame, pygame.K_a) == Action.LEFT
        assert action_for_key(pygame, pygame.K_d) == Action.RIGHT

    def test_return_and_space_confirm(self):
        pygame = make_fake_pygame()
        assert action_for_key(pygame, pygame.K_RETURN) == Action.CONFIRM
        assert action_for_key(pygame, pygame.K_SPACE) == Action.CONFIRM
        assert action_for_key(pygame, pygame.K_KP_ENTER) == Action.CONFIRM

    def test_escape_is_back(self):
        pygame = make_fake_pygame()
        assert action_for_key(pygame, pygame.K_ESCAPE) == Action.BACK

    def test_tab_is_menu(self):
        pygame = make_fake_pygame()
        assert action_for_key(pygame, pygame.K_TAB) == Action.MENU

    def test_backspace_is_back_outside_text_mode(self):
        pygame = make_fake_pygame()
        assert action_for_key(pygame, pygame.K_BACKSPACE) == Action.BACK

    def test_backspace_is_text_backspace_in_text_mode(self):
        pygame = make_fake_pygame()
        assert action_for_key(pygame, pygame.K_BACKSPACE, text_mode=True) == Action.TEXT_BACKSPACE

    def test_unmapped_key_returns_none(self):
        pygame = make_fake_pygame()
        assert action_for_key(pygame, 999999) is None

    def test_missing_constant_on_this_pygame_build_is_skipped_not_crashed(self):
        pygame = make_fake_pygame()
        del pygame.K_TAB
        assert action_for_key(pygame, 4242) is None


class TestActionForKeyUp:
    def test_confirm_keys_release_to_confirm_released(self):
        pygame = make_fake_pygame()
        assert action_for_key_up(pygame, pygame.K_RETURN) == Action.CONFIRM_RELEASED
        assert action_for_key_up(pygame, pygame.K_SPACE) == Action.CONFIRM_RELEASED
        assert action_for_key_up(pygame, pygame.K_KP_ENTER) == Action.CONFIRM_RELEASED

    def test_non_confirm_key_release_is_none(self):
        pygame = make_fake_pygame()
        assert action_for_key_up(pygame, pygame.K_ESCAPE) is None
        assert action_for_key_up(pygame, pygame.K_UP) is None


class TestTextForInputEvent:
    def test_extracts_typed_text(self):
        event = FakeEvent(text="a")
        assert text_for_input_event(event) == "a"

    def test_empty_text_returns_none(self):
        event = FakeEvent(text="")
        assert text_for_input_event(event) is None

    def test_missing_text_attribute_returns_none(self):
        event = FakeEvent()
        assert text_for_input_event(event) is None
