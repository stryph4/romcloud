"""Controller-safe Uninstall/Purge confirmation and detached handoff."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from typing import Callable

from ports_gfx.actions import Action
from ports_gfx.hold_confirm import HoldToConfirmState, handle_hold_to_confirm_event
from ports_gfx.input_manager import InputEvent


@dataclass
class LifecycleScreenState:
    operation: str = "uninstall"
    view: str = "choices"  # choices | confirm
    selected_index: int = 0
    confirm: HoldToConfirmState = field(default_factory=HoldToConfirmState)

    @property
    def title(self) -> str:
        return "Uninstall ROMCloud" if self.operation == "uninstall" else "Purge all local ROMCloud data"

    @property
    def choices(self) -> tuple[str, ...]:
        if self.operation == "uninstall":
            return ("Uninstall", "Advanced", "Back")
        return ("Purge all local data", "Back")

    @property
    def body(self) -> str:
        if self.operation == "uninstall":
            return (
                "Remove ROMCloud from this device while keeping your settings, "
                "credentials, cached games, downloads, and sync state."
            )
        return (
            "This removes ROMCloud's settings, credentials, cache, download history, "
            "and local sync metadata. Your original ROMs, emulator saves, external "
            "keys, and remote server data will NOT be deleted."
        )

    def move(self, delta: int) -> None:
        self.selected_index = (self.selected_index + delta) % len(self.choices)

    def handle_event(self, event: InputEvent) -> str:
        """Return stay, back, or confirmed; all destructive paths require a hold."""
        if self.view == "confirm":
            handle_hold_to_confirm_event(event, self.confirm)
            if self.confirm.cancelled:
                self.confirm = HoldToConfirmState()
                self.view = "choices"
            return "stay"
        if event.action == Action.BACK:
            return "back"
        if event.action in (Action.UP, Action.LEFT):
            self.move(-1)
        elif event.action in (Action.DOWN, Action.RIGHT):
            self.move(1)
        elif event.action == Action.CONFIRM:
            choice = self.choices[self.selected_index]
            if choice == "Back":
                return "back"
            if choice == "Advanced":
                self.operation = "purge"
                self.selected_index = 0
            else:
                self.confirm = HoldToConfirmState()
                self.confirm.press()
                self.view = "confirm"
        return "stay"

    def update(self, dt: float) -> bool:
        if self.view != "confirm":
            return False
        self.confirm.update(dt)
        return self.confirm.confirmed


def launch_lifecycle_helper(
    romcloud_bin: str,
    operation: str,
    *,
    gui_pid: int | None = None,
    run: Callable[..., object] = subprocess.run,
) -> object:
    """Stage the independent worker before allowing this GUI to exit."""
    if operation not in {"uninstall", "purge"}:
        raise ValueError(f"Unsupported lifecycle operation: {operation}")
    pid = os.getpid() if gui_pid is None else gui_pid
    result = run(
        [romcloud_bin, operation, "--yes", "--stage-for-pid", str(pid)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    if getattr(result, "returncode", 1) != 0:
        raise RuntimeError("Could not stage the ROMCloud lifecycle helper; nothing was removed")
    return result
