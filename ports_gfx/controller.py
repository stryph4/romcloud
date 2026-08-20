"""Controller (gamepad) input → semantic actions.

Design
------
Raw joystick input (SDL's original ``JOYBUTTONDOWN``/``JOYBUTTONUP``/
``JOYAXISMOTION``/``JOYHATMOTION`` events, exposed by pygame as
``pygame.joystick``) is the **primary** Batocera compatibility path: real
Batocera 42 hardware testing found that ``pygame.controller``
(``SDL_GameController``) detects a connected pad but does not reliably
deliver its higher-level ``CONTROLLER*`` events, leaving the UI
unresponsive even though the device is visible. Raw joystick events, by
contrast, are always emitted by SDL for any opened joystick — so every
device is opened via ``pygame.joystick.Joystick`` first and its raw events
are always handled, regardless of whether SDL also recognizes it as a
"game controller".

``pygame.controller``/``CONTROLLER*`` events remain a **secondary** input
source — useful on builds/devices where they *do* work, and for pads whose
raw button/axis numbering isn't otherwise known. They are still translated
via the logical ``CONTROLLER_BUTTON_*``/``CONTROLLER_AXIS_*`` constant
names (never a hardcoded number). To avoid dispatching the same physical
input twice when SDL emits both event families for one press (game
controllers are always joystick devices too), a device stops honoring its
``CONTROLLER*`` stream the first time a raw ``JOY*`` event is observed for
it — raw events always win and are never suppressed.

Raw button/axis numbering for a *specific* pad model can be supplied via a
:class:`ControllerProfile`, looked up by SDL GUID and isolated from the
generic dispatch logic (see :data:`CONTROLLER_PROFILES`) — but only ever
added once real hardware capture confirms the numbers (see
``input_debug.py``), never guessed. Everything else (an unrecognized
controller with no profile) still works through the generic raw fallback
plus per-user remapping (see ``controller_config.py``), which always takes
priority over both a profile and the generic default.

Controller identity for persistence/mapping/profile-lookup purposes is the
SDL GUID when available (stable across reconnects and even across machines
for the same model), falling back to the device name string when a GUID
can't be read — never the transient per-session joystick/instance index,
which is reused and reassigned across hot-plug events and therefore
useless as a key.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Optional

from ports_gfx.actions import ACTION_DIRECTIONS, DIRECTION_ACTIONS, Action
from ports_gfx.input_repeat import HeldDirectionRepeater

DEFAULT_DEADZONE = 0.5
"""Analog stick magnitude (0..1) below which a direction is not navigated —
generous on purpose: menu navigation only needs a firm push, not precision."""

# SDL logical button constant *names* (read off pygame at call time — never
# a hardcoded int) mapped to the action they navigate/trigger.
_LOGICAL_BUTTON_ACTION_NAMES: dict[str, Action] = {
    "CONTROLLER_BUTTON_DPAD_UP": Action.UP,
    "CONTROLLER_BUTTON_DPAD_DOWN": Action.DOWN,
    "CONTROLLER_BUTTON_DPAD_LEFT": Action.LEFT,
    "CONTROLLER_BUTTON_DPAD_RIGHT": Action.RIGHT,
    "CONTROLLER_BUTTON_A": Action.CONFIRM,
    "CONTROLLER_BUTTON_B": Action.BACK,
    "CONTROLLER_BUTTON_LEFTSHOULDER": Action.PREVIOUS_PAGE,
    "CONTROLLER_BUTTON_RIGHTSHOULDER": Action.NEXT_PAGE,
    "CONTROLLER_BUTTON_START": Action.MENU,
    "CONTROLLER_BUTTON_LEFTSTICK": Action.TEXT_TOGGLE_SHIFT,
}

# Left analog stick logical axis constant names -> which axis ("x"/"y").
_LOGICAL_AXIS_NAMES: dict[str, str] = {
    "CONTROLLER_AXIS_LEFTX": "x",
    "CONTROLLER_AXIS_LEFTY": "y",
}

# Raw joystick fallback used generically for any pad — including one SDL
# recognizes as a game controller, since raw JOY* events are now always the
# primary path (see module docstring): a common-enough default (axis 0/1 =
# left stick, button 0/1 = confirm/back) that the Controller Test screen
# lets a user override per-device via a persisted custom mapping. A
# per-model :class:`ControllerProfile` can supply a better default before
# falling back to these.
RAW_FALLBACK_AXIS_INDEX = {0: "x", 1: "y"}
RAW_FALLBACK_BUTTON_ACTIONS: dict[int, Action] = {0: Action.CONFIRM, 1: Action.BACK}


@dataclass(frozen=True)
class ControllerProfile:
    """Optional raw button/axis numbering for a *specific* pad model,
    looked up by SDL GUID (see :data:`CONTROLLER_PROFILES`).

    Deliberately isolated from the rest of ``ControllerManager``: adding,
    editing, or removing a profile never touches the generic dispatch
    logic. A profile only ever supplies a *default* — it sits between the
    generic raw fallback and a user's persisted custom mapping, which
    always wins regardless of whether a profile exists for the device.
    """

    name: str
    button_actions: dict[int, Action] = field(default_factory=dict)
    axis_names: dict[int, str] = field(default_factory=dict)
    ignore_hat_motion: bool = False


# Built-in profiles, keyed by SDL GUID string. Each entry is only added
# after the raw numbering has been verified from a real device capture
# (see ``input_debug.py``), never guessed from a similar-looking GUID or an
# unrelated project's mapping string.
CONTROLLER_PROFILES: dict[str, ControllerProfile] = {
    "03000000de2800000512000010010000": ControllerProfile(
        name="Steam Deck",
        button_actions={
            3: Action.CONFIRM,
            4: Action.BACK,
            16: Action.UP,
            17: Action.DOWN,
            18: Action.LEFT,
            19: Action.RIGHT,
            12: Action.MENU,
        },
        ignore_hat_motion=True,
    )
}


def _attr_map(pygame, names: dict[str, "object"]) -> dict[int, "object"]:  # noqa: ANN001
    """Build {constant_value: mapped_value} from {constant_name: mapped_value},
    silently skipping any constant this pygame build doesn't define."""
    result: dict[int, object] = {}
    for name, mapped in names.items():
        value = getattr(pygame, name, None)
        if value is not None:
            result[value] = mapped
    return result


def build_logical_button_action_map(pygame) -> dict[int, Action]:  # noqa: ANN001
    return _attr_map(pygame, _LOGICAL_BUTTON_ACTION_NAMES)


def build_logical_axis_map(pygame) -> dict[int, str]:  # noqa: ANN001
    return _attr_map(pygame, _LOGICAL_AXIS_NAMES)


def normalize_axis_value(raw_value: float) -> float:
    """SDL axis events report a signed 16-bit int (-32768..32767); scale to
    a clamped -1.0..1.0 float regardless of which end of that range."""
    return max(-1.0, min(1.0, float(raw_value) / 32767.0))


def direction_from_axes(x: float, y: float, *, deadzone: float = DEFAULT_DEADZONE) -> Optional[tuple[int, int]]:
    """Resolve an analog stick position into a single discrete navigation
    direction, or ``None`` if inside the deadzone.

    Only the dominant axis navigates at a time (no diagonal focus jumps in
    a 2D grid) — whichever of x/y has the larger magnitude wins.
    """
    ax, ay = abs(x), abs(y)
    if ax < deadzone and ay < deadzone:
        return None
    if ax >= ay:
        return (1, 0) if x > 0 else (-1, 0)
    return (0, 1) if y > 0 else (0, -1)


def direction_from_hat(hat_x: int, hat_y: int) -> Optional[tuple[int, int]]:
    """SDL/pygame hat (D-pad) values use ``y = 1`` for *up*, the opposite
    of this project's screen-space ``(dx, dy)`` convention (``dy = -1`` is
    up) — flip it here, once, rather than anywhere navigation logic lives."""
    if hat_x == 0 and hat_y == 0:
        return None
    dx = 1 if hat_x > 0 else (-1 if hat_x < 0 else 0)
    dy = -1 if hat_y > 0 else (1 if hat_y < 0 else 0)
    if dx and dy:
        # Diagonal hat push — prefer the axis with a clearer signal; hats
        # are digital (values only ever -1/0/1) so just pick x, matching
        # direction_from_axes's "no diagonal focus jumps" behavior.
        return (dx, 0)
    return (dx, dy)


@dataclass(frozen=True)
class ControllerIdentity:
    """Persistent identity for a connected controller.

    Prefers the SDL GUID (stable across reconnects/machines for the same
    model); falls back to the device name when no GUID is available. Never
    the transient joystick/instance index, which pygame reassigns across
    hot-plug events.
    """

    name: str
    guid: Optional[str] = None

    @property
    def key(self) -> str:
        return f"guid:{self.guid}" if self.guid else f"name:{self.name}"


def get_identity(joystick) -> ControllerIdentity:  # noqa: ANN001
    """Read identity off a ``pygame.joystick.Joystick`` (or
    ``pygame.controller.Controller``, which exposes the same methods)."""
    name = "Unknown Controller"
    get_name = getattr(joystick, "get_name", None)
    if callable(get_name):
        try:
            name = get_name() or name
        except Exception:  # noqa: BLE001
            pass

    guid = None
    get_guid = getattr(joystick, "get_guid", None)
    if callable(get_guid):
        try:
            guid = get_guid() or None
        except Exception:  # noqa: BLE001
            guid = None

    return ControllerIdentity(name=name, guid=guid)


@dataclass(frozen=True)
class ControllerSnapshot:
    """A point-in-time diagnostic view of one connected controller, for the
    Controller Test/diagnostics screen."""

    instance_id: int
    identity: ControllerIdentity
    is_game_controller: bool
    using_custom_mapping: bool
    held_direction: Optional[tuple[int, int]]
    last_action: Optional[Action]
    axis_x: float
    axis_y: float


@dataclass
class _DeviceState:
    identity: ControllerIdentity
    is_game_controller: bool
    joystick: object
    controller: Optional[object] = None
    profile: Optional[ControllerProfile] = None
    custom_mapping: dict = field(default_factory=dict)
    repeater: HeldDirectionRepeater = field(default_factory=HeldDirectionRepeater)
    axis_x: float = 0.0
    axis_y: float = 0.0
    last_action: Optional[Action] = None
    using_custom_mapping: bool = False
    seen_joy_event: bool = False
    held_buttons: set[tuple[bool, int]] = field(default_factory=set)
    """Set the first time a raw ``JOY*`` event arrives for this device;
    once ``True``, its ``CONTROLLER*`` event stream is treated as a
    duplicate mirror of the same physical input and ignored (see
    :meth:`ControllerManager.handle_event`)."""


@dataclass(frozen=True)
class _RemapSession:
    instance_id: int
    action: Action


ConfigLoader = Callable[[str], Optional[dict]]
ConfigSaver = Callable[[str, dict], None]


class ControllerManager:
    """Tracks connected controllers and translates their events into
    :class:`Action`\\ s. Never raises: a missing/partial ``pygame``
    controller API, an unrecognized pad, or a mid-session
    disconnect/reconnect all degrade gracefully rather than crashing the
    graphical UI.
    """

    def __init__(
        self,
        pygame,  # noqa: ANN001
        *,
        deadzone: float = DEFAULT_DEADZONE,
        load_mapping: Optional[ConfigLoader] = None,
        save_mapping: Optional[ConfigSaver] = None,
        profiles: Optional[dict[str, ControllerProfile]] = None,
    ) -> None:
        self._pygame = pygame
        self._deadzone = deadzone
        self._load_mapping = load_mapping or (lambda key: None)
        self._save_mapping = save_mapping or (lambda key, mapping: None)
        self._profiles = CONTROLLER_PROFILES if profiles is None else profiles
        self._button_action_map = build_logical_button_action_map(pygame)
        self._axis_map = build_logical_axis_map(pygame)
        self._devices: dict[int, _DeviceState] = {}
        self._remap: Optional[_RemapSession] = None

    # ── device lifecycle ──────────────────────────────────────────────────

    def _open_device(self, device_index: int) -> None:
        pygame = self._pygame

        try:
            joystick = pygame.joystick.Joystick(device_index)
            if hasattr(joystick, "init"):
                joystick.init()
        except Exception:  # noqa: BLE001 — a device that can't even open is simply ignored
            return

        try:
            instance_id = joystick.get_instance_id()
        except Exception:  # noqa: BLE001
            instance_id = device_index

        if instance_id in self._devices:
            # SDL can fire both JOYDEVICEADDED and CONTROLLERDEVICEADDED
            # for the same physical device — never clobber already-tracked
            # state (held direction, axis position, custom mapping) on a
            # harmless duplicate "added" notification.
            return

        # pygame.controller is opened only as a *secondary*, best-effort
        # handle — the raw joystick above is always the primary, required
        # one (see module docstring).
        is_game_controller = False
        controller = None
        controller_module = getattr(pygame, "controller", None)
        if controller_module is not None:
            try:
                if controller_module.is_controller(device_index):
                    is_game_controller = True
                    try:
                        controller = controller_module.Controller(device_index)
                    except Exception:  # noqa: BLE001 — raw joystick handle above still works
                        controller = None
            except Exception:  # noqa: BLE001
                is_game_controller = False

        identity = get_identity(joystick)
        custom_mapping = self._load_mapping(identity.key) or {}
        profile = self._profiles.get(identity.guid) if identity.guid else None
        self._devices[instance_id] = _DeviceState(
            identity=identity,
            is_game_controller=is_game_controller,
            joystick=joystick,
            controller=controller,
            profile=profile,
            custom_mapping=custom_mapping,
            using_custom_mapping=bool(custom_mapping),
        )

    def _close_device(self, instance_id: int) -> None:
        self._devices.pop(instance_id, None)
        if self._remap is not None and self._remap.instance_id == instance_id:
            self._remap = None

    def open_existing_devices(self, count: int) -> None:
        """Enumerate devices already connected before the event loop starts
        (hot-plug events only fire for connects/disconnects *after* init).
        Safe to call with 0 and never raises."""
        for device_index in range(max(0, count)):
            try:
                self._open_device(device_index)
            except Exception:  # noqa: BLE001 — one bad device must not block the rest
                continue

    # ── remapping ──────────────────────────────────────────────────────────

    def begin_remap(self, instance_id: int, action: Action) -> bool:
        """Arm capture of the *next* raw input from *instance_id* as the new
        binding for *action*. Returns ``False`` if that device isn't
        connected."""
        if instance_id not in self._devices:
            return False
        self._remap = _RemapSession(instance_id=instance_id, action=action)
        return True

    def cancel_remap(self) -> None:
        self._remap = None

    @property
    def remap_pending_action(self) -> Optional[Action]:
        return self._remap.action if self._remap else None

    def _capture_remap(self, instance_id: int, raw_kind: str, raw_key: str) -> bool:
        """If a remap capture is pending for *instance_id*, bind
        *raw_key* (of *raw_kind*, "button" or "hat") to the pending action,
        persist it, and clear the pending session. Returns whether a
        capture was consumed (caller must suppress normal dispatch)."""
        if self._remap is None or self._remap.instance_id != instance_id:
            return False
        device = self._devices.get(instance_id)
        if device is None:
            self._remap = None
            return True

        mapping = dict(device.custom_mapping)
        bucket = dict(mapping.get(raw_kind, {}))
        bucket[raw_key] = self._remap.action.value
        mapping[raw_kind] = bucket
        device.custom_mapping = mapping
        device.using_custom_mapping = True
        self._save_mapping(device.identity.key, mapping)
        self._remap = None
        return True

    # ── event handling ────────────────────────────────────────────────────

    def handle_event(self, event) -> Optional[Action]:  # noqa: ANN001, C901
        pygame = self._pygame
        event_type = event.type

        if event_type in (getattr(pygame, "CONTROLLERDEVICEADDED", object()), getattr(pygame, "JOYDEVICEADDED", object())):
            self._open_device(event.device_index)
            return None

        if event_type in (getattr(pygame, "CONTROLLERDEVICEREMOVED", object()), getattr(pygame, "JOYDEVICEREMOVED", object())):
            self._close_device(event.instance_id)
            return None

        instance_id = getattr(event, "instance_id", None)
        device = self._devices.get(instance_id) if instance_id is not None else None
        if device is None:
            return None

        joy_button_down = getattr(pygame, "JOYBUTTONDOWN", object())
        joy_button_up = getattr(pygame, "JOYBUTTONUP", object())
        joy_axis = getattr(pygame, "JOYAXISMOTION", object())
        joy_hat = getattr(pygame, "JOYHATMOTION", object())
        controller_button_down = getattr(pygame, "CONTROLLERBUTTONDOWN", object())
        controller_button_up = getattr(pygame, "CONTROLLERBUTTONUP", object())
        controller_axis = getattr(pygame, "CONTROLLERAXISMOTION", object())

        if event_type in (joy_button_down, joy_button_up, joy_axis, joy_hat):
            # Raw joystick events are the primary path (see module
            # docstring) — always processed, and they mark this device as
            # one whose CONTROLLER* stream (if any) is now a duplicate.
            device.seen_joy_event = True
        elif event_type in (controller_button_down, controller_button_up, controller_axis) and device.seen_joy_event:
            # This device's raw JOY* events already work; ignore the
            # higher-level mirror of the same physical input rather than
            # dispatching it twice.
            if event_type == controller_button_up:
                device.held_buttons.discard((False, event.button))
            return None

        if event_type == joy_button_down:
            return self._handle_button_down(instance_id, device, event.button, raw=True)
        if event_type == controller_button_down:
            return self._handle_button_down(instance_id, device, event.button, raw=False)

        if event_type == joy_button_up:
            return self._handle_button_up(device, event.button, raw=True)
        if event_type == controller_button_up:
            return self._handle_button_up(device, event.button, raw=False)

        if event_type == joy_axis:
            return self._handle_axis(device, self._resolve_raw_axis_name(device, event.axis), event.value)
        if event_type == controller_axis:
            return self._handle_axis(device, self._axis_map.get(event.axis), event.value)

        if event_type == joy_hat:
            return self._handle_hat(instance_id, device, event.value)

        return None

    def _resolve_raw_axis_name(self, device: _DeviceState, axis_index: int) -> Optional[str]:
        if device.profile is not None:
            name = device.profile.axis_names.get(axis_index)
            if name is not None:
                return name
        return RAW_FALLBACK_AXIS_INDEX.get(axis_index)

    def _resolve_button_action(self, device: _DeviceState, button: int, *, raw: bool) -> Optional[Action]:
        custom = device.custom_mapping.get("button", {})
        custom_value = custom.get(str(button))
        if custom_value is not None:
            try:
                return Action(custom_value)
            except ValueError:
                pass

        if raw:
            if device.profile is not None:
                profile_action = device.profile.button_actions.get(button)
                if profile_action is not None:
                    return profile_action
            return RAW_FALLBACK_BUTTON_ACTIONS.get(button)

        return self._button_action_map.get(button)

    def _handle_button_down(
        self, instance_id: int, device: _DeviceState, button: int, *, raw: bool
    ) -> Optional[Action]:
        device.held_buttons.add((raw, button))
        if self._capture_remap(instance_id, "button", str(button)):
            return None
        action = self._resolve_button_action(device, button, raw=raw)
        if action is None:
            return None
        device.last_action = action
        if action in ACTION_DIRECTIONS:
            device.repeater.press(ACTION_DIRECTIONS[action])
        return action

    def _handle_button_up(self, device: _DeviceState, button: int, *, raw: bool) -> Optional[Action]:
        device.held_buttons.discard((raw, button))
        action = self._resolve_button_action(device, button, raw=raw)
        if action in ACTION_DIRECTIONS and device.repeater.held_direction == ACTION_DIRECTIONS[action]:
            device.repeater.release()
        if action == Action.CONFIRM:
            return Action.CONFIRM_RELEASED
        return None

    def _handle_axis(self, device: _DeviceState, axis_name: Optional[str], raw_value: float) -> Optional[Action]:
        if axis_name is None:
            return None
        value = normalize_axis_value(raw_value)
        if axis_name == "x":
            device.axis_x = value
        else:
            device.axis_y = value

        direction = direction_from_axes(device.axis_x, device.axis_y, deadzone=self._deadzone)
        if direction == device.repeater.held_direction:
            return None
        if direction is None:
            device.repeater.release()
            return None
        device.repeater.press(direction)
        action = DIRECTION_ACTIONS[direction]
        device.last_action = action
        return action

    def _handle_hat(self, instance_id: int, device: _DeviceState, value: tuple[int, int]) -> Optional[Action]:
        hat_x, hat_y = value
        if self._remap is not None and self._remap.instance_id == instance_id and (hat_x or hat_y):
            if self._capture_remap(instance_id, "hat", f"{hat_x},{hat_y}"):
                return None

        custom = device.custom_mapping.get("hat", {})
        custom_value = custom.get(f"{hat_x},{hat_y}")
        direction = None
        if custom_value is not None:
            try:
                direction = ACTION_DIRECTIONS[Action(custom_value)]
            except (ValueError, KeyError):
                direction = None
        if direction is None:
            if device.profile is not None and device.profile.ignore_hat_motion:
                return None
            direction = direction_from_hat(hat_x, hat_y)

        if direction == device.repeater.held_direction:
            return None
        if direction is None:
            device.repeater.release()
            return None
        device.repeater.press(direction)
        action = DIRECTION_ACTIONS[direction]
        device.last_action = action
        return action

    # ── per-frame repeat polling ───────────────────────────────────────────

    def update(self, dt: float) -> list[Action]:
        """Poll every connected device's held-direction repeater. Call once
        per frame with the elapsed time; returns any repeat actions that
        should fire this tick (usually empty)."""
        fired = []
        for device in self._devices.values():
            if device.repeater.update(dt):
                direction = device.repeater.held_direction
                if direction is not None:
                    fired.append(DIRECTION_ACTIONS[direction])
        return fired

    # ── diagnostics ────────────────────────────────────────────────────────

    def snapshots(self) -> list[ControllerSnapshot]:
        return [
            ControllerSnapshot(
                instance_id=instance_id,
                identity=d.identity,
                is_game_controller=d.is_game_controller,
                using_custom_mapping=d.using_custom_mapping,
                held_direction=d.repeater.held_direction,
                last_action=d.last_action,
                axis_x=d.axis_x,
                axis_y=d.axis_y,
            )
            for instance_id, d in self._devices.items()
        ]

    def all_controls_released(self) -> bool:
        """Poll actual pad state for popup handoff release barriers."""
        for device in self._devices.values():
            if device.held_buttons or device.repeater.held_direction is not None:
                return False
            joystick = device.joystick
            try:
                if any(
                    joystick.get_button(index)
                    for index in range(joystick.get_numbuttons())
                ):
                    return False
            except (AttributeError, OSError):
                pass
            try:
                if any(
                    joystick.get_hat(index) != (0, 0)
                    for index in range(joystick.get_numhats())
                ):
                    return False
            except (AttributeError, OSError):
                pass
            try:
                # ROMCloud navigation uses the first two raw axes.  Do not
                # include trigger axes, whose neutral value is often -1.
                for index in range(min(2, joystick.get_numaxes())):
                    if abs(float(joystick.get_axis(index))) >= self._deadzone:
                        return False
            except (AttributeError, OSError, TypeError, ValueError):
                pass
        return True

    @property
    def device_count(self) -> int:
        return len(self._devices)
