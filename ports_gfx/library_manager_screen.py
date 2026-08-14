"""Native presentation state for the existing browser Library Manager."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from ports_gfx.client import operation_result, start_backend_operation
from ports_gfx.operation import OperationRunner


STARTING = "starting"
READY = "ready"
FAILED = "failed"
OPENING = "opening"
PopenFunc = Callable[..., object]


@dataclass
class LibraryManagerScreenState:
    romcloud_bin: str
    step: str = STARTING
    details: dict[str, Any] = field(default_factory=dict)
    error: str = ""
    popen: Optional[PopenFunc] = None
    selected_index: int = 0
    operation: str = "start"
    _runner: Optional[OperationRunner] = field(default=None, repr=False)

    def start_or_refresh(self) -> None:
        self.cancel_pending()
        self.step = STARTING
        self.details = {}
        self.error = ""
        self._runner = start_backend_operation(
            self.romcloud_bin,
            "manager-start",
            popen=self.popen,
        )

    @property
    def actions(self) -> tuple[str, ...]:
        return ("Open Here", "Pair Another Device", "Refresh")

    def move(self, delta: int) -> None:
        self.selected_index = (self.selected_index + delta) % len(self.actions)

    def activate(self) -> None:
        action = self.actions[self.selected_index]
        if action == "Open Here":
            self.cancel_pending()
            self.operation = "open"
            self.step = OPENING
            self.error = ""
            self._runner = start_backend_operation(
                self.romcloud_bin, "manager-open-local", popen=self.popen
            )
        elif action == "Pair Another Device":
            self.cancel_pending()
            self.operation = "pair"
            self.step = STARTING
            self.error = ""
            self._runner = start_backend_operation(
                self.romcloud_bin, "manager-pair", popen=self.popen
            )
        else:
            self.operation = "start"
            self.start_or_refresh()

    def poll(self) -> list:
        if self._runner is None:
            return []
        drained = self._runner.poll()
        if not self._runner.is_finished:
            return drained
        result = operation_result(self._runner)
        self._runner = None
        if self.operation == "open":
            if result.ok:
                self.step = READY
            else:
                self.error = result.error or "The local browser could not be opened."
                self.step = FAILED
            return drained
        if self.operation == "pair":
            if result.ok:
                self.details = {
                    **self.details,
                    "url": result.data.get("url", self.details.get("url", "")),
                    "pairing_code": result.data.get("code", ""),
                    "pairing_expires_in": result.data.get("expires_in", 120),
                }
                self.error = ""
                self.step = READY
            else:
                self.error = result.error or "A pairing code could not be created."
                self.step = FAILED
            return drained
        if result.ok and result.data.get("running"):
            self.details = result.data
            self.error = ""
            self.step = READY
        else:
            self.error = result.error or "Library Manager is not running."
            self.step = FAILED
        return drained

    def cancel_pending(self) -> None:
        if self._runner is not None:
            self._runner.cancel()
            self._runner = None

    @property
    def running(self) -> bool:
        return self.step == READY and bool(self.details.get("running"))
