"""Decision state for conflicts discovered by setup's initial SaveSync."""

from __future__ import annotations

from dataclasses import dataclass

from ports_gfx.actions import Action
from ports_gfx.input_manager import InputEvent

ACTION_LABELS = ("Resolve Save Conflicts", "Finish Setup")


@dataclass
class SetupSaveConflictState:
    conflict_ids: tuple[str, ...]
    selected_index: int = 0

    @property
    def action_labels(self) -> tuple[str, ...]:
        return ACTION_LABELS

    def select(self, index: int) -> None:
        self.selected_index = max(0, min(index, len(self.action_labels) - 1))

    def handle_event(self, event: InputEvent) -> str | None:
        if event.touch_index is not None:
            self.select(event.touch_index)
        if event.action == Action.UP:
            self.select(self.selected_index - 1)
        elif event.action == Action.DOWN:
            self.select(self.selected_index + 1)
        elif event.action == Action.BACK:
            return "cancel"
        elif event.action == Action.CONFIRM:
            return "resolve" if self.selected_index == 0 else "finish"
        return None

    def update(self, _dt: float) -> None:
        return None
