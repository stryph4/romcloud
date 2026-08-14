"""Unit tests for ports_gfx.input_manager — the keyboard+controller+touch
façade that funnels every device into the same Action vocabulary."""

from __future__ import annotations

from pathlib import Path

from ports_gfx.actions import Action
from ports_gfx.input_manager import InputEvent, InputManager
from ports_gfx.layout import Rect
from tests.unit._pygame_fakes import FakeEvent, FakeJoystick, make_fake_pygame

_RECTS = [Rect(x=0, y=0, w=100, h=100), Rect(x=100, y=0, w=100, h=100)]


def _manager(tmp_path: Path, **pygame_kwargs) -> InputManager:
    pygame = make_fake_pygame(**pygame_kwargs)
    return InputManager(pygame, str(tmp_path / "romcloud" / "bin" / "romcloud"))


class TestKeyboardRouting:
    def test_arrow_key_maps_to_direction_action(self, tmp_path: Path):
        manager = _manager(tmp_path)
        pygame = manager._pygame
        event = FakeEvent(type=pygame.KEYDOWN, key=pygame.K_UP)
        result = manager.handle_event(event, screen_w=1920, screen_h=1080)
        assert result == InputEvent(action=Action.UP)
        assert manager.last_input_mode == "keyboard"

    def test_text_input_event_carries_text(self, tmp_path: Path):
        manager = _manager(tmp_path)
        pygame = manager._pygame
        event = FakeEvent(type=pygame.TEXTINPUT, text="q")
        result = manager.handle_event(event, screen_w=1920, screen_h=1080, text_mode=True)
        assert result.action == Action.TEXT_INPUT
        assert result.text == "q"

    def test_release_barrier_tracks_keyboard_press_until_key_up(self, tmp_path: Path):
        manager = _manager(tmp_path)
        pygame = manager._pygame

        manager.handle_event(
            FakeEvent(type=pygame.KEYDOWN, key=pygame.K_RETURN),
            screen_w=1920,
            screen_h=1080,
        )
        assert not manager.all_controls_released()

        manager.handle_event(
            FakeEvent(type=pygame.KEYUP, key=pygame.K_RETURN),
            screen_w=1920,
            screen_h=1080,
        )
        assert manager.all_controls_released()


class TestTouchRouting:
    def test_mouse_click_on_widget_focuses_and_confirms(self, tmp_path: Path):
        manager = _manager(tmp_path)
        pygame = manager._pygame
        event = FakeEvent(type=pygame.MOUSEBUTTONDOWN, pos=(150, 50))
        result = manager.handle_event(event, screen_w=1920, screen_h=1080, rects=_RECTS, now=1.0)
        assert result.action == Action.CONFIRM
        assert result.touch_index == 1
        assert manager.last_input_mode == "touch"

    def test_finger_tap_scales_to_screen_size(self, tmp_path: Path):
        manager = _manager(tmp_path)
        pygame = manager._pygame
        event = FakeEvent(type=pygame.FINGERDOWN, x=0.02, y=0.02)
        result = manager.handle_event(event, screen_w=200, screen_h=200, rects=_RECTS, now=1.0)
        assert result.action == Action.CONFIRM
        assert result.touch_index == 0

    def test_tap_outside_every_widget_produces_no_action(self, tmp_path: Path):
        manager = _manager(tmp_path)
        pygame = manager._pygame
        event = FakeEvent(type=pygame.MOUSEBUTTONDOWN, pos=(5000, 5000))
        result = manager.handle_event(event, screen_w=1920, screen_h=1080, rects=_RECTS, now=1.0)
        assert result == InputEvent()

    def test_pointer_debounce_collapses_duplicate_taps(self, tmp_path: Path):
        manager = _manager(tmp_path)
        pygame = manager._pygame
        first = FakeEvent(type=pygame.MOUSEBUTTONDOWN, pos=(50, 50))
        second = FakeEvent(type=pygame.FINGERDOWN, x=0.026, y=0.046)

        r1 = manager.handle_event(first, screen_w=1920, screen_h=1080, rects=_RECTS, now=1.0)
        r2 = manager.handle_event(second, screen_w=1920, screen_h=1080, rects=_RECTS, now=1.01)
        assert r1.action == Action.CONFIRM
        assert r2 == InputEvent()

    def test_pointer_release_ends_hold_to_confirm(self, tmp_path: Path):
        manager = _manager(tmp_path)
        pygame = manager._pygame

        result = manager.handle_event(
            FakeEvent(type=pygame.FINGERUP, x=0.5, y=0.5),
            screen_w=1920,
            screen_h=1080,
        )

        assert result.action == Action.CONFIRM_RELEASED
        assert manager.last_input_mode == "touch"

    def test_release_barrier_tracks_mouse_press_until_button_up(self, tmp_path: Path):
        manager = _manager(tmp_path)
        pygame = manager._pygame

        manager.handle_event(
            FakeEvent(type=pygame.MOUSEBUTTONDOWN, button=1, pos=(50, 50)),
            screen_w=1920,
            screen_h=1080,
            rects=_RECTS,
            now=1.0,
        )
        assert not manager.all_controls_released()

        manager.handle_event(
            FakeEvent(type=pygame.MOUSEBUTTONUP, button=1, pos=(50, 50)),
            screen_w=1920,
            screen_h=1080,
        )
        assert manager.all_controls_released()


class TestControllerRouting:
    def test_controller_event_routes_through_and_sets_mode(self, tmp_path: Path):
        joystick = FakeJoystick(guid="g1")
        manager = _manager(tmp_path, joysticks={0: joystick}, controller_indices=frozenset({0}))
        pygame = manager._pygame
        manager.handle_event(
            FakeEvent(type=pygame.CONTROLLERDEVICEADDED, device_index=0), screen_w=1920, screen_h=1080
        )
        event = FakeEvent(type=pygame.CONTROLLERBUTTONDOWN, instance_id=1, button=pygame.CONTROLLER_BUTTON_A)
        result = manager.handle_event(event, screen_w=1920, screen_h=1080)
        assert result.action == Action.CONFIRM
        assert manager.last_input_mode == "controller"

    def test_update_reports_controller_repeats(self, tmp_path: Path):
        joystick = FakeJoystick(guid="g1")
        manager = _manager(tmp_path, joysticks={0: joystick}, controller_indices=frozenset({0}))
        pygame = manager._pygame
        manager.handle_event(
            FakeEvent(type=pygame.CONTROLLERDEVICEADDED, device_index=0), screen_w=1920, screen_h=1080
        )
        manager.handle_event(
            FakeEvent(type=pygame.CONTROLLERBUTTONDOWN, instance_id=1, button=pygame.CONTROLLER_BUTTON_DPAD_DOWN),
            screen_w=1920,
            screen_h=1080,
        )
        assert manager.update(0.5) == [Action.DOWN]
        assert manager.last_input_mode == "controller"

    def test_release_barrier_tracks_confirm_until_controller_button_up(self, tmp_path: Path):
        joystick = FakeJoystick(guid="g1")
        manager = _manager(tmp_path, joysticks={0: joystick})
        pygame = manager._pygame
        manager.controllers.open_existing_devices(1)

        manager.handle_event(
            FakeEvent(type=pygame.JOYBUTTONDOWN, instance_id=1, button=0),
            screen_w=1920,
            screen_h=1080,
        )
        assert not manager.all_controls_released()

        manager.handle_event(
            FakeEvent(type=pygame.JOYBUTTONUP, instance_id=1, button=0),
            screen_w=1920,
            screen_h=1080,
        )
        assert manager.all_controls_released()


class TestInputModeSwitchingPreservesUnrelatedState:
    def test_switching_modes_does_not_reset_pointer_debounce_state(self, tmp_path: Path):
        """Regression: touch -> keyboard -> touch again must not corrupt
        the pointer debouncer or otherwise crash; focus itself lives in the
        caller's MenuState (untouched here), so mode switches can never
        clobber it."""
        manager = _manager(tmp_path)
        pygame = manager._pygame

        manager.handle_event(
            FakeEvent(type=pygame.MOUSEBUTTONDOWN, pos=(150, 50)), screen_w=1920, screen_h=1080, rects=_RECTS, now=1.0
        )
        assert manager.last_input_mode == "touch"

        manager.handle_event(FakeEvent(type=pygame.KEYDOWN, key=pygame.K_DOWN), screen_w=1920, screen_h=1080)
        assert manager.last_input_mode == "keyboard"

        result = manager.handle_event(
            FakeEvent(type=pygame.MOUSEBUTTONDOWN, pos=(50, 50)), screen_w=1920, screen_h=1080, rects=_RECTS, now=2.0
        )
        assert result.action == Action.CONFIRM
        assert result.touch_index == 0
        assert manager.last_input_mode == "touch"
