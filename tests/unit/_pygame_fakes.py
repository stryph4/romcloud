"""Lightweight fakes for pygame's event/constant/joystick/controller APIs,
shared by the ports_gfx input-system unit tests.

None of these tests need real pygame installed (it isn't, in this dev
venv) — every ``ports_gfx`` input module accepts ``pygame`` as an injected
parameter specifically so a tiny fake object exposing only the handful of
constants/classes actually used is enough to exercise the real logic.
"""

from __future__ import annotations

import itertools
from types import SimpleNamespace
from typing import Optional

_counter = itertools.count(1000)

_CONSTANT_NAMES = [
    "KEYDOWN",
    "TEXTINPUT",
    "MOUSEBUTTONDOWN",
    "MOUSEBUTTONUP",
    "FINGERDOWN",
    "FINGERUP",
    "CONTROLLERDEVICEADDED",
    "CONTROLLERDEVICEREMOVED",
    "CONTROLLERBUTTONDOWN",
    "CONTROLLERBUTTONUP",
    "CONTROLLERAXISMOTION",
    "JOYDEVICEADDED",
    "JOYDEVICEREMOVED",
    "JOYBUTTONDOWN",
    "JOYBUTTONUP",
    "JOYAXISMOTION",
    "JOYHATMOTION",
    "K_UP",
    "K_DOWN",
    "K_LEFT",
    "K_RIGHT",
    "K_w",
    "K_a",
    "K_s",
    "K_d",
    "K_RETURN",
    "K_SPACE",
    "K_KP_ENTER",
    "K_ESCAPE",
    "K_BACKSPACE",
    "K_TAB",
    "CONTROLLER_BUTTON_A",
    "CONTROLLER_BUTTON_B",
    "CONTROLLER_BUTTON_START",
    "CONTROLLER_BUTTON_DPAD_UP",
    "CONTROLLER_BUTTON_DPAD_DOWN",
    "CONTROLLER_BUTTON_DPAD_LEFT",
    "CONTROLLER_BUTTON_DPAD_RIGHT",
    "CONTROLLER_AXIS_LEFTX",
    "CONTROLLER_AXIS_LEFTY",
]


class FakeEvent(SimpleNamespace):
    """A minimal stand-in for a ``pygame.event.Event`` — just whatever
    attributes the test needs, via kwargs."""


class FakeJoystick:
    def __init__(self, name: str = "Fake Pad", guid: Optional[str] = "fake-guid-0001", instance_id: int = 1) -> None:
        self._name = name
        self._guid = guid
        self._instance_id = instance_id
        self.initialized = False

    def init(self) -> None:
        self.initialized = True

    def get_name(self) -> str:
        return self._name

    def get_guid(self) -> Optional[str]:
        return self._guid

    def get_instance_id(self) -> int:
        return self._instance_id


class _FakeJoystickModule:
    def __init__(self, joysticks: Optional[dict[int, FakeJoystick]] = None) -> None:
        self._joysticks = joysticks or {}

    def init(self) -> None:
        pass

    def get_count(self) -> int:
        return len(self._joysticks)

    def Joystick(self, device_index: int) -> FakeJoystick:  # noqa: N802 - mirrors pygame's API name
        return self._joysticks[device_index]


class _FakeControllerModule:
    def __init__(
        self,
        joysticks: Optional[dict[int, FakeJoystick]] = None,
        controller_indices: frozenset = frozenset(),
    ) -> None:
        self._joysticks = joysticks or {}
        self._controller_indices = controller_indices

    def init(self) -> None:
        pass

    def is_controller(self, device_index: int) -> bool:
        return device_index in self._controller_indices

    def Controller(self, device_index: int) -> FakeJoystick:  # noqa: N802 - mirrors pygame's API name
        return self._joysticks[device_index]


def make_fake_pygame(
    *,
    joysticks: Optional[dict[int, FakeJoystick]] = None,
    controller_indices: frozenset = frozenset(),
    has_controller_module: bool = True,
) -> SimpleNamespace:
    """A fake ``pygame`` module exposing just the event-type/key constants
    and joystick/controller API surface the ports_gfx input modules use.

    *joysticks* / *controller_indices* seed the fake ``pygame.joystick`` /
    ``pygame.controller`` submodules so ``ControllerManager`` can "open"
    them exactly as it would real devices.
    """
    ns = SimpleNamespace()
    for name in _CONSTANT_NAMES:
        setattr(ns, name, next(_counter))

    ns.joystick = _FakeJoystickModule(joysticks)
    if has_controller_module:
        ns.controller = _FakeControllerModule(joysticks, controller_indices)
    return ns
