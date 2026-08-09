"""Physical keyboard → semantic action translation.

``pygame`` is never imported here — the actual module is passed in by the
caller (see ``app.py``), exactly like the rest of ``ports_gfx``'s lazy-
pygame convention. That also makes this trivially unit-testable with a
lightweight fake object exposing only the handful of constants used.
"""

from __future__ import annotations

from typing import Optional

from ports_gfx.actions import Action

_DIRECTION_KEY_NAMES = {
    "K_UP": Action.UP,
    "K_w": Action.UP,
    "K_DOWN": Action.DOWN,
    "K_s": Action.DOWN,
    "K_LEFT": Action.LEFT,
    "K_a": Action.LEFT,
    "K_RIGHT": Action.RIGHT,
    "K_d": Action.RIGHT,
}
_CONFIRM_KEY_NAMES = ("K_RETURN", "K_SPACE", "K_KP_ENTER")
_BACK_KEY_NAMES = ("K_ESCAPE", "K_BACKSPACE")
_MENU_KEY_NAMES = ("K_TAB",)
_BACKSPACE_KEY_NAMES = ("K_BACKSPACE",)


def _keys_by_name(pygame, names) -> set:  # noqa: ANN001
    values = set()
    for name in names:
        value = getattr(pygame, name, None)
        if value is not None:
            values.add(value)
    return values


def action_for_key(pygame, key: int, *, text_mode: bool = False) -> Optional[Action]:  # noqa: ANN001
    """Translate a ``KEYDOWN`` key constant into a semantic :class:`Action`.

    *text_mode* is set while an on-screen/text-entry field has keyboard
    focus: ``K_BACKSPACE`` then means "delete a character" rather than
    "back/cancel the screen" (the same physical key serves both roles
    depending on context, same as most software keyboards).
    """
    if text_mode and key in _keys_by_name(pygame, _BACKSPACE_KEY_NAMES):
        return Action.TEXT_BACKSPACE

    for name, action in _DIRECTION_KEY_NAMES.items():
        value = getattr(pygame, name, None)
        if value is not None and key == value:
            return action

    if key in _keys_by_name(pygame, _CONFIRM_KEY_NAMES):
        return Action.CONFIRM
    if key in _keys_by_name(pygame, _BACK_KEY_NAMES):
        return Action.BACK
    if key in _keys_by_name(pygame, _MENU_KEY_NAMES):
        return Action.MENU
    return None


def text_for_input_event(event) -> Optional[str]:  # noqa: ANN001
    """Extract typed unicode text from a ``pygame.TEXTINPUT`` event.

    Physical-keyboard text entry (letters, numbers, symbols, including
    non-US layouts and dead-key composition) always arrives via
    ``TEXTINPUT``, never via ``KEYDOWN.unicode`` — using ``TEXTINPUT`` is
    what lets this keep working unmodified while an on-screen keyboard is
    also active for the same field.
    """
    text = getattr(event, "text", None)
    return text if text else None
