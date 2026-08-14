"""Native presentation state for the existing browser Library Manager."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from ports_gfx.client import operation_result, start_backend_operation
from ports_gfx.operation import OperationRunner


STARTING = "starting"
READY = "ready"
FAILED = "failed"
PopenFunc = Callable[..., object]


@dataclass
class LibraryManagerScreenState:
    romcloud_bin: str
    step: str = STARTING
    details: dict[str, Any] = field(default_factory=dict)
    error: str = ""
    popen: Optional[PopenFunc] = None
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

    def poll(self) -> list:
        if self._runner is None:
            return []
        drained = self._runner.poll()
        if not self._runner.is_finished:
            return drained
        result = operation_result(self._runner)
        self._runner = None
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
