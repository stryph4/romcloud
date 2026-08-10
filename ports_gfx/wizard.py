"""Pure state and navigation for ROMCloud's graphical first-run wizard."""

from __future__ import annotations

from enum import Enum
from typing import Any, Sequence

from ports_gfx.actions import ACTION_DIRECTIONS, Action
from ports_gfx.client import BackendResult, operation_result, start_backend_operation
from ports_gfx.input_manager import InputEvent
from ports_gfx.layout import Rect, find_next_focus_index
from ports_gfx.operation import OperationRunner
from ports_gfx.osk import OskState


class WizardStep(Enum):
    WELCOME = "welcome"
    SOURCE = "source"
    SERVER = "server"
    USERNAME = "username"
    PASSWORD = "password"
    DISCOVER = "discover"
    SHARE = "share"
    DETECT = "detect"
    SYSTEMS = "systems"
    REMOTE_DATA = "remote_data"
    REMOTE_LOCAL = "remote_local"
    REMOTE_AUTH = "remote_auth"
    REMOTE_SERVER = "remote_server"
    REMOTE_USERNAME = "remote_username"
    REMOTE_PASSWORD = "remote_password"
    REMOTE_DISCOVER = "remote_discover"
    REMOTE_SHARE = "remote_share"
    REMOTE_VALIDATE = "remote_validate"
    CACHE = "cache"
    REVIEW = "review"
    APPLY = "apply"
    DONE = "done"


STEPS = tuple(WizardStep)
TEXT_STEPS = (
    WizardStep.SERVER,
    WizardStep.USERNAME,
    WizardStep.PASSWORD,
    WizardStep.REMOTE_LOCAL,
    WizardStep.REMOTE_SERVER,
    WizardStep.REMOTE_USERNAME,
    WizardStep.REMOTE_PASSWORD,
)
CACHE_FIELDS = ("cache_root", "max_size_gb", "min_free_gb")


class WizardState:
    """Device-agnostic setup state. Secrets live only in this process."""

    def __init__(self, status: BackendResult | None = None) -> None:
        data = status.data if status and status.ok else {}
        self.mode = str(data.get("state", "fresh"))
        self.step = WizardStep.WELCOME
        self.selected_index = 0
        self.server = str(data.get("server", ""))
        self.username = str(data.get("username", ""))
        self.password = ""
        self.port = int(data.get("port", 445))
        self.share = str(data.get("share", ""))
        self.rom_root = str(data.get("rom_root", "/userdata/romcloud/source"))
        self.shares: list[dict[str, str]] = []
        self.systems: list[str] = []
        self.remote_data_type = str(data.get("remote_data_type", "none"))
        self.remote_data_root = str(data.get("remote_data_root", ""))
        self.remote_server = str(data.get("remote_server", ""))
        self.remote_username = str(data.get("remote_username", ""))
        self.remote_password = ""
        self.remote_port = int(data.get("remote_port", 445))
        self.remote_reuse_source_credentials = False
        self.remote_share = str(data.get("remote_share", ""))
        self.remote_shares: list[dict[str, str]] = []
        self.cache_root = str(data.get("cache_root", "/userdata/romcloud/cache"))
        self.max_size_gb = float(data.get("max_size_gb", 50.0))
        self.min_free_gb = float(data.get("min_free_gb", 5.0))
        self.issues = [str(issue) for issue in data.get("issues", [])]
        self.error = status.error if status and not status.ok else ""
        self.osk: OskState | None = None
        self.cache_osk_field: str | None = None
        self.runner: OperationRunner | None = None
        self.finished = False
        self.applied_summary: dict[str, Any] = {}
        self.source_validation: dict[str, Any] = {}
        self.remote_validation: dict[str, Any] = {}

    @property
    def step_number(self) -> int:
        return STEPS.index(self.step) + 1

    @property
    def title(self) -> str:
        titles = {
            WizardStep.WELCOME: "Repair ROMCloud" if self.mode == "partial" else "Welcome to ROMCloud",
            WizardStep.SOURCE: "Choose Source Type",
            WizardStep.SERVER: "SMB Server",
            WizardStep.USERNAME: "SMB Username",
            WizardStep.PASSWORD: "SMB Password",
            WizardStep.DISCOVER: "Discover Shares",
            WizardStep.SHARE: "Select Share",
            WizardStep.DETECT: "Detect Systems",
            WizardStep.SYSTEMS: "Detected Systems",
            WizardStep.REMOTE_DATA: "ROMCloud Data Storage",
            WizardStep.REMOTE_LOCAL: "Local Data Directory",
            WizardStep.REMOTE_AUTH: "Data SMB Credentials",
            WizardStep.REMOTE_SERVER: "Data SMB Server",
            WizardStep.REMOTE_USERNAME: "Data SMB Username",
            WizardStep.REMOTE_PASSWORD: "Data SMB Password",
            WizardStep.REMOTE_DISCOVER: "Discover Data Shares",
            WizardStep.REMOTE_SHARE: "Select Data Share",
            WizardStep.REMOTE_VALIDATE: "Validate Data Share",
            WizardStep.CACHE: "Cache Settings",
            WizardStep.REVIEW: "Review Setup",
            WizardStep.APPLY: "Configure ROMCloud",
            WizardStep.DONE: "Setup Complete",
        }
        return titles[self.step]

    @property
    def options(self) -> list[str]:
        if self.step == WizardStep.WELCOME:
            return ["Resume / Repair Setup" if self.mode == "partial" else "Start Setup"]
        if self.step == WizardStep.SOURCE:
            return ["SMB network share", "Local / external (coming later)"]
        if self.step == WizardStep.SHARE:
            return [share["name"] for share in self.shares]
        if self.step == WizardStep.REMOTE_DATA:
            return [
                "SMB network location",
                "Local / external directory",
                "Skip (SaveSync unavailable)",
            ]
        if self.step == WizardStep.REMOTE_AUTH:
            return [
                "Use same server and credentials",
                "Use different server or credentials",
            ]
        if self.step == WizardStep.REMOTE_SHARE:
            return [share["name"] for share in self.remote_shares]
        if self.step == WizardStep.CACHE and self.osk is None:
            return [
                f"Location: {self.cache_root}",
                f"Maximum size: {self.max_size_gb:g} GB",
                f"Minimum free: {self.min_free_gb:g} GB",
                "Continue",
            ]
        if self.step in (WizardStep.SYSTEMS, WizardStep.REVIEW, WizardStep.DONE):
            return ["Continue" if self.step != WizardStep.DONE else "Finish"]
        if self.step in (
            WizardStep.DISCOVER,
            WizardStep.DETECT,
            WizardStep.REMOTE_DISCOVER,
            WizardStep.REMOTE_VALIDATE,
            WizardStep.APPLY,
        ) and self.runner is None:
            return ["Retry"]
        return []

    @property
    def is_text_mode(self) -> bool:
        return self.osk is not None

    def widget_rects(self, default_rects: Sequence[Rect], osk_rects: Sequence[Rect]) -> Sequence[Rect]:
        return osk_rects if self.osk is not None else default_rects

    def enter_text_step(self, step: WizardStep) -> None:
        self.step = step
        initial = {
            WizardStep.SERVER: self.server,
            WizardStep.USERNAME: self.username,
            WizardStep.PASSWORD: self.password,
            WizardStep.REMOTE_LOCAL: self.remote_data_root or "/userdata/romcloud/remote",
            WizardStep.REMOTE_SERVER: self.remote_server,
            WizardStep.REMOTE_USERNAME: self.remote_username,
            WizardStep.REMOTE_PASSWORD: self.remote_password,
        }[step]
        self.osk = OskState(
            initial_text=initial,
            masked=step in (WizardStep.PASSWORD, WizardStep.REMOTE_PASSWORD),
        )
        self.selected_index = self.osk.selected_index
        self.error = ""

    def handle_event(
        self,
        event: InputEvent,
        rects: Sequence[Rect],
        romcloud_bin: str,
    ) -> None:
        if self.osk is not None:
            self._handle_osk(event, rects, romcloud_bin)
            return

        if event.touch_index is not None:
            self.select(event.touch_index)

        if event.action in ACTION_DIRECTIONS:
            dx, dy = ACTION_DIRECTIONS[event.action]
            self.select(find_next_focus_index(rects, self.selected_index, dx, dy))
        elif event.action == Action.CONFIRM:
            self._confirm(romcloud_bin)
        elif event.action == Action.BACK:
            self.back()

    def _handle_osk(self, event: InputEvent, rects: Sequence[Rect], romcloud_bin: str) -> None:
        assert self.osk is not None
        if event.action == Action.TEXT_INPUT and event.text:
            self.osk.insert_text(event.text)
            return
        if event.action == Action.TEXT_BACKSPACE:
            self.osk.backspace()
            return
        if event.action == Action.BACK:
            self._cancel_osk()
            return
        if event.touch_index is not None:
            self.osk.select(event.touch_index)
        if event.action in ACTION_DIRECTIONS:
            dx, dy = ACTION_DIRECTIONS[event.action]
            self.osk.select(find_next_focus_index(rects, self.osk.selected_index, dx, dy))
            return
        if event.action != Action.CONFIRM:
            return

        self.osk.activate(self.osk.selected_index)
        if self.osk.cancelled:
            self._cancel_osk()
        elif self.osk.confirmed:
            self._commit_osk(romcloud_bin)

    def _commit_osk(self, romcloud_bin: str) -> None:
        assert self.osk is not None
        value = (
            self.osk.text
            if self.step in (WizardStep.PASSWORD, WizardStep.REMOTE_PASSWORD)
            else self.osk.text.strip()
        )
        if not value:
            self.error = "A value is required."
            self.osk.confirmed = False
            return
        if self.cache_osk_field is not None:
            field = self.cache_osk_field
            try:
                if field == "cache_root":
                    if not value.startswith("/"):
                        raise ValueError("Cache location must be an absolute path.")
                    if value == self.rom_root or value.startswith(f"{self.rom_root.rstrip('/')}/"):
                        raise ValueError("Cache cannot be inside the ROM source.")
                    self.cache_root = value
                else:
                    number = float(value)
                    if number <= 0 and field == "max_size_gb":
                        raise ValueError("Maximum size must be greater than zero.")
                    if number < 0:
                        raise ValueError("Minimum free space cannot be negative.")
                    setattr(self, field, number)
            except ValueError as exc:
                self.error = str(exc)
                self.osk.confirmed = False
                return
            self.cache_osk_field = None
            self.osk = None
            return

        current = self.step
        if current == WizardStep.SERVER:
            self.server = value
            self.enter_text_step(WizardStep.USERNAME)
        elif current == WizardStep.USERNAME:
            self.username = value
            self.enter_text_step(WizardStep.PASSWORD)
        elif current == WizardStep.PASSWORD:
            self.password = value
            self.osk = None
            self._start_operation(WizardStep.DISCOVER, "setup-discover", romcloud_bin)
        elif current == WizardStep.REMOTE_LOCAL:
            if not value.startswith("/"):
                self.error = "ROMCloud data location must be an absolute path."
                self.osk.confirmed = False
                return
            self.remote_data_type = "local"
            self.remote_data_root = value
            self.osk = None
            self.step = WizardStep.CACHE
            self.selected_index = 0
        elif current == WizardStep.REMOTE_SERVER:
            self.remote_server = value
            self.enter_text_step(WizardStep.REMOTE_USERNAME)
        elif current == WizardStep.REMOTE_USERNAME:
            self.remote_username = value
            self.enter_text_step(WizardStep.REMOTE_PASSWORD)
        elif current == WizardStep.REMOTE_PASSWORD:
            self.remote_password = value
            self.remote_reuse_source_credentials = False
            self.osk = None
            self._start_operation(
                WizardStep.REMOTE_DISCOVER, "setup-discover", romcloud_bin
            )

    def _cancel_osk(self) -> None:
        if self.cache_osk_field is not None:
            self.cache_osk_field = None
            self.osk = None
            return
        previous = {
            WizardStep.SERVER: WizardStep.SOURCE,
            WizardStep.USERNAME: WizardStep.SERVER,
            WizardStep.PASSWORD: WizardStep.USERNAME,
            WizardStep.REMOTE_LOCAL: WizardStep.REMOTE_DATA,
            WizardStep.REMOTE_SERVER: WizardStep.REMOTE_AUTH,
            WizardStep.REMOTE_USERNAME: WizardStep.REMOTE_SERVER,
            WizardStep.REMOTE_PASSWORD: WizardStep.REMOTE_USERNAME,
        }[self.step]
        if previous in TEXT_STEPS:
            self.enter_text_step(previous)
        else:
            self.osk = None
            self.step = previous
            self.selected_index = 0

    def _confirm(self, romcloud_bin: str) -> None:
        self.error = ""
        if self.step == WizardStep.WELCOME:
            self.step = WizardStep.SOURCE
        elif self.step == WizardStep.SOURCE:
            if self.selected_index == 0:
                self.enter_text_step(WizardStep.SERVER)
            else:
                self.error = "Local and external sources are not available in this graphical setup yet."
        elif self.step == WizardStep.DISCOVER:
            self._start_operation(WizardStep.DISCOVER, "setup-discover", romcloud_bin)
        elif self.step == WizardStep.SHARE and self.shares:
            self.share = self.shares[self.selected_index]["name"]
            self._start_operation(WizardStep.DETECT, "setup-validate", romcloud_bin)
        elif self.step == WizardStep.DETECT:
            self._start_operation(WizardStep.DETECT, "setup-validate", romcloud_bin)
        elif self.step == WizardStep.SYSTEMS:
            self.step = WizardStep.REMOTE_DATA
            self.selected_index = 0
        elif self.step == WizardStep.REMOTE_DATA:
            if self.selected_index == 0:
                self.remote_data_type = "smb"
                self.step = WizardStep.REMOTE_AUTH
                self.selected_index = 0
            elif self.selected_index == 1:
                self.enter_text_step(WizardStep.REMOTE_LOCAL)
            else:
                self.remote_data_type = "none"
                self.step = WizardStep.CACHE
                self.selected_index = 0
        elif self.step == WizardStep.REMOTE_AUTH:
            if self.selected_index == 0:
                self.remote_reuse_source_credentials = True
                self.remote_server = self.server
                self.remote_username = self.username
                self.remote_password = ""
                self.remote_port = self.port
                self._start_operation(
                    WizardStep.REMOTE_DISCOVER, "setup-discover", romcloud_bin
                )
            else:
                self.remote_reuse_source_credentials = False
                self.remote_server = ""
                self.remote_username = ""
                self.remote_password = ""
                self.enter_text_step(WizardStep.REMOTE_SERVER)
        elif self.step == WizardStep.REMOTE_DISCOVER:
            self._start_operation(
                WizardStep.REMOTE_DISCOVER, "setup-discover", romcloud_bin
            )
        elif self.step == WizardStep.REMOTE_SHARE and self.remote_shares:
            self.remote_share = self.remote_shares[self.selected_index]["name"]
            self._start_operation(
                WizardStep.REMOTE_VALIDATE, "setup-validate", romcloud_bin
            )
        elif self.step == WizardStep.REMOTE_VALIDATE:
            self._start_operation(
                WizardStep.REMOTE_VALIDATE, "setup-validate", romcloud_bin
            )
        elif self.step == WizardStep.CACHE:
            if self.selected_index < len(CACHE_FIELDS):
                self.cache_osk_field = CACHE_FIELDS[self.selected_index]
                initial = str(getattr(self, self.cache_osk_field))
                self.osk = OskState(initial_text=initial)
            else:
                if self.min_free_gb < 0 or self.max_size_gb <= 0:
                    self.error = "Cache sizes are invalid."
                else:
                    self.step = WizardStep.REVIEW
                    self.selected_index = 0
        elif self.step == WizardStep.REVIEW:
            self._start_operation(WizardStep.APPLY, "setup-apply", romcloud_bin)
        elif self.step == WizardStep.APPLY:
            self._start_operation(WizardStep.APPLY, "setup-apply", romcloud_bin)
        elif self.step == WizardStep.DONE:
            self.finished = True

    def back(self) -> None:
        if self.runner is not None:
            self.runner.cancel()
            self.runner = None
        previous = {
            WizardStep.SOURCE: WizardStep.WELCOME,
            WizardStep.DISCOVER: WizardStep.PASSWORD,
            WizardStep.SHARE: WizardStep.PASSWORD,
            WizardStep.DETECT: WizardStep.SHARE,
            WizardStep.SYSTEMS: WizardStep.SHARE,
            WizardStep.REMOTE_DATA: WizardStep.SYSTEMS,
            WizardStep.REMOTE_AUTH: WizardStep.REMOTE_DATA,
            WizardStep.REMOTE_DISCOVER: (
                WizardStep.REMOTE_AUTH
                if self.remote_reuse_source_credentials
                else WizardStep.REMOTE_PASSWORD
            ),
            WizardStep.REMOTE_SHARE: (
                WizardStep.REMOTE_AUTH
                if self.remote_reuse_source_credentials
                else WizardStep.REMOTE_PASSWORD
            ),
            WizardStep.REMOTE_VALIDATE: WizardStep.REMOTE_SHARE,
            WizardStep.CACHE: WizardStep.REMOTE_DATA,
            WizardStep.REVIEW: WizardStep.CACHE,
            WizardStep.APPLY: WizardStep.REVIEW,
            WizardStep.DONE: WizardStep.REVIEW,
        }.get(self.step)
        if previous in TEXT_STEPS:
            self.enter_text_step(previous)
        elif previous is not None:
            self.step = previous
            self.selected_index = 0
            self.error = ""

    def select(self, index: int) -> None:
        count = max(1, len(self.options))
        self.selected_index = max(0, min(index, count - 1))

    def update_direction(self, action: Action, rects: Sequence[Rect]) -> None:
        if action not in ACTION_DIRECTIONS:
            return
        dx, dy = ACTION_DIRECTIONS[action]
        if self.osk is not None:
            self.osk.select(find_next_focus_index(rects, self.osk.selected_index, dx, dy))
        else:
            self.select(find_next_focus_index(rects, self.selected_index, dx, dy))

    def poll(self) -> None:
        if self.runner is None:
            return
        self.runner.poll()
        if not self.runner.is_finished:
            return
        result = operation_result(self.runner)
        self.runner = None
        if not result.ok:
            self.error = result.error
            return

        if self.step == WizardStep.DISCOVER:
            self.shares = [dict(item) for item in result.data.get("shares", [])]
            if not self.shares:
                self.error = "No accessible shares were found."
                return
            self.step = WizardStep.SHARE
            self.selected_index = 0
        elif self.step == WizardStep.DETECT:
            self.systems = [str(system) for system in result.data.get("systems", [])]
            self.source_validation = dict(result.data.get("validation", {}))
            self.step = WizardStep.SYSTEMS
            self.selected_index = 0
        elif self.step == WizardStep.REMOTE_DISCOVER:
            self.remote_shares = [dict(item) for item in result.data.get("shares", [])]
            if not self.remote_shares:
                self.error = "No accessible data shares were found."
                return
            self.step = WizardStep.REMOTE_SHARE
            self.selected_index = 0
        elif self.step == WizardStep.REMOTE_VALIDATE:
            self.remote_validation = dict(result.data.get("validation", {}))
            self.step = WizardStep.CACHE
            self.selected_index = 0
        elif self.step == WizardStep.APPLY:
            self.applied_summary = dict(result.data)
            self.password = ""
            self.remote_password = ""
            self.step = WizardStep.DONE
            self.selected_index = 0

    def request_payload(self) -> dict[str, Any]:
        return {
            "server": self.server,
            "share": self.share,
            "username": self.username,
            "password": self.password,
            "port": self.port,
            "rom_root": self.rom_root,
            "cache_root": self.cache_root,
            "max_size_gb": self.max_size_gb,
            "min_free_gb": self.min_free_gb,
            "remote_data_type": self.remote_data_type,
            "remote_data_root": self.remote_data_root,
            "remote_server": self.remote_server,
            "remote_share": self.remote_share,
            "remote_username": self.remote_username,
            "remote_password": self.remote_password,
            "remote_port": self.remote_port,
            "remote_reuse_source_credentials": self.remote_reuse_source_credentials,
            "purpose": (
                "remote_data"
                if self.step in (
                    WizardStep.REMOTE_DISCOVER,
                    WizardStep.REMOTE_SHARE,
                    WizardStep.REMOTE_VALIDATE,
                )
                else "source"
            ),
        }

    def _start_operation(self, step: WizardStep, action: str, romcloud_bin: str) -> None:
        self.step = step
        self.error = ""
        self.runner = start_backend_operation(romcloud_bin, action, self.request_payload())
