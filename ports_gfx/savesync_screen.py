"""SaveSync top-level GUI screen — pure state, no pygame.

Flow: Dashboard -> (Upload | Download) -> non-blocking preview -> hold-to-
confirm -> non-blocking commit -> result -> back to Dashboard. Settings
toggles the Original Xbox opt-in. Every backend call goes through the same
``romcloud uidata savesync-*`` bridge the CLI's ``romcloud saves``
commands use (see :mod:`romcloud.services.saves`) — this module never
re-implements selection/diffing/commit logic itself.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from ports_gfx.client import call_backend, operation_result, start_backend_operation
from ports_gfx.hold_confirm import HoldToConfirmState, handle_hold_to_confirm_event
from ports_gfx.input_manager import InputEvent
from ports_gfx.operation import OperationRunner

DASHBOARD = "dashboard"
PREVIEWING = "previewing"
PREVIEW = "preview"
CONFIRMING = "confirming"
COMMITTING = "committing"
RESULT = "result"
SETTINGS = "settings"
APPLYING_SETTINGS = "applying_settings"

DASHBOARD_ITEMS: tuple[str, ...] = ("Upload Saves", "Download Saves", "SaveSync Settings", "Back")
_UPLOAD_INDEX = 0
_DOWNLOAD_INDEX = 1
_SETTINGS_INDEX = 2
_BACK_INDEX = 3

PopenFunc = Callable[..., "object"]


@dataclass
class SaveSyncScreenState:
    romcloud_bin: str
    step: str = DASHBOARD
    selected_index: int = 0
    status: dict[str, Any] = field(default_factory=dict)
    error: str = ""
    direction: str = ""
    diff: dict[str, Any] = field(default_factory=dict)
    preview_summary: dict[str, Any] = field(default_factory=dict)
    result: dict[str, Any] = field(default_factory=dict)
    confirm: HoldToConfirmState = field(default_factory=HoldToConfirmState)
    popen: Optional[PopenFunc] = None
    """Injected for tests; ``None`` uses the real subprocess default."""

    _runner: Optional[OperationRunner] = field(default=None, repr=False)

    # ── dashboard ─────────────────────────────────────────────────────────

    def refresh_status(self, *, run: Optional[Callable[..., Any]] = None) -> None:
        kwargs = {"run": run} if run is not None else {}
        result = call_backend(self.romcloud_bin, "savesync-status", **kwargs)
        self.error = "" if result.ok else result.error
        self.status = result.data if result.ok else {}

    def select(self, index: int) -> None:
        self.selected_index = max(0, min(index, len(DASHBOARD_ITEMS) - 1))

    def confirm_dashboard_selection(self) -> Optional[str]:
        """Returns ``"back"`` if the caller should leave this screen entirely."""
        if self.selected_index == _UPLOAD_INDEX:
            self.start_preview("upload")
        elif self.selected_index == _DOWNLOAD_INDEX:
            self.start_preview("download")
        elif self.selected_index == _SETTINGS_INDEX:
            self.step = SETTINGS
        elif self.selected_index == _BACK_INDEX:
            return "back"
        return None

    # ── preview (non-blocking backend call) ──────────────────────────────

    def start_preview(self, direction: str) -> None:
        self.direction = direction
        self.error = ""
        self._start_operation("savesync-preview", {"direction": direction})
        self.step = PREVIEWING

    # ── confirm ───────────────────────────────────────────────────────────

    def begin_confirm(self) -> None:
        self.confirm = HoldToConfirmState()
        self.step = CONFIRMING

    def handle_confirm_event(self, ievent: InputEvent) -> None:
        handle_hold_to_confirm_event(ievent, self.confirm)

    def update_confirm(self, dt: float) -> None:
        if self.step != CONFIRMING:
            return
        self.confirm.update(dt)
        if self.confirm.cancelled:
            self.step = PREVIEW
        elif self.confirm.confirmed:
            self._start_operation("savesync-commit", {"direction": self.direction, "diff": self.diff})
            self.step = COMMITTING

    # ── settings ──────────────────────────────────────────────────────────

    def set_xbox_enabled(self, enabled: bool) -> None:
        self._start_operation("savesync-settings", {"xbox_enabled": enabled})
        self.step = APPLYING_SETTINGS

    def return_to_dashboard(self) -> None:
        self.step = DASHBOARD
        self.selected_index = 0

    # ── polling (call once per frame; never blocks) ─────────────────────

    def poll(self) -> None:
        if self._runner is None:
            return
        self._runner.poll()
        if not self._runner.is_finished:
            return

        result = operation_result(self._runner)
        self._runner = None

        if self.step == PREVIEWING:
            if result.ok:
                self.diff = result.data.get("diff", {})
                self.preview_summary = result.data
                self.step = PREVIEW
                self.selected_index = 0
            else:
                self.error = result.error
                self.step = DASHBOARD
        elif self.step == COMMITTING:
            if result.ok:
                self.result = result.data.get("record", {})
            else:
                self.error = result.error
            self.step = RESULT
        elif self.step == APPLYING_SETTINGS:
            if result.ok:
                self.status = {**self.status, "xbox_enabled": result.data.get("xbox_enabled", False)}
            else:
                self.error = result.error
            self.step = SETTINGS

    # ── internal ──────────────────────────────────────────────────────────

    def _start_operation(self, action: str, payload: dict[str, Any]) -> None:
        self._runner = start_backend_operation(self.romcloud_bin, action, payload, popen=self.popen)
