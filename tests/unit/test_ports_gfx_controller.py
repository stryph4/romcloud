"""Unit tests for ports_gfx.controller — logical/raw controller -> semantic
action translation, identity, deadzone, repeat, hot-plug, and remapping."""

from __future__ import annotations

from ports_gfx.actions import Action
from ports_gfx.controller import (
    ControllerManager,
    ControllerProfile,
    direction_from_axes,
    direction_from_hat,
    get_identity,
    normalize_axis_value,
)
from tests.unit._pygame_fakes import FakeEvent, FakeJoystick, make_fake_pygame


def _added_event(pygame, device_index=0, use_controller=False):
    event_type = pygame.CONTROLLERDEVICEADDED if use_controller else pygame.JOYDEVICEADDED
    return FakeEvent(type=event_type, device_index=device_index)


def _removed_event(pygame, instance_id, use_controller=False):
    event_type = pygame.CONTROLLERDEVICEREMOVED if use_controller else pygame.JOYDEVICEREMOVED
    return FakeEvent(type=event_type, instance_id=instance_id)


class TestControllerIdentity:
    def test_prefers_guid(self):
        joy = FakeJoystick(name="Xbox Wireless Controller", guid="030000005e0400008e02000010010000")
        identity = get_identity(joy)
        assert identity.guid == "030000005e0400008e02000010010000"
        assert identity.key == "guid:030000005e0400008e02000010010000"

    def test_falls_back_to_name_when_no_guid(self):
        joy = FakeJoystick(name="Generic Pad", guid=None)
        identity = get_identity(joy)
        assert identity.key == "name:Generic Pad"

    def test_missing_get_guid_method_does_not_crash(self):
        class NoGuidJoystick:
            def get_name(self):
                return "Old Pad"

        identity = get_identity(NoGuidJoystick())
        assert identity.name == "Old Pad"
        assert identity.guid is None

    def test_get_guid_raising_is_treated_as_unavailable(self):
        class BrokenJoystick(FakeJoystick):
            def get_guid(self):
                raise RuntimeError("boom")

        identity = get_identity(BrokenJoystick())
        assert identity.guid is None


class TestNormalizeAxisAndDeadzone:
    def test_normalize_full_positive_and_negative(self):
        assert normalize_axis_value(32767) == 1.0
        assert normalize_axis_value(-32768) == -1.0

    def test_normalize_clamped_within_range(self):
        value = normalize_axis_value(16000)
        assert 0.0 < value < 1.0

    def test_direction_from_axes_inside_deadzone_is_none(self):
        assert direction_from_axes(0.1, 0.1, deadzone=0.5) is None

    def test_direction_from_axes_dominant_axis_wins(self):
        assert direction_from_axes(0.9, 0.1, deadzone=0.5) == (1, 0)
        assert direction_from_axes(0.1, 0.9, deadzone=0.5) == (0, 1)

    def test_direction_from_axes_negative(self):
        assert direction_from_axes(-0.9, 0.0, deadzone=0.5) == (-1, 0)
        assert direction_from_axes(0.0, -0.9, deadzone=0.5) == (0, -1)

    def test_direction_from_hat_flips_y_convention(self):
        assert direction_from_hat(0, 1) == (0, -1)   # SDL "up" -> screen dy = -1
        assert direction_from_hat(0, -1) == (0, 1)
        assert direction_from_hat(1, 0) == (1, 0)
        assert direction_from_hat(-1, 0) == (-1, 0)
        assert direction_from_hat(0, 0) is None


class TestLogicalControllerNavigation:
    def test_dpad_button_navigates_and_starts_repeat(self):
        pygame = make_fake_pygame(joysticks={0: FakeJoystick(guid="g1")}, controller_indices=frozenset({0}))
        manager = ControllerManager(pygame)
        manager.handle_event(_added_event(pygame, 0, use_controller=True))

        down = FakeEvent(type=pygame.CONTROLLERBUTTONDOWN, instance_id=1, button=pygame.CONTROLLER_BUTTON_DPAD_UP)
        assert manager.handle_event(down) == Action.UP

    def test_button_up_stops_repeat(self):
        pygame = make_fake_pygame(joysticks={0: FakeJoystick(guid="g1")}, controller_indices=frozenset({0}))
        manager = ControllerManager(pygame)
        manager.handle_event(_added_event(pygame, 0, use_controller=True))
        down = FakeEvent(type=pygame.CONTROLLERBUTTONDOWN, instance_id=1, button=pygame.CONTROLLER_BUTTON_DPAD_UP)
        manager.handle_event(down)

        up = FakeEvent(type=pygame.CONTROLLERBUTTONUP, instance_id=1, button=pygame.CONTROLLER_BUTTON_DPAD_UP)
        manager.handle_event(up)
        assert manager.update(1.0) == []

    def test_a_button_confirms_b_button_back_start_is_menu(self):
        pygame = make_fake_pygame(joysticks={0: FakeJoystick(guid="g1")}, controller_indices=frozenset({0}))
        manager = ControllerManager(pygame)
        manager.handle_event(_added_event(pygame, 0, use_controller=True))

        confirm = FakeEvent(type=pygame.CONTROLLERBUTTONDOWN, instance_id=1, button=pygame.CONTROLLER_BUTTON_A)
        back = FakeEvent(type=pygame.CONTROLLERBUTTONDOWN, instance_id=1, button=pygame.CONTROLLER_BUTTON_B)
        menu = FakeEvent(type=pygame.CONTROLLERBUTTONDOWN, instance_id=1, button=pygame.CONTROLLER_BUTTON_START)
        assert manager.handle_event(confirm) == Action.CONFIRM
        assert manager.handle_event(back) == Action.BACK
        assert manager.handle_event(menu) == Action.MENU

    def test_shoulders_use_shared_page_actions(self):
        pygame = make_fake_pygame(joysticks={0: FakeJoystick(guid="g1")}, controller_indices=frozenset({0}))
        manager = ControllerManager(pygame)
        manager.handle_event(_added_event(pygame, 0, use_controller=True))

        previous = FakeEvent(
            type=pygame.CONTROLLERBUTTONDOWN,
            instance_id=1,
            button=pygame.CONTROLLER_BUTTON_LEFTSHOULDER,
        )
        following = FakeEvent(
            type=pygame.CONTROLLERBUTTONDOWN,
            instance_id=1,
            button=pygame.CONTROLLER_BUTTON_RIGHTSHOULDER,
        )
        assert manager.handle_event(previous) == Action.PREVIOUS_PAGE
        assert manager.handle_event(following) == Action.NEXT_PAGE

    def test_analog_stick_navigates_past_deadzone(self):
        pygame = make_fake_pygame(joysticks={0: FakeJoystick(guid="g1")}, controller_indices=frozenset({0}))
        manager = ControllerManager(pygame, deadzone=0.5)
        manager.handle_event(_added_event(pygame, 0, use_controller=True))

        small = FakeEvent(type=pygame.CONTROLLERAXISMOTION, instance_id=1, axis=pygame.CONTROLLER_AXIS_LEFTX, value=5000)
        assert manager.handle_event(small) is None

        big = FakeEvent(type=pygame.CONTROLLERAXISMOTION, instance_id=1, axis=pygame.CONTROLLER_AXIS_LEFTX, value=30000)
        assert manager.handle_event(big) == Action.RIGHT

    def test_analog_stick_returning_to_center_releases_repeat(self):
        pygame = make_fake_pygame(joysticks={0: FakeJoystick(guid="g1")}, controller_indices=frozenset({0}))
        manager = ControllerManager(pygame, deadzone=0.5)
        manager.handle_event(_added_event(pygame, 0, use_controller=True))
        manager.handle_event(
            FakeEvent(type=pygame.CONTROLLERAXISMOTION, instance_id=1, axis=pygame.CONTROLLER_AXIS_LEFTX, value=30000)
        )
        manager.handle_event(
            FakeEvent(type=pygame.CONTROLLERAXISMOTION, instance_id=1, axis=pygame.CONTROLLER_AXIS_LEFTX, value=0)
        )
        assert manager.update(1.0) == []

    def test_held_dpad_produces_repeat_via_update(self):
        pygame = make_fake_pygame(joysticks={0: FakeJoystick(guid="g1")}, controller_indices=frozenset({0}))
        manager = ControllerManager(pygame)
        manager.handle_event(_added_event(pygame, 0, use_controller=True))
        manager.handle_event(
            FakeEvent(type=pygame.CONTROLLERBUTTONDOWN, instance_id=1, button=pygame.CONTROLLER_BUTTON_DPAD_DOWN)
        )
        assert manager.update(0.1) == []
        assert manager.update(0.4) == [Action.DOWN]


class TestHotPlug:
    def test_connect_registers_device_and_disconnect_removes_it(self):
        pygame = make_fake_pygame(joysticks={0: FakeJoystick(guid="g1")}, controller_indices=frozenset({0}))
        manager = ControllerManager(pygame)
        assert manager.device_count == 0
        manager.handle_event(_added_event(pygame, 0, use_controller=True))
        assert manager.device_count == 1

        manager.handle_event(_removed_event(pygame, instance_id=1, use_controller=True))
        assert manager.device_count == 0

    def test_events_for_a_disconnected_device_are_ignored_not_crashed(self):
        pygame = make_fake_pygame(joysticks={0: FakeJoystick(guid="g1")}, controller_indices=frozenset({0}))
        manager = ControllerManager(pygame)
        manager.handle_event(_added_event(pygame, 0, use_controller=True))
        manager.handle_event(_removed_event(pygame, instance_id=1, use_controller=True))

        stale = FakeEvent(type=pygame.CONTROLLERBUTTONDOWN, instance_id=1, button=pygame.CONTROLLER_BUTTON_A)
        assert manager.handle_event(stale) is None

    def test_reconnect_same_guid_reloads_its_custom_mapping(self):
        loaded_keys = []

        def loader(key):
            loaded_keys.append(key)
            return {"button": {"5": "confirm"}} if key == "guid:g1" else None

        pygame = make_fake_pygame(joysticks={0: FakeJoystick(guid="g1")}, controller_indices=frozenset())
        manager = ControllerManager(pygame, load_mapping=loader)
        manager.handle_event(_added_event(pygame, 0))
        manager.handle_event(_removed_event(pygame, instance_id=1))
        manager.handle_event(_added_event(pygame, 0))

        assert "guid:g1" in loaded_keys
        custom_button = FakeEvent(type=pygame.JOYBUTTONDOWN, instance_id=1, button=5)
        assert manager.handle_event(custom_button) == Action.CONFIRM

    def test_duplicate_added_event_for_same_device_does_not_reset_state(self):
        pygame = make_fake_pygame(joysticks={0: FakeJoystick(guid="g1")}, controller_indices=frozenset({0}))
        manager = ControllerManager(pygame)
        manager.handle_event(_added_event(pygame, 0, use_controller=True))
        manager.handle_event(
            FakeEvent(type=pygame.CONTROLLERBUTTONDOWN, instance_id=1, button=pygame.CONTROLLER_BUTTON_DPAD_UP)
        )
        # A second "added" notification for the same device (JOYDEVICEADDED
        # firing alongside CONTROLLERDEVICEADDED) must not wipe held state.
        manager.handle_event(_added_event(pygame, 0, use_controller=False))
        assert manager.snapshots()[0].held_direction == (0, -1)


class TestRawFallback:
    def test_unrecognized_pad_uses_raw_button_fallback(self):
        pygame = make_fake_pygame(joysticks={0: FakeJoystick(guid="g2")}, controller_indices=frozenset())
        manager = ControllerManager(pygame)
        manager.handle_event(_added_event(pygame, 0))

        confirm = FakeEvent(type=pygame.JOYBUTTONDOWN, instance_id=1, button=0)
        back = FakeEvent(type=pygame.JOYBUTTONDOWN, instance_id=1, button=1)
        other = FakeEvent(type=pygame.JOYBUTTONDOWN, instance_id=1, button=3)
        assert manager.handle_event(confirm) == Action.CONFIRM
        assert manager.handle_event(back) == Action.BACK
        assert manager.handle_event(other) is None

    def test_raw_hat_navigates(self):
        pygame = make_fake_pygame(joysticks={0: FakeJoystick(guid="g2")}, controller_indices=frozenset())
        manager = ControllerManager(pygame)
        manager.handle_event(_added_event(pygame, 0))

        hat_up = FakeEvent(type=pygame.JOYHATMOTION, instance_id=1, value=(0, 1))
        assert manager.handle_event(hat_up) == Action.UP

    def test_raw_axis_navigates(self):
        pygame = make_fake_pygame(joysticks={0: FakeJoystick(guid="g2")}, controller_indices=frozenset())
        manager = ControllerManager(pygame, deadzone=0.5)
        manager.handle_event(_added_event(pygame, 0))

        axis = FakeEvent(type=pygame.JOYAXISMOTION, instance_id=1, axis=1, value=-30000)
        assert manager.handle_event(axis) == Action.UP

    def test_no_controller_module_falls_back_cleanly(self):
        pygame = make_fake_pygame(joysticks={0: FakeJoystick(guid="g3")}, has_controller_module=False)
        manager = ControllerManager(pygame)
        manager.handle_event(_added_event(pygame, 0))
        confirm = FakeEvent(type=pygame.JOYBUTTONDOWN, instance_id=1, button=0)
        assert manager.handle_event(confirm) == Action.CONFIRM
        assert manager.snapshots()[0].is_game_controller is False

    def test_is_controller_raising_falls_back_to_joystick(self):
        pygame = make_fake_pygame(joysticks={0: FakeJoystick(guid="g4")}, controller_indices=frozenset({0}))

        def boom(_index):
            raise RuntimeError("SDL mapping DB unavailable")

        pygame.controller.is_controller = boom
        manager = ControllerManager(pygame)
        manager.handle_event(_added_event(pygame, 0, use_controller=True))
        assert manager.snapshots()[0].is_game_controller is False


class TestNoControllerPresent:
    def test_snapshots_empty_when_nothing_connected(self):
        pygame = make_fake_pygame()
        manager = ControllerManager(pygame)
        assert manager.snapshots() == []

    def test_update_with_no_devices_returns_empty_list(self):
        pygame = make_fake_pygame()
        manager = ControllerManager(pygame)
        assert manager.update(1.0) == []

    def test_unrelated_event_types_are_ignored(self):
        pygame = make_fake_pygame()
        manager = ControllerManager(pygame)
        assert manager.handle_event(FakeEvent(type=pygame.KEYDOWN, key=1)) is None


class TestRemap:
    def test_begin_remap_requires_connected_device(self):
        pygame = make_fake_pygame(joysticks={0: FakeJoystick(guid="g5")}, controller_indices=frozenset({0}))
        manager = ControllerManager(pygame)
        assert manager.begin_remap(1, Action.UP) is False
        manager.handle_event(_added_event(pygame, 0, use_controller=True))
        assert manager.begin_remap(1, Action.UP) is True

    def test_next_button_press_is_captured_and_persisted(self):
        saved = {}

        def saver(key, mapping):
            saved[key] = mapping

        pygame = make_fake_pygame(joysticks={0: FakeJoystick(guid="g6")}, controller_indices=frozenset({0}))
        manager = ControllerManager(pygame, save_mapping=saver)
        manager.handle_event(_added_event(pygame, 0, use_controller=True))
        manager.begin_remap(1, Action.UP)

        capture_event = FakeEvent(type=pygame.CONTROLLERBUTTONDOWN, instance_id=1, button=99)
        result = manager.handle_event(capture_event)

        assert result is None  # captured, not dispatched as a normal action
        assert manager.remap_pending_action is None
        assert saved["guid:g6"]["button"]["99"] == "up"

        # The newly-bound button now fires the action directly.
        again = FakeEvent(type=pygame.CONTROLLERBUTTONDOWN, instance_id=1, button=99)
        assert manager.handle_event(again) == Action.UP

    def test_cancel_remap_stops_capture(self):
        pygame = make_fake_pygame(joysticks={0: FakeJoystick(guid="g7")}, controller_indices=frozenset({0}))
        manager = ControllerManager(pygame)
        manager.handle_event(_added_event(pygame, 0, use_controller=True))
        manager.begin_remap(1, Action.UP)
        manager.cancel_remap()
        assert manager.remap_pending_action is None

        # Without a pending remap, this button press resolves normally
        # (CONTROLLER_BUTTON A isn't bound to anything meaningful here, so
        # it should just fall through to None rather than being captured).
        event = FakeEvent(type=pygame.CONTROLLERBUTTONDOWN, instance_id=1, button=123456)
        assert manager.handle_event(event) is None

    def test_hat_capture_for_remap(self):
        saved = {}
        pygame = make_fake_pygame(joysticks={0: FakeJoystick(guid="g8")}, controller_indices=frozenset())
        manager = ControllerManager(pygame, save_mapping=lambda k, m: saved.setdefault(k, m))
        manager.handle_event(_added_event(pygame, 0))
        manager.begin_remap(1, Action.LEFT)

        hat_event = FakeEvent(type=pygame.JOYHATMOTION, instance_id=1, value=(-1, 0))
        assert manager.handle_event(hat_event) is None
        assert saved["guid:g8"]["hat"]["-1,0"] == "left"


class TestSnapshotFields:
    def test_snapshot_reports_identity_and_axis_state(self):
        pygame = make_fake_pygame(
            joysticks={0: FakeJoystick(name="Test Pad", guid="g9")}, controller_indices=frozenset({0})
        )
        manager = ControllerManager(pygame, deadzone=0.5)
        manager.handle_event(_added_event(pygame, 0, use_controller=True))
        manager.handle_event(
            FakeEvent(type=pygame.CONTROLLERAXISMOTION, instance_id=1, axis=pygame.CONTROLLER_AXIS_LEFTX, value=32767)
        )

        snap = manager.snapshots()[0]
        assert snap.identity.name == "Test Pad"
        assert snap.identity.guid == "g9"
        assert snap.is_game_controller is True
        assert snap.using_custom_mapping is False
        assert snap.axis_x == 1.0
        assert snap.last_action == Action.RIGHT
        assert snap.instance_id == 1


class TestRawPrimaryOnRecognizedGameController:
    """Raw JOY* events must always be handled, even for a device SDL
    recognizes as a game controller — this is the actual Batocera hardware
    fix: pygame.controller detects the pad but its CONTROLLER* events don't
    reliably arrive, so raw joystick events must still drive navigation."""

    def test_joy_hat_navigates_on_recognized_game_controller(self):
        pygame = make_fake_pygame(joysticks={0: FakeJoystick(guid="gc1")}, controller_indices=frozenset({0}))
        manager = ControllerManager(pygame)
        manager.handle_event(_added_event(pygame, 0, use_controller=True))

        hat_up = FakeEvent(type=pygame.JOYHATMOTION, instance_id=1, value=(0, 1))
        assert manager.handle_event(hat_up) == Action.UP

    def test_joy_axis_navigates_past_deadzone_on_recognized_game_controller(self):
        pygame = make_fake_pygame(joysticks={0: FakeJoystick(guid="gc2")}, controller_indices=frozenset({0}))
        manager = ControllerManager(pygame, deadzone=0.5)
        manager.handle_event(_added_event(pygame, 0, use_controller=True))

        small = FakeEvent(type=pygame.JOYAXISMOTION, instance_id=1, axis=0, value=5000)
        assert manager.handle_event(small) is None

        big = FakeEvent(type=pygame.JOYAXISMOTION, instance_id=1, axis=0, value=30000)
        assert manager.handle_event(big) == Action.RIGHT

    def test_joy_axis_repeat_and_release_on_recognized_game_controller(self):
        pygame = make_fake_pygame(joysticks={0: FakeJoystick(guid="gc2b")}, controller_indices=frozenset({0}))
        manager = ControllerManager(pygame, deadzone=0.5)
        manager.handle_event(_added_event(pygame, 0, use_controller=True))
        manager.handle_event(FakeEvent(type=pygame.JOYAXISMOTION, instance_id=1, axis=1, value=-30000))

        assert manager.update(0.1) == []
        assert manager.update(0.4) == [Action.UP]

        manager.handle_event(FakeEvent(type=pygame.JOYAXISMOTION, instance_id=1, axis=1, value=0))
        assert manager.update(1.0) == []

    def test_joy_button_down_uses_generic_raw_fallback_on_recognized_game_controller(self):
        pygame = make_fake_pygame(joysticks={0: FakeJoystick(guid="gc3")}, controller_indices=frozenset({0}))
        manager = ControllerManager(pygame)
        manager.handle_event(_added_event(pygame, 0, use_controller=True))

        confirm = FakeEvent(type=pygame.JOYBUTTONDOWN, instance_id=1, button=0)
        assert manager.handle_event(confirm) == Action.CONFIRM

    def test_joy_button_up_clears_held_direction_on_recognized_game_controller(self):
        profile = ControllerProfile(name="Test Pad", button_actions={7: Action.UP})
        pygame = make_fake_pygame(joysticks={0: FakeJoystick(guid="gc4")}, controller_indices=frozenset({0}))
        manager = ControllerManager(pygame, profiles={"gc4": profile})
        manager.handle_event(_added_event(pygame, 0, use_controller=True))

        down = FakeEvent(type=pygame.JOYBUTTONDOWN, instance_id=1, button=7)
        assert manager.handle_event(down) == Action.UP

        up = FakeEvent(type=pygame.JOYBUTTONUP, instance_id=1, button=7)
        manager.handle_event(up)
        assert manager.update(1.0) == []


class TestControllerProfiles:
    """GUID-keyed raw button/axis overrides, injected via the ``profiles``
    constructor param — isolated from any real built-in profile table."""

    def test_profile_selected_by_guid_maps_unusual_raw_button(self):
        profile = ControllerProfile(name="Known Pad", button_actions={42: Action.MENU})
        pygame = make_fake_pygame(joysticks={0: FakeJoystick(guid="known-guid")}, controller_indices=frozenset())
        manager = ControllerManager(pygame, profiles={"known-guid": profile})
        manager.handle_event(_added_event(pygame, 0))

        event = FakeEvent(type=pygame.JOYBUTTONDOWN, instance_id=1, button=42)
        assert manager.handle_event(event) == Action.MENU

    def test_profile_selected_by_guid_maps_unusual_raw_axis(self):
        profile = ControllerProfile(name="Known Pad", axis_names={2: "x"})
        pygame = make_fake_pygame(joysticks={0: FakeJoystick(guid="known-guid-2")}, controller_indices=frozenset())
        manager = ControllerManager(pygame, deadzone=0.5, profiles={"known-guid-2": profile})
        manager.handle_event(_added_event(pygame, 0))

        event = FakeEvent(type=pygame.JOYAXISMOTION, instance_id=1, axis=2, value=30000)
        assert manager.handle_event(event) == Action.RIGHT

    def test_unknown_guid_without_profile_leaves_unusual_raw_axis_unmapped(self):
        pygame = make_fake_pygame(joysticks={0: FakeJoystick(guid="no-profile-guid")}, controller_indices=frozenset())
        manager = ControllerManager(pygame, deadzone=0.5)
        manager.handle_event(_added_event(pygame, 0))

        event = FakeEvent(type=pygame.JOYAXISMOTION, instance_id=1, axis=2, value=30000)
        assert manager.handle_event(event) is None

    def test_custom_mapping_overrides_profile(self):
        profile = ControllerProfile(name="Known Pad", button_actions={7: Action.UP})

        def loader(key):
            return {"button": {"7": "down"}} if key == "guid:known-guid-3" else None

        pygame = make_fake_pygame(joysticks={0: FakeJoystick(guid="known-guid-3")}, controller_indices=frozenset())
        manager = ControllerManager(pygame, load_mapping=loader, profiles={"known-guid-3": profile})
        manager.handle_event(_added_event(pygame, 0))

        event = FakeEvent(type=pygame.JOYBUTTONDOWN, instance_id=1, button=7)
        assert manager.handle_event(event) == Action.DOWN


class TestConfirmButtonRelease:
    """Needed by ports_gfx.hold_confirm — releasing the Confirm button must
    be observable, not just its press edge."""

    def test_raw_confirm_button_release_returns_confirm_released(self):
        pygame = make_fake_pygame(joysticks={0: FakeJoystick(guid="g1")}, controller_indices=frozenset())
        manager = ControllerManager(pygame)
        manager.handle_event(_added_event(pygame, 0))

        down = FakeEvent(type=pygame.JOYBUTTONDOWN, instance_id=1, button=0)
        assert manager.handle_event(down) == Action.CONFIRM
        up = FakeEvent(type=pygame.JOYBUTTONUP, instance_id=1, button=0)
        assert manager.handle_event(up) == Action.CONFIRM_RELEASED

    def test_logical_confirm_button_release_returns_confirm_released(self):
        pygame = make_fake_pygame(joysticks={0: FakeJoystick(guid="g1")}, controller_indices=frozenset({0}))
        manager = ControllerManager(pygame)
        manager.handle_event(_added_event(pygame, 0, use_controller=True))

        down = FakeEvent(type=pygame.CONTROLLERBUTTONDOWN, instance_id=1, button=pygame.CONTROLLER_BUTTON_A)
        assert manager.handle_event(down) == Action.CONFIRM
        up = FakeEvent(type=pygame.CONTROLLERBUTTONUP, instance_id=1, button=pygame.CONTROLLER_BUTTON_A)
        assert manager.handle_event(up) == Action.CONFIRM_RELEASED

    def test_non_confirm_button_release_returns_none(self):
        pygame = make_fake_pygame(joysticks={0: FakeJoystick(guid="g1")}, controller_indices=frozenset())
        manager = ControllerManager(pygame)
        manager.handle_event(_added_event(pygame, 0))

        up = FakeEvent(type=pygame.JOYBUTTONUP, instance_id=1, button=1)  # raw fallback BACK
        assert manager.handle_event(up) is None


class TestSteamDeckProfile:
    def test_builtin_profile_is_selected_by_guid_for_raw_buttons(self):
        pygame = make_fake_pygame(
            joysticks={0: FakeJoystick(name="Steam Deck", guid="03000000de2800000512000010010000")},
            controller_indices=frozenset(),
        )
        manager = ControllerManager(pygame)
        manager.handle_event(_added_event(pygame, 0))

        assert manager.handle_event(FakeEvent(type=pygame.JOYBUTTONDOWN, instance_id=1, button=3)) == Action.CONFIRM
        assert manager.handle_event(FakeEvent(type=pygame.JOYBUTTONDOWN, instance_id=1, button=4)) == Action.BACK
        assert manager.handle_event(FakeEvent(type=pygame.JOYBUTTONDOWN, instance_id=1, button=16)) == Action.UP
        assert manager.handle_event(FakeEvent(type=pygame.JOYBUTTONDOWN, instance_id=1, button=17)) == Action.DOWN
        assert manager.handle_event(FakeEvent(type=pygame.JOYBUTTONDOWN, instance_id=1, button=18)) == Action.LEFT
        assert manager.handle_event(FakeEvent(type=pygame.JOYBUTTONDOWN, instance_id=1, button=19)) == Action.RIGHT
        assert manager.handle_event(FakeEvent(type=pygame.JOYBUTTONDOWN, instance_id=1, button=12)) == Action.MENU
        assert manager.handle_event(FakeEvent(type=pygame.JOYBUTTONDOWN, instance_id=1, button=11)) is None

    def test_builtin_profile_ignores_hat_motion_for_navigation(self):
        pygame = make_fake_pygame(
            joysticks={0: FakeJoystick(name="Steam Deck", guid="03000000de2800000512000010010000")},
            controller_indices=frozenset(),
        )
        manager = ControllerManager(pygame, deadzone=0.5)
        manager.handle_event(_added_event(pygame, 0))

        assert manager.handle_event(FakeEvent(type=pygame.JOYAXISMOTION, instance_id=1, axis=0, value=30000)) == Action.RIGHT
        assert manager.handle_event(FakeEvent(type=pygame.JOYHATMOTION, instance_id=1, value=(0, 1))) is None
        assert manager.snapshots()[0].held_direction == (1, 0)

    def test_custom_mapping_overrides_builtin_profile(self):
        def loader(key):
            return {"button": {"3": "back"}} if key == "guid:03000000de2800000512000010010000" else None

        pygame = make_fake_pygame(
            joysticks={0: FakeJoystick(name="Steam Deck", guid="03000000de2800000512000010010000")},
            controller_indices=frozenset(),
        )
        manager = ControllerManager(pygame, load_mapping=loader)
        manager.handle_event(_added_event(pygame, 0))

        assert manager.handle_event(FakeEvent(type=pygame.JOYBUTTONDOWN, instance_id=1, button=3)) == Action.BACK


class TestDuplicateJoyControllerEvents:
    """SDL emits both a raw JOY* event and a mirrored CONTROLLER* event for
    the same physical press on a recognized game controller — once the raw
    stream has been observed, the mirrored one must be ignored so one
    physical input never produces two actions."""

    def test_mirrored_controller_button_after_joy_button_is_suppressed(self):
        pygame = make_fake_pygame(joysticks={0: FakeJoystick(guid="dup1")}, controller_indices=frozenset({0}))
        manager = ControllerManager(pygame)
        manager.handle_event(_added_event(pygame, 0, use_controller=True))

        joy_confirm = FakeEvent(type=pygame.JOYBUTTONDOWN, instance_id=1, button=0)
        assert manager.handle_event(joy_confirm) == Action.CONFIRM

        mirrored = FakeEvent(type=pygame.CONTROLLERBUTTONDOWN, instance_id=1, button=pygame.CONTROLLER_BUTTON_A)
        assert manager.handle_event(mirrored) is None

    def test_mirrored_controller_axis_after_joy_axis_is_suppressed(self):
        pygame = make_fake_pygame(joysticks={0: FakeJoystick(guid="dup2")}, controller_indices=frozenset({0}))
        manager = ControllerManager(pygame, deadzone=0.5)
        manager.handle_event(_added_event(pygame, 0, use_controller=True))

        joy_axis = FakeEvent(type=pygame.JOYAXISMOTION, instance_id=1, axis=0, value=30000)
        assert manager.handle_event(joy_axis) == Action.RIGHT

        mirrored = FakeEvent(
            type=pygame.CONTROLLERAXISMOTION, instance_id=1, axis=pygame.CONTROLLER_AXIS_LEFTX, value=30000
        )
        assert manager.handle_event(mirrored) is None

    def test_controller_event_still_works_before_any_joy_event_seen(self):
        pygame = make_fake_pygame(joysticks={0: FakeJoystick(guid="dup3")}, controller_indices=frozenset({0}))
        manager = ControllerManager(pygame)
        manager.handle_event(_added_event(pygame, 0, use_controller=True))

        confirm = FakeEvent(type=pygame.CONTROLLERBUTTONDOWN, instance_id=1, button=pygame.CONTROLLER_BUTTON_A)
        assert manager.handle_event(confirm) == Action.CONFIRM
