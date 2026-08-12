"""Shared save/state GUI screen — pure state, no pygame.

Flow: Dashboard -> (Upload | Download) -> non-blocking preview -> hold-to-
confirm -> non-blocking commit -> result -> back to Dashboard. Settings expose
the heavyweight-content and local-game opt-ins. Every backend call goes through the same
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
RPCS3_WARNING = "rpcs3_warning"
RPCS3_CONFIRMING = "rpcs3_confirming"
LOCAL_GAMES_WARNING = "local_games_warning"

DASHBOARD_ITEMS: tuple[str, ...] = (
    "Upload All Saves",
    "Download All Saves",
    "SaveSync Settings",
    "Back",
)
SETTINGS_ITEMS: tuple[str, ...] = (
    "Original Xbox virtual drive",
    "Include RPCS3 Installed Games",
    "Include Local Games in Save Sync",
    "Back",
)
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
    settings_selected_index: int = 0
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
            if self.status.get("remote_configured", False):
                self.start_preview("upload")
            else:
                self.error = "Configure writable ROMCloud data storage before using SaveSync."
        elif self.selected_index == _DOWNLOAD_INDEX:
            if self.status.get("remote_configured", False):
                self.start_preview("download")
            else:
                self.error = "Configure writable ROMCloud data storage before using SaveSync."
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
        if self.step not in (CONFIRMING, RPCS3_CONFIRMING):
            return
        purpose = self.step
        self.confirm.update(dt)
        if self.confirm.cancelled:
            self.step = PREVIEW if purpose == CONFIRMING else RPCS3_WARNING
        elif self.confirm.confirmed:
            if purpose == CONFIRMING:
                self._start_operation(
                    "savesync-commit",
                    {"direction": self.direction, "diff": self.diff},
                )
                self.step = COMMITTING
            else:
                self.set_rpcs3_installed_games_enabled(True)

    # ── settings ──────────────────────────────────────────────────────────

    def set_xbox_enabled(self, enabled: bool) -> None:
        self._start_operation("savesync-settings", {"xbox_enabled": enabled})
        self.step = APPLYING_SETTINGS

    def select_setting(self, index: int) -> None:
        self.settings_selected_index = max(0, min(index, len(SETTINGS_ITEMS) - 1))

    def confirm_settings_selection(self) -> Optional[str]:
        if self.settings_selected_index == 0:
            self.set_xbox_enabled(not self.status.get("xbox_enabled", False))
        elif self.settings_selected_index == 1:
            enabled = self.status.get("rpcs3_installed_games_enabled", False)
            if enabled:
                self.set_rpcs3_installed_games_enabled(False)
            else:
                self.step = RPCS3_WARNING
        elif self.settings_selected_index == 2:
            enabled = self.status.get("include_local_games", False)
            if enabled:
                self.set_include_local_games(False)
            else:
                self.step = LOCAL_GAMES_WARNING
        else:
            self.return_to_dashboard()
            return "back"
        return None

    def begin_rpcs3_confirm(self) -> None:
        self.confirm = HoldToConfirmState()
        self.step = RPCS3_CONFIRMING

    def set_rpcs3_installed_games_enabled(self, enabled: bool) -> None:
        self._start_operation(
            "savesync-settings", {"rpcs3_installed_games_enabled": enabled}
        )
        self.step = APPLYING_SETTINGS

    def set_include_local_games(self, enabled: bool) -> None:
        self._start_operation("savesync-settings", {"include_local_games": enabled})
        self.step = APPLYING_SETTINGS

    def return_to_dashboard(self) -> None:
        self.step = DASHBOARD
        self.selected_index = 0

    # ── polling (call once per frame; never blocks) ─────────────────────

    def poll(self) -> list:
        if self._runner is None:
            return []
        drained = self._runner.poll()
        if not self._runner.is_finished:
            return drained

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
                self.status = {
                    **self.status,
                    "xbox_enabled": result.data.get(
                        "xbox_enabled", self.status.get("xbox_enabled", False)
                    ),
                    "rpcs3_installed_games_enabled": result.data.get(
                        "rpcs3_installed_games_enabled",
                        self.status.get("rpcs3_installed_games_enabled", False),
                    ),
                    "include_local_games": result.data.get(
                        "include_local_games",
                        self.status.get("include_local_games", False),
                    ),
                }
            else:
                self.error = result.error
            self.step = SETTINGS
        return drained

    def cancel_pending(self) -> None:
        if self._runner is not None:
            self._runner.cancel()
            self._runner = None

    # ── internal ──────────────────────────────────────────────────────────

    def _start_operation(self, action: str, payload: dict[str, Any]) -> None:
        self._runner = start_backend_operation(
            self.romcloud_bin,
            action,
            {**payload, "progress": True},
            popen=self.popen,
        )
