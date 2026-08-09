"""Unit tests for the temporary raw input diagnostics logger."""

from __future__ import annotations

from ports_gfx.controller import ControllerIdentity, ControllerSnapshot
from ports_gfx.input_debug import DEBUG_ENV_VAR, InputDebugLogger, is_enabled_from_env
from tests.unit._pygame_fakes import FakeEvent, FakeJoystick, make_fake_pygame


class TestInputDebugEnvGate:
    def test_truthy_values_enable_logging(self):
        assert is_enabled_from_env({DEBUG_ENV_VAR: "1"}) is True

    def test_falsey_values_disable_logging(self):
        assert is_enabled_from_env({DEBUG_ENV_VAR: "0"}) is False
        assert is_enabled_from_env({DEBUG_ENV_VAR: "false"}) is False


class TestInputDebugLogger:
    def test_writes_startup_and_event_details(self, tmp_path):
        pygame = make_fake_pygame(
            joysticks={0: FakeJoystick(name="Steam Deck", guid="03000000de2800000512000010010000")},
            controller_indices=frozenset({0}),
        )
        log_path = tmp_path / "controller-debug.log"
        logger = InputDebugLogger(pygame, enabled=True, log_path=log_path)

        snapshot = ControllerSnapshot(
            instance_id=1,
            identity=ControllerIdentity(name="Steam Deck", guid="03000000de2800000512000010010000"),
            is_game_controller=True,
            using_custom_mapping=False,
            held_direction=None,
            last_action=None,
            axis_x=0.0,
            axis_y=0.0,
        )
        logger.log_startup(joystick_count=1, controller_module_present=True, snapshots=[snapshot])
        logger.log_event(FakeEvent(type=pygame.CONTROLLERBUTTONDOWN, instance_id=1, button=pygame.CONTROLLER_BUTTON_A))
        logger.log_event(FakeEvent(type=pygame.JOYAXISMOTION, instance_id=1, axis=2, value=-32768))
        logger.log_event(FakeEvent(type=pygame.JOYHATMOTION, instance_id=1, hat=0, value=(0, 1)))
        logger.close()

        contents = log_path.read_text(encoding="utf-8")
        assert "startup joystick_count=1" in contents
        assert "name='Steam Deck'" in contents
        assert "guid='03000000de2800000512000010010000'" in contents
        assert "event=CONTROLLERBUTTONDOWN" in contents
        assert "logical_button=CONTROLLER_BUTTON_A" in contents
        assert "event=JOYAXISMOTION" in contents
        assert "axis=2" in contents
        assert "event=JOYHATMOTION" in contents
        assert "value=(0, 1)" in contents

    def test_disabled_logger_does_not_create_file(self, tmp_path):
        pygame = make_fake_pygame()
        log_path = tmp_path / "controller-debug.log"
        logger = InputDebugLogger(pygame, enabled=False, log_path=log_path)
        logger.log_event(FakeEvent(type=pygame.JOYBUTTONDOWN, instance_id=1, button=0))
        logger.close()
        assert not log_path.exists()
