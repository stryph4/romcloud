"""Controller-friendly state for selecting managed source systems."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Optional

from ports_gfx.client import operation_result, start_backend_operation
from ports_gfx.operation import OperationRunner

LOADING = "loading"
SELECTING = "selecting"
APPLYING = "applying"
RESULT = "result"

PopenFunc = Callable[..., object]


@dataclass
class SystemSelectionScreenState:
    romcloud_bin: str
    step: str = LOADING
    detected_systems: list[str] = field(default_factory=list)
    selected_systems: set[str] = field(default_factory=set)
    selected_index: int = 0
    result: dict = field(default_factory=dict)
    error: str = ""
    popen: Optional[PopenFunc] = None
    _runner: Optional[OperationRunner] = field(default=None, repr=False)

    @property
    def options(self) -> list[str]:
        if self.step != SELECTING:
            return []
        return [
            "Select All",
            "Clear All",
            *[
                f"[{'x' if system in self.selected_systems else ' '}] {system}"
                for system in self.detected_systems
            ],
            "Save Selection",
        ]

    def start_loading(self) -> None:
        self.cancel_pending()
        self.step = LOADING
        self.error = ""
        self._runner = start_backend_operation(
            self.romcloud_bin,
            "system-selection-status",
            popen=self.popen,
        )

    def move(self, delta: int) -> None:
        if self.options:
            self.selected_index = (self.selected_index + delta) % len(self.options)

    def activate(self) -> None:
        if self.step == RESULT:
            if self.error:
                self.start_loading()
            return
        if self.step != SELECTING:
            return
        if self.selected_index == 0:
            self.selected_systems = set(self.detected_systems)
        elif self.selected_index == 1:
            self.selected_systems.clear()
        elif self.selected_index < len(self.detected_systems) + 2:
            system = self.detected_systems[self.selected_index - 2]
            if system in self.selected_systems:
                self.selected_systems.remove(system)
            else:
                self.selected_systems.add(system)
        else:
            self.error = ""
            self.step = APPLYING
            self._runner = start_backend_operation(
                self.romcloud_bin,
                "system-selection-apply",
                {
                    "selected_systems": [
                        system
                        for system in self.detected_systems
                        if system in self.selected_systems
                    ],
                    "progress": True,
                },
                popen=self.popen,
            )

    def poll(self) -> list:
        if self._runner is None:
            return []
        drained = self._runner.poll()
        if not self._runner.is_finished:
            return drained
        outcome = operation_result(self._runner)
        self._runner = None
        if not outcome.ok:
            self.error = outcome.error
            self.step = RESULT
            return drained
        if self.step == LOADING:
            self.detected_systems = [
                str(system) for system in outcome.data.get("detected_systems", [])
            ]
            self.selected_systems = {
                str(system) for system in outcome.data.get("selected_systems", [])
            }
            self.selected_index = 0
            self.step = SELECTING
        else:
            self.result = outcome.data
            self.error = ""
            self.step = RESULT
        return drained

    def cancel_pending(self) -> None:
        if self._runner is not None:
            self._runner.cancel()
            self._runner = None
