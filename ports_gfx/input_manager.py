"""Top-level input façade: keyboard + controller + touch, all funneled into
the same :class:`~ports_gfx.actions.Action` vocabulary.

Screens never touch ``pygame`` events directly — they call
:meth:`InputManager.handle_event` per event and :meth:`InputManager.update`
once per frame, and only ever react to the returned :class:`Action`\\ s (or,
for touch, the resolved widget index — touch inherently needs the caller's
rendered layout rects to know *which* widget was tapped, and always uses
the true rendered geometry, never a hardcoded region).
"""

from __future__ import annotations

from typing import Optional, Sequence

from ports_gfx.actions import Action
from ports_gfx.controller import DEFAULT_DEADZONE, ControllerManager
from ports_gfx.controller_config import make_loader, make_saver
from ports_gfx.input_keyboard import action_for_key, action_for_key_up, text_for_input_event
from ports_gfx.input_touch import (
    PointerDebouncer,
    Rect,
    point_from_finger_event,
    point_from_mouse_event,
    resolve_hit,
)


class InputEvent:
    """Result of translating one raw pygame event.

    *action* is the semantic action (if any). *touch_index* is set only for
    a resolved touch/mouse tap, since touch inherently targets a specific
    widget rather than a direction. *text* carries typed unicode for
    ``Action.TEXT_INPUT``.
    """

    __slots__ = ("action", "touch_index", "text")

    def __init__(
        self,
        action: Optional[Action] = None,
        touch_index: Optional[int] = None,
        text: Optional[str] = None,
    ) -> None:
        self.action = action
        self.touch_index = touch_index
        self.text = text

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, InputEvent):
            return NotImplemented
        return (self.action, self.touch_index, self.text) == (other.action, other.touch_index, other.text)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid only
        return f"InputEvent(action={self.action!r}, touch_index={self.touch_index!r}, text={self.text!r})"


_NONE_EVENT = InputEvent()


class InputManager:
    def __init__(
        self,
        pygame,  # noqa: ANN001
        romcloud_bin: str,
        *,
        deadzone: float = DEFAULT_DEADZONE,
    ) -> None:
        self._pygame = pygame
        self.controllers = ControllerManager(
            pygame,
            deadzone=deadzone,
            load_mapping=make_loader(romcloud_bin),
            save_mapping=make_saver(romcloud_bin),
        )
        self._pointer_debouncer = PointerDebouncer()
        self.last_input_mode = "keyboard"

    def handle_event(
        self,
        event,  # noqa: ANN001
        *,
        screen_w: int,
        screen_h: int,
        rects: Sequence[Rect] = (),
        text_mode: bool = False,
        now: float = 0.0,
    ) -> InputEvent:
        pygame = self._pygame
        event_type = event.type

        if event_type == pygame.KEYDOWN:
            action = action_for_key(pygame, event.key, text_mode=text_mode)
            if action is not None:
                self.last_input_mode = "keyboard"
            return InputEvent(action=action)

        if event_type == getattr(pygame, "KEYUP", object()):
            action = action_for_key_up(pygame, event.key)
            return InputEvent(action=action) if action is not None else _NONE_EVENT

        if event_type == getattr(pygame, "TEXTINPUT", object()):
            text = text_for_input_event(event)
            if not text:
                return _NONE_EVENT
            self.last_input_mode = "keyboard"
            return InputEvent(action=Action.TEXT_INPUT, text=text)

        if event_type == getattr(pygame, "MOUSEBUTTONDOWN", object()):
            if not self._pointer_debouncer.should_handle(now):
                return _NONE_EVENT
            index = resolve_hit(rects, point_from_mouse_event(event))
            return self._touch_result(index)

        if event_type == getattr(pygame, "FINGERDOWN", object()):
            if not self._pointer_debouncer.should_handle(now):
                return _NONE_EVENT
            index = resolve_hit(rects, point_from_finger_event(event, screen_w, screen_h))
            return self._touch_result(index)

        action = self.controllers.handle_event(event)
        if action is not None:
            self.last_input_mode = "controller"
            return InputEvent(action=action)
        return _NONE_EVENT

    def _touch_result(self, index: Optional[int]) -> InputEvent:
        if index is None:
            return _NONE_EVENT
        self.last_input_mode = "touch"
        return InputEvent(action=Action.CONFIRM, touch_index=index)

    def update(self, dt: float) -> list[Action]:
        """Call once per frame; returns any repeat-fired directional
        actions from held D-pad/analog-stick input."""
        actions = self.controllers.update(dt)
        if actions:
            self.last_input_mode = "controller"
        return actions
