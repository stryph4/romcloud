"""Deliberate graphical source-metadata import workflow."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from ports_gfx.activity import ActivityEvent, parse_progress_line
from ports_gfx.client import operation_result, start_backend_operation
from ports_gfx.hold_confirm import HoldToConfirmState, handle_hold_to_confirm_event
from ports_gfx.input_manager import InputEvent
from ports_gfx.operation import OperationRunner

PREFLIGHTING = "preflighting"
PREFLIGHT = "preflight"
CONFIRMING = "confirming"
IMPORTING = "importing"
RESULT = "result"

PopenFunc = Callable[..., object]


@dataclass
class LibrarySyncScreenState:
    romcloud_bin: str
    step: str = PREFLIGHTING
    preview: dict[str, Any] = field(default_factory=dict)
    result: dict[str, Any] = field(default_factory=dict)
    error: str = ""
    cancelled: bool = False
    confirm: HoldToConfirmState = field(default_factory=HoldToConfirmState)
    latest_progress: ActivityEvent | None = None
    popen: Optional[PopenFunc] = None
    _runner: Optional[OperationRunner] = field(default=None, repr=False)

    def start_preview(self) -> None:
        self.error = ""
        self.cancelled = False
        self.latest_progress = None
        self._start_operation("library-sync-preview", {})
        self.step = PREFLIGHTING

    def begin_confirm(self) -> None:
        self.confirm = HoldToConfirmState()
        self.step = CONFIRMING

    def handle_confirm_event(self, event: InputEvent) -> None:
        handle_hold_to_confirm_event(event, self.confirm)

    def update_confirm(self, dt: float) -> None:
        if self.step != CONFIRMING:
            return
        self.confirm.update(dt)
        if self.confirm.cancelled:
            self.step = PREFLIGHT
        elif self.confirm.confirmed:
            self._start_operation("library-sync", {})
            self.step = IMPORTING

    def cancel_import(self) -> None:
        if self.step != IMPORTING or self._runner is None:
            return
        self._runner.cancel()
        self._runner = None
        self.cancelled = True
        self.error = "Import canceled. ROMCloud remains usable and the import is safe to retry."
        self.step = RESULT

    def cancel_pending(self) -> None:
        if self._runner is not None:
            self._runner.cancel()
            self._runner = None

    def poll(self) -> list:
        if self._runner is None:
            return []
        drained = self._runner.poll()
        for line in drained:
            event = parse_progress_line(line.text)
            if event is not None:
                self.latest_progress = event
        if not self._runner.is_finished:
            return drained

        outcome = operation_result(self._runner)
        self._runner = None
        if self.step == PREFLIGHTING:
            if outcome.ok:
                self.preview = outcome.data
                self.step = PREFLIGHT
            else:
                self.error = outcome.error
                self.step = RESULT
        elif self.step == IMPORTING:
            if outcome.ok:
                self.result = outcome.data
                self.error = ""
            else:
                self.error = outcome.error
            self.step = RESULT
        return drained

    def retry(self) -> None:
        self.start_preview()

    @property
    def progress_fraction(self) -> float | None:
        event = self.latest_progress
        if event is None or event.current is None or event.total is None or event.total <= 0:
            return None
        return max(0.0, min(1.0, event.current / event.total))

    def _start_operation(self, action: str, payload: dict[str, Any]) -> None:
        self._runner = start_backend_operation(
            self.romcloud_bin,
            action,
            {**payload, "progress": True},
            popen=self.popen,
        )
