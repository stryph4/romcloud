"""Decision state for save-authority conflicts discovered entering Direct."""

from __future__ import annotations

from dataclasses import dataclass, field

from ports_gfx.actions import Action
from ports_gfx.hold_confirm import HoldToConfirmState, handle_hold_to_confirm_event
from ports_gfx.input_manager import InputEvent

ACTION_LABELS = ("Resolve Conflicts", "Use Remote Saves & Continue", "Cancel")


@dataclass
class ModeSaveConflictState:
    conflict_ids: tuple[str, ...]
    purpose: str = "direct"
    selected_index: int = 0
    confirm: HoldToConfirmState = field(default_factory=HoldToConfirmState)

    @property
    def action_labels(self) -> tuple[str, ...]:
        if self.purpose == "setup":
            return ("Resolve Save Conflicts", "Finish Setup")
        if self.purpose == "setup-direct":
            return (
                "Resolve Save Conflicts",
                "Use Remote Saves & Continue",
                "Finish Setup",
            )
        return ACTION_LABELS

    @property
    def remote_wins_index(self) -> int | None:
        return 1 if self.purpose in {"direct", "setup-direct"} else None

    def select(self, index: int) -> None:
        selected = max(0, min(index, len(self.action_labels) - 1))
        if selected != self.selected_index:
            self.confirm.reset()
        self.selected_index = selected

    def handle_event(self, event: InputEvent) -> str | None:
        if event.touch_index is not None:
            self.select(event.touch_index)
        if event.action == Action.UP:
            self.select(self.selected_index - 1)
        elif event.action == Action.DOWN:
            self.select(self.selected_index + 1)
        elif event.action == Action.BACK:
            return "cancel"
        elif self.selected_index == self.remote_wins_index:
            handle_hold_to_confirm_event(event, self.confirm)
        elif event.action == Action.CONFIRM:
            return "resolve" if self.selected_index == 0 else "finish"
        return None

    def update(self, dt: float) -> str | None:
        if self.selected_index != self.remote_wins_index:
            return None
        self.confirm.update(dt)
        return "remote-wins" if self.confirm.confirmed else None
