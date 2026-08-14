"""State for the startup-integration activation prompt."""

from __future__ import annotations

from dataclasses import dataclass

RESTART_NOW = "restart"
LATER = "later"


@dataclass
class StartupRestartPromptState:
    """Small, controller-friendly Restart Now / Later choice."""

    selected_index: int = 0
    error: str | None = None

    @property
    def actions(self) -> tuple[str, str]:
        return ("Restart Now", "Later")

    def move(self, delta: int) -> None:
        self.selected_index = (self.selected_index + delta) % len(self.actions)
        self.error = None

    def activate(self) -> str:
        return RESTART_NOW if self.selected_index == 0 else LATER

