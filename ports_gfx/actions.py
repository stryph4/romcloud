"""Semantic input actions — the vocabulary every screen/widget consumes.

Nothing above this layer (menus, the on-screen keyboard, the controller
diagnostics screen) should ever look at a raw ``pygame`` event type, a raw
key constant, or a raw joystick/controller button index. Keyboard,
controller, and touch input are all translated into these same actions by
the ``input_*``/``controller`` modules; screens only ever react to
:class:`Action` values.
"""

from __future__ import annotations

from enum import Enum


class Action(Enum):
    """A high-level, device-agnostic interaction event."""

    UP = "up"
    DOWN = "down"
    LEFT = "left"
    RIGHT = "right"
    CONFIRM = "confirm"
    CONFIRM_RELEASED = "confirm_released"
    """Emitted when the physical Confirm input (keyboard Enter, controller
    A) is released — device-agnostic, for widgets that need to distinguish
    a held press from a tap (see ``ports_gfx.hold_confirm``). Every other
    screen simply never reacts to it."""
    BACK = "back"
    MENU = "menu"
    PREVIOUS_PAGE = "previous_page"
    NEXT_PAGE = "next_page"

    # Text-entry actions — emitted while an on-screen/physical keyboard is
    # directed at a text field (see ports_gfx.osk). ``TEXT_INPUT`` carries
    # the actual text via a side channel (the event/action pair), never a
    # raw keycode.
    TEXT_INPUT = "text_input"
    TEXT_BACKSPACE = "text_backspace"
    TEXT_TOGGLE_SHIFT = "text_toggle_shift"
    TEXT_TOGGLE_SYMBOLS = "text_toggle_symbols"
    TEXT_TOGGLE_MASK = "text_toggle_mask"


# Actions a directional held-input repeater (D-pad / analog stick) may
# repeat while held. Confirm/back/menu are always single-shot, edge-only.
DIRECTIONAL_ACTIONS = (Action.UP, Action.DOWN, Action.LEFT, Action.RIGHT)

# (dx, dy) unit vectors for each directional action — the single place
# translating a direction action to/from a movement vector, so
# find_next_focus_index (layout.py) and the repeat/deadzone logic never
# duplicate this mapping.
ACTION_DIRECTIONS: dict[Action, tuple[int, int]] = {
    Action.UP: (0, -1),
    Action.DOWN: (0, 1),
    Action.LEFT: (-1, 0),
    Action.RIGHT: (1, 0),
}
DIRECTION_ACTIONS: dict[tuple[int, int], Action] = {v: k for k, v in ACTION_DIRECTIONS.items()}
