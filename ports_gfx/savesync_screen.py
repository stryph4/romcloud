"""Shared save/state GUI screen -- pure state, no pygame.

The dashboard is local-first: local/configured status and writable remote-data
availability are loaded by separate subprocesses while the dashboard remains
interactive.  Preview and commit continue to use the same backend service as
the CLI; this module never re-implements discovery, diffing, or commit logic.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from ports_gfx.client import operation_result, start_backend_operation
from ports_gfx.hold_confirm import HoldToConfirmState, handle_hold_to_confirm_event
from ports_gfx.input_manager import InputEvent
from ports_gfx.operation import OperationRunner
from ports_gfx.savesync_conflict_popup import (
    DONE as CONFLICTS_DONE,
    ConflictPopupState,
)

DASHBOARD = "dashboard"
PREVIEWING = "previewing"
PREVIEW = "preview"
CONFIRMING = "confirming"
COMMITTING = "committing"
RESULT = "result"
SETTINGS = "settings"
APPLYING_SETTINGS = "applying_settings"
CONFLICTS = "conflicts"

REMOTE_CHECKING = "checking"
REMOTE_AVAILABLE = "available"
REMOTE_UNAVAILABLE = "unavailable"
REMOTE_AVAILABILITY_TIMEOUT = 6.0

_DASHBOARD_ACTIONS: tuple[str, ...] = (
    "Quick Sync",
    "Full Sync",
    "Upload All Saves",
    "Download All Saves",
    "SaveSync Settings",
)
DASHBOARD_ITEMS: tuple[str, ...] = (*_DASHBOARD_ACTIONS, "Back")
SETTINGS_ITEMS: tuple[str, ...] = (
    "Auto Sync Saves",
    "Original Xbox virtual drive",
    "Back",
)
_QUICK_SYNC_INDEX = 0
_FULL_SYNC_INDEX = 1
_UPLOAD_INDEX = 2
_DOWNLOAD_INDEX = 3
_SETTINGS_INDEX = 4

PopenFunc = Callable[..., "object"]


@dataclass
class SaveSyncScreenState:
    romcloud_bin: str
    step: str = DASHBOARD
    selected_index: int = 0
    settings_selected_index: int = 0
    status: dict[str, Any] = field(default_factory=dict)
    status_loading: bool = False
    status_error: str = ""
    remote_availability: str = REMOTE_CHECKING
    remote_detail: str = ""
    error: str = ""
    direction: str = ""
    diff: dict[str, Any] = field(default_factory=dict)
    preview_summary: dict[str, Any] = field(default_factory=dict)
    result: dict[str, Any] = field(default_factory=dict)
    result_mode: str = ""
    confirm: HoldToConfirmState = field(default_factory=HoldToConfirmState)
    resolver: Optional[ConflictPopupState] = None
    popen: Optional[PopenFunc] = None
    """Injected for tests; ``None`` uses the real subprocess default."""
    clock: Optional[Callable[[], float]] = None
    """Injected monotonic clock for deterministic availability timeout tests."""
    availability_timeout: float = REMOTE_AVAILABILITY_TIMEOUT

    _runner: Optional[OperationRunner] = field(default=None, repr=False)
    _status_runner: Optional[OperationRunner] = field(default=None, repr=False)
    _availability_runner: Optional[OperationRunner] = field(default=None, repr=False)

    # -- dashboard -------------------------------------------------------

    def start_loading(self) -> None:
        """Load local status and remote availability without blocking the UI."""
        self._cancel_background_checks()
        self.status_loading = True
        self.status_error = ""
        self.remote_availability = REMOTE_CHECKING
        self.remote_detail = ""
        self.error = ""
        self._status_runner = self._new_runner("savesync-status")
        self._availability_runner = self._new_runner(
            "savesync-availability",
            max_runtime=self.availability_timeout,
            timeout_message="Remote storage check timed out.",
        )

    @property
    def remote_actions_available(self) -> bool:
        return (
            self.status.get("remote_configured") is not False
            and self.remote_availability == REMOTE_AVAILABLE
        )

    @property
    def remote_writable(self) -> bool:
        # Older backends reported one combined availability bit; preserve
        # their writable interpretation while new backends send the explicit
        # field needed for read-only Download behavior.
        return bool(self.status.get("remote_writable", True))

    @property
    def dashboard_items(self) -> tuple[str, ...]:
        conflicts = int(self.status.get("active_conflicts", 0))
        resolve = (f"Resolve Conflicts ({conflicts})",) if conflicts > 0 else ()
        return (*_DASHBOARD_ACTIONS, *resolve, "Back")

    def select(self, index: int) -> None:
        self.selected_index = max(0, min(index, len(self.dashboard_items) - 1))

    def confirm_dashboard_selection(self) -> Optional[str]:
        """Returns ``"back"`` if the caller should leave this screen entirely."""
        selected = self.dashboard_items[self.selected_index]
        if self.selected_index in (
            _UPLOAD_INDEX,
            _DOWNLOAD_INDEX,
            _QUICK_SYNC_INDEX,
            _FULL_SYNC_INDEX,
        ):
            if self.status.get("remote_configured") is False:
                self.error = (
                    "Configure writable ROMCloud data storage before using SaveSync."
                )
            elif self.remote_availability == REMOTE_CHECKING:
                self.error = "Remote storage is still being checked. Try again shortly."
            elif not self.remote_actions_available:
                detail = f" {self.remote_detail}" if self.remote_detail else ""
                self.error = f"Remote storage is unavailable.{detail}"
            elif self.selected_index in (
                _UPLOAD_INDEX,
                _QUICK_SYNC_INDEX,
                _FULL_SYNC_INDEX,
            ) and not self.remote_writable:
                self.error = "Remote storage is read-only; Download remains available."
            elif self.selected_index == _QUICK_SYNC_INDEX:
                self.start_quick_sync()
            elif self.selected_index == _FULL_SYNC_INDEX:
                self.start_full_sync()
            else:
                self.start_preview(
                    "upload" if self.selected_index == _UPLOAD_INDEX else "download"
                )
        elif self.selected_index == _SETTINGS_INDEX:
            self.error = ""
            self.step = SETTINGS
        elif selected.startswith("Resolve Conflicts"):
            self.error = ""
            self.resolver = ConflictPopupState(
                self.romcloud_bin,
                source="manual",
                popen=self.popen,
                clock=self.clock,
            )
            self.resolver.start()
            self.step = CONFLICTS
        elif selected == "Back":
            return "back"
        return None

    # -- preview (non-blocking backend call) ----------------------------

    def start_preview(self, direction: str) -> None:
        self.direction = direction
        self.error = ""
        self.result_mode = ""
        self._start_operation("savesync-preview", {"direction": direction})
        self.step = PREVIEWING

    def start_quick_sync(self) -> None:
        self.direction = "quick-sync"
        self.error = ""
        self.result = {}
        self.result_mode = "quick-sync"
        self._start_operation("savesync-quick-sync", {})
        self.step = PREVIEWING

    def start_full_sync(self) -> None:
        self.direction = "full-sync"
        self.error = ""
        self.result = {}
        self.result_mode = "full-sync"
        self._start_operation("savesync-full-sync", {})
        self.step = PREVIEWING

    # -- confirm ---------------------------------------------------------

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
            self._start_operation(
                "savesync-commit",
                {"direction": self.direction, "diff": self.diff},
            )
            self.step = COMMITTING

    def update(self, dt: float) -> None:
        if self.step != CONFLICTS or self.resolver is None:
            self.update_confirm(dt)
            return
        self.resolver.update(dt)
        if self.resolver.step == CONFLICTS_DONE:
            self.resolver = None
            self.step = DASHBOARD
            self.selected_index = 0
            self.start_loading()

    def handle_conflict_event(self, event: InputEvent) -> None:
        if self.step == CONFLICTS and self.resolver is not None:
            self.resolver.handle_event(event)

    # -- settings --------------------------------------------------------

    def set_xbox_enabled(self, enabled: bool) -> None:
        self._cancel_runner("_status_runner")
        self.status_loading = False
        self._start_operation("savesync-settings", {"xbox_enabled": enabled})
        self.step = APPLYING_SETTINGS

    def set_auto_sync_enabled(self, enabled: bool) -> None:
        self._cancel_runner("_status_runner")
        self.status_loading = False
        self._start_operation("savesync-settings", {"auto_sync_enabled": enabled})
        self.step = APPLYING_SETTINGS

    @property
    def settings_items(self) -> tuple[str, ...]:
        auto_label = (
            "On"
            if self.status.get("auto_sync_enabled") is True
            else "Off"
            if self.status.get("auto_sync_enabled") is False
            else "Loadingâ€¦"
            if self.status_loading
            else "Unknown"
        )
        return (
            f"Auto Sync Saves: {auto_label}",
            "Original Xbox virtual drive",
            "Back",
        )

    def select_setting(self, index: int) -> None:
        self.settings_selected_index = max(0, min(index, len(SETTINGS_ITEMS) - 1))

    def confirm_settings_selection(self) -> Optional[str]:
        if self.settings_selected_index == 0:
            if "auto_sync_enabled" not in self.status:
                self.error = "Local SaveSync settings are still loading."
            else:
                self.error = ""
                self.set_auto_sync_enabled(not self.status["auto_sync_enabled"])
        elif self.settings_selected_index == 1:
            if "xbox_enabled" not in self.status:
                self.error = "Local SaveSync settings are still loading."
            else:
                self.error = ""
                self.set_xbox_enabled(not self.status["xbox_enabled"])
        else:
            self.return_to_dashboard()
            return "back"
        return None

    def return_to_dashboard(self) -> None:
        self.step = DASHBOARD
        self.selected_index = 0

    # -- polling (call once per frame) ----------------------------------

    def poll(self) -> list:
        drained: list = []
        drained.extend(self._poll_status())
        drained.extend(self._poll_availability())
        drained.extend(self._poll_foreground_operation())
        return drained

    def _poll_status(self) -> list:
        runner = self._status_runner
        if runner is None:
            return []
        drained = runner.poll()
        if not runner.is_finished:
            return drained
        result = operation_result(runner)
        self._status_runner = None
        self.status_loading = False
        if result.ok:
            live_availability_state = {}
            if self.remote_availability != REMOTE_CHECKING:
                live_availability_state = {
                    key: self.status[key]
                    for key in ("remote_configured", "sync_status", "active_conflicts")
                    if key in self.status
                }
            self.status = {**result.data, **live_availability_state}
            self.status_error = ""
        else:
            self.status_error = result.error
        return drained

    def _poll_availability(self) -> list:
        runner = self._availability_runner
        if runner is None:
            return []
        drained = runner.poll()
        if not runner.is_finished:
            return drained
        result = operation_result(runner)
        self._availability_runner = None
        if result.ok:
            configured = bool(result.data.get("remote_configured", False))
            availability_state = {
                key: result.data[key]
                for key in ("sync_status", "active_conflicts")
                if key in result.data
            }
            self.status = {
                **self.status,
                "remote_configured": configured,
                "remote_readable": bool(result.data.get("remote_readable", False)),
                "remote_writable": bool(result.data.get("remote_writable", False)),
                **availability_state,
            }
            available = bool(
                result.data.get(
                    "remote_available", result.data.get("remote_reachable", False)
                )
            )
            self.remote_availability = (
                REMOTE_AVAILABLE if configured and available else REMOTE_UNAVAILABLE
            )
            self.remote_detail = str(result.data.get("detail", ""))
        else:
            self.remote_availability = REMOTE_UNAVAILABLE
            self.remote_detail = result.error
        return drained

    def _poll_foreground_operation(self) -> list:
        if self._runner is None:
            return []
        drained = self._runner.poll()
        if not self._runner.is_finished:
            return drained

        result = operation_result(self._runner)
        self._runner = None

        if self.step == PREVIEWING:
            if self.result_mode in ("quick-sync", "full-sync"):
                if result.ok:
                    self.result = result.data
                    self.error = ""
                else:
                    self.error = result.error
                self.step = RESULT
            elif result.ok:
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
                    "auto_sync_enabled": result.data.get(
                        "auto_sync_enabled",
                        self.status.get("auto_sync_enabled", False),
                    ),
                    "xbox_enabled": result.data.get(
                        "xbox_enabled", self.status.get("xbox_enabled", False)
                    ),
                    # Retain backend compatibility values even though unsafe
                    # RPCS3 application data and the old local-game opt-in are
                    # no longer exposed by this screen.
                    "rpcs3_installed_games_enabled": result.data.get(
                        "rpcs3_installed_games_enabled",
                        self.status.get("rpcs3_installed_games_enabled", False),
                    ),
                }
            else:
                self.error = result.error
            self.step = SETTINGS
        return drained

    def cancel_pending(self) -> None:
        """Boundedly cancel every subprocess owned by this screen."""
        if self.resolver is not None:
            self.resolver.cancel_pending()
            self.resolver = None
        self._cancel_runner("_runner")
        self._cancel_background_checks()

    # -- internal --------------------------------------------------------

    def _cancel_background_checks(self) -> None:
        self._cancel_runner("_status_runner")
        self._cancel_runner("_availability_runner")

    def _cancel_runner(self, attribute: str) -> None:
        runner = getattr(self, attribute)
        if runner is not None:
            runner.cancel()
            setattr(self, attribute, None)

    def _new_runner(
        self,
        action: str,
        payload: dict[str, Any] | None = None,
        *,
        max_runtime: float | None = None,
        timeout_message: str = "operation timed out",
    ) -> OperationRunner:
        return start_backend_operation(
            self.romcloud_bin,
            action,
            payload,
            popen=self.popen,
            max_runtime=max_runtime,
            timeout_message=timeout_message,
            clock=self.clock,
        )

    def _start_operation(self, action: str, payload: dict[str, Any]) -> None:
        self._runner = self._new_runner(
            action,
            {**payload, "progress": True},
        )
