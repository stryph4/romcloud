"""Pure state and navigation for ROMCloud's graphical first-run wizard."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any, Sequence

from ports_gfx.actions import ACTION_DIRECTIONS, Action
from ports_gfx.activity import ActivityEvent, ActivityLog
from ports_gfx.client import BackendResult, operation_result, start_backend_operation
from ports_gfx.input_manager import InputEvent
from ports_gfx.layout import Rect
from ports_gfx.operation import OperationRunner
from ports_gfx.osk import OskState


class WizardStep(Enum):
    WELCOME = "welcome"
    SOURCE = "source"
    LOCAL_BROWSE = "local_browse"
    SERVER = "server"
    PORT = "port"
    USERNAME = "username"
    PASSWORD = "password"
    SFTP_TRUST = "sftp_trust"
    SFTP_PATH = "sftp_path"
    DISCOVER = "discover"
    SHARE = "share"
    SOURCE_BROWSE = "source_browse"
    DETECT = "detect"
    SYSTEMS = "systems"
    GAME_ACCESS = "game_access"
    REMOTE_DATA = "remote_data"
    REMOTE_LOCAL = "remote_local"
    REMOTE_AUTH = "remote_auth"
    REMOTE_SERVER = "remote_server"
    REMOTE_PORT = "remote_port"
    REMOTE_USERNAME = "remote_username"
    REMOTE_PASSWORD = "remote_password"
    REMOTE_SFTP_TRUST = "remote_sftp_trust"
    REMOTE_SFTP_PATH = "remote_sftp_path"
    REMOTE_DISCOVER = "remote_discover"
    REMOTE_SHARE = "remote_share"
    REMOTE_BROWSE = "remote_browse"
    REMOTE_VALIDATE = "remote_validate"
    LIBRARY_SYNC = "library_sync"
    CACHE = "cache"
    REVIEW = "review"
    APPLY = "apply"
    DONE = "done"


STEPS = tuple(WizardStep)
TEXT_STEPS = (
    WizardStep.SERVER,
    WizardStep.PORT,
    WizardStep.USERNAME,
    WizardStep.PASSWORD,
    WizardStep.SFTP_PATH,
    WizardStep.REMOTE_LOCAL,
    WizardStep.REMOTE_SERVER,
    WizardStep.REMOTE_PORT,
    WizardStep.REMOTE_USERNAME,
    WizardStep.REMOTE_PASSWORD,
    WizardStep.REMOTE_SFTP_PATH,
)
CACHE_FIELDS = ("cache_root", "cache_root_manual", "max_size_gb", "min_free_gb")


@dataclass(frozen=True)
class WizardStepContext:
    primary: str
    secondary: str


@dataclass(frozen=True)
class WizardProgressState:
    message: str
    status: str = "running"
    stage: str = ""
    current: int | None = None
    total: int | None = None

    @property
    def determinate(self) -> bool:
        return self.current is not None and self.total is not None and self.total > 0

    @property
    def fraction(self) -> float | None:
        if not self.determinate:
            return None
        assert self.current is not None and self.total is not None
        fraction = max(0.0, min(1.0, self.current / self.total))
        # A running phase must not look complete merely because its final item
        # was reported before the phase's terminal event reached the UI.
        if fraction >= 1.0 and not (
            self.status == "success"
            and self.stage in {"refresh_completed", "complete"}
        ):
            return 0.99
        return fraction

    @property
    def label(self) -> str:
        if not self.determinate:
            return self.message
        assert self.current is not None and self.total is not None
        return f"{self.message} — {self.current:,} / {self.total:,}"


STEP_CONTEXT: dict[WizardStep, WizardStepContext] = {
    WizardStep.WELCOME: WizardStepContext(
        "ROMCloud will guide you through your ROM library, game access, and shared data settings.",
        "You can review each choice before ROMCloud changes this device.",
    ),
    WizardStep.SOURCE: WizardStepContext(
        "Choose where your ROM collection is stored.",
        "ROMCloud can use a NAS or a supported local or external folder.",
    ),
    WizardStep.LOCAL_BROWSE: WizardStepContext(
        "Choose a folder on this device or attached storage.",
        "Open folders to browse, then select the folder you want ROMCloud to use.",
    ),
    WizardStep.SERVER: WizardStepContext(
        "Enter the network name or address of the server holding your ROMs.",
        "Next, ROMCloud will ask for the connection port and account.",
    ),
    WizardStep.PORT: WizardStepContext(
        "Enter the network port used by your ROM share.",
        "Most SMB servers use port 445.",
    ),
    WizardStep.USERNAME: WizardStepContext(
        "Enter an account that can read your ROM library.",
        "ROMCloud keeps ROM-library access read-only.",
    ),
    WizardStep.PASSWORD: WizardStepContext(
        "Enter the password for your ROM-library account.",
        "ROMCloud will connect and show the shares this account can access.",
    ),
    WizardStep.SFTP_TRUST: WizardStepContext(
        "Review the SFTP server host-key fingerprint before trusting it.",
        "Only trust a fingerprint you verified through your server or NAS administration tools.",
    ),
    WizardStep.SFTP_PATH: WizardStepContext(
        "Enter the absolute SFTP path containing your Batocera system folders.",
        "ROMCloud will validate read access and use Cached Storage for this source.",
    ),
    WizardStep.DISCOVER: WizardStepContext(
        "ROMCloud is connecting to your ROM server.",
        "It will authenticate and find the shares available to your account.",
    ),
    WizardStep.SHARE: WizardStepContext(
        "Choose the share that contains your ROM collection.",
        "Next, you can select the specific ROM folder inside it.",
    ),
    WizardStep.SOURCE_BROWSE: WizardStepContext(
        "Choose the folder that contains your Batocera system folders.",
        "ROMCloud reads this library without modifying your source games.",
    ),
    WizardStep.DETECT: WizardStepContext(
        "ROMCloud is testing access to the selected ROM folder.",
        "It will identify the Batocera systems available there.",
    ),
    WizardStep.SYSTEMS: WizardStepContext(
        "Review the systems ROMCloud found in your library.",
        "Continue to choose how games should be opened on this device.",
    ),
    WizardStep.GAME_ACCESS: WizardStepContext(
        "Choose ROMCloud's initial operating mode.",
        "Cached Storage copies games into managed local storage as needed; Direct launches from the configured source.",
    ),
    WizardStep.REMOTE_DATA: WizardStepContext(
        "Choose where ROMCloud should store shared data such as synchronized saves.",
        "This location must be writable and separate from the read-only ROM share.",
    ),
    WizardStep.REMOTE_LOCAL: WizardStepContext(
        "Enter a writable local or external folder for shared ROMCloud data.",
        "Next, choose whether to enable shared library metadata.",
    ),
    WizardStep.REMOTE_AUTH: WizardStepContext(
        "Choose how ROMCloud should connect to the writable data share.",
        "It may use the same account prompts or a different server and account.",
    ),
    WizardStep.REMOTE_SERVER: WizardStepContext(
        "Enter the server that will hold shared ROMCloud data.",
        "This may be the ROM server or another supported server.",
    ),
    WizardStep.REMOTE_PORT: WizardStepContext(
        "Enter the network port used by the writable data share.",
        "Most SMB servers use port 445.",
    ),
    WizardStep.REMOTE_USERNAME: WizardStepContext(
        "Enter an account that can write shared ROMCloud data.",
        "ROMCloud will verify write and cleanup access before setup completes.",
    ),
    WizardStep.REMOTE_PASSWORD: WizardStepContext(
        "Enter the password for the writable-data account.",
        "ROMCloud will connect and show the shares this account can access.",
    ),
    WizardStep.REMOTE_SFTP_TRUST: WizardStepContext(
        "Review the remote-data SFTP server host-key fingerprint before trusting it.",
        "Read-only remote data remains usable for safe downloads; publishing depends on its validated capabilities.",
    ),
    WizardStep.REMOTE_SFTP_PATH: WizardStepContext(
        "Enter the absolute SFTP path for ROMCloud shared data.",
        "ROMCloud will verify read access and report any write limitations during setup.",
    ),
    WizardStep.REMOTE_DISCOVER: WizardStepContext(
        "ROMCloud is connecting to the shared-data server.",
        "It will authenticate and find the shares available to your account.",
    ),
    WizardStep.REMOTE_SHARE: WizardStepContext(
        "Choose the share where ROMCloud should keep synchronized data.",
        "Next, you can choose a folder inside that share.",
    ),
    WizardStep.REMOTE_BROWSE: WizardStepContext(
        "Choose the folder for shared ROMCloud data.",
        "ROMCloud will test this location before saving the configuration.",
    ),
    WizardStep.REMOTE_VALIDATE: WizardStepContext(
        "ROMCloud is checking access to the shared-data folder.",
        "A full write and cleanup test will run when setup is applied.",
    ),
    WizardStep.LIBRARY_SYNC: WizardStepContext(
        "Choose whether ROMCloud should share game descriptions and media between devices.",
        "Setup only enables it; import starts later from Library after a preview and long press.",
    ),
    WizardStep.CACHE: WizardStepContext(
        "Choose how much local space ROMCloud may use for games you play.",
        "Cached and pinned games can launch faster and remain available in Offline.",
    ),
    WizardStep.REVIEW: WizardStepContext(
        "Review your choices before ROMCloud configures this device.",
        "Continue to test storage, scan your games, and prepare EmulationStation.",
    ),
    WizardStep.APPLY: WizardStepContext(
        "ROMCloud is configuring and testing this device.",
        "Writable shared storage is initialized with a Full Sync before Auto SaveSync is ready.",
    ),
    WizardStep.DONE: WizardStepContext(
        "ROMCloud setup is complete.",
        "Return to EmulationStation and refresh its game list to show ROMCloud games.",
    ),
}


_RUNNING_MESSAGES = {
    WizardStep.LOCAL_BROWSE: "Opening the selected folder…",
    WizardStep.DISCOVER: "Connecting to your ROM library…",
    WizardStep.SOURCE_BROWSE: "Opening the ROM share…",
    WizardStep.DETECT: "Testing access to your ROM library…",
    WizardStep.REMOTE_DISCOVER: "Connecting to shared data storage…",
    WizardStep.REMOTE_BROWSE: "Opening the shared-data folder…",
    WizardStep.REMOTE_VALIDATE: "Testing access to shared data storage…",
    WizardStep.APPLY: "Saving configuration and initializing SaveSync…",
}


_FAILURE_MESSAGES = {
    WizardStep.LOCAL_BROWSE: "Could not open that folder. Check that it is available, then retry.",
    WizardStep.DISCOVER: "Could not connect. Check the ROM server and account, then retry.",
    WizardStep.SOURCE_BROWSE: "Could not open the ROM share. Check access, then retry.",
    WizardStep.DETECT: "Could not read that ROM folder. Check the folder and permissions, then retry.",
    WizardStep.REMOTE_DISCOVER: "Could not connect. Check the shared-data server and account, then retry.",
    WizardStep.REMOTE_BROWSE: "Could not open the shared-data share. Check access, then retry.",
    WizardStep.REMOTE_VALIDATE: "Could not verify that data folder. Check access, then retry.",
    WizardStep.APPLY: "Setup could not finish. Your earlier settings were preserved when possible; review details and retry.",
}


class WizardState:
    """Device-agnostic setup state. Secrets live only in this process."""

    def __init__(self, status: BackendResult | None = None) -> None:
        data = status.data if status and status.ok else {}
        self.mode = str(data.get("state", "fresh"))
        self.source_type = str(data.get("source_type", "smb") or "smb")
        self.step = WizardStep.WELCOME
        self.selected_index = 0
        self.server = str(data.get("server", ""))
        self.username = str(data.get("username", ""))
        self.password = ""
        self.port = int(data.get("port", 445))
        self.share = str(data.get("share", ""))
        self.source_remote_path = str(data.get("source_remote_path", ""))
        self.sftp_host_key_fingerprint = ""
        self.sftp_host_key_type = ""
        self.rom_root = str(data.get("rom_root", "/userdata/romcloud/source"))
        self.shares: list[dict[str, str]] = []
        self.systems: list[str] = []
        self.game_access_mode = str(data.get("game_access_mode", "smart_cache"))
        self.remote_data_type = str(data.get("remote_data_type", "none"))
        self.remote_data_root = str(data.get("remote_data_root", ""))
        self.remote_server = str(data.get("remote_server", ""))
        self.remote_username = str(data.get("remote_username", ""))
        self.remote_password = ""
        self.remote_port = int(data.get("remote_port", 445))
        self.remote_reuse_source_credentials = False
        self.remote_share = str(data.get("remote_share", ""))
        self.remote_remote_path = str(data.get("remote_remote_path", ""))
        self.remote_sftp_host_key_fingerprint = ""
        self.remote_sftp_host_key_type = ""
        self.remote_shares: list[dict[str, str]] = []
        self.browser_path = ""
        self.browser_entries: list[dict[str, Any]] = []
        self.local_browse_purpose = ""
        self.cache_root = str(data.get("cache_root", "/userdata/romcloud/cache"))
        self.max_size_gb = float(data.get("max_size_gb", 50.0))
        self.min_free_gb = float(data.get("min_free_gb", 5.0))
        self.issues = [str(issue) for issue in data.get("issues", [])]
        self.error = status.error if status and not status.ok else ""
        self.osk: OskState | None = None
        self.osk_visible = False
        self._osk_restore_index = 0
        self.cache_osk_field: str | None = None
        self.runner: OperationRunner | None = None
        self.finished = False
        self.applied_summary: dict[str, Any] = {}
        self.source_validation: dict[str, Any] = {}
        self.remote_validation: dict[str, Any] = {}
        self.library_sync_enabled = bool(data.get("library_sync_enabled", False))
        self.activity = ActivityLog()
        self.show_details = False
        self.notice = ""
        self.technical_error = ""
        self._progress_event: ActivityEvent | None = None

    @property
    def step_number(self) -> int:
        return STEPS.index(self.step) + 1

    @property
    def title(self) -> str:
        titles = {
            WizardStep.WELCOME: (
                "Repair ROMCloud"
                if self.mode == "partial"
                else "Storage Setup"
                if self.mode == "configured"
                else "Welcome to ROMCloud"
            ),
            WizardStep.SOURCE: "Choose Source Type",
            WizardStep.LOCAL_BROWSE: "Select a Local Folder",
            WizardStep.SERVER: "SFTP Server" if self.source_type == "sftp" else "SMB Server",
            WizardStep.PORT: "SFTP Port" if self.source_type == "sftp" else "SMB Port",
            WizardStep.USERNAME: "SFTP Username" if self.source_type == "sftp" else "SMB Username",
            WizardStep.PASSWORD: "SFTP Password" if self.source_type == "sftp" else "SMB Password",
            WizardStep.SFTP_TRUST: "Trust SFTP Host Key",
            WizardStep.SFTP_PATH: "SFTP ROM Folder",
            WizardStep.DISCOVER: "Discover Shares",
            WizardStep.SHARE: "Select Share",
            WizardStep.SOURCE_BROWSE: "Choose the ROM Folder",
            WizardStep.DETECT: "Detect Systems",
            WizardStep.SYSTEMS: "Detected Systems",
            WizardStep.GAME_ACCESS: "Choose Game Access",
            WizardStep.REMOTE_DATA: "ROMCloud Data Storage",
            WizardStep.REMOTE_LOCAL: "Local Data Directory",
            WizardStep.REMOTE_AUTH: "Data SMB Credentials",
            WizardStep.REMOTE_SERVER: "Data SFTP Server" if self.remote_data_type == "sftp" else "Data SMB Server",
            WizardStep.REMOTE_PORT: "Data SFTP Port" if self.remote_data_type == "sftp" else "Data SMB Port",
            WizardStep.REMOTE_USERNAME: "Data SFTP Username" if self.remote_data_type == "sftp" else "Data SMB Username",
            WizardStep.REMOTE_PASSWORD: "Data SFTP Password" if self.remote_data_type == "sftp" else "Data SMB Password",
            WizardStep.REMOTE_SFTP_TRUST: "Trust Data SFTP Host Key",
            WizardStep.REMOTE_SFTP_PATH: "SFTP Data Folder",
            WizardStep.REMOTE_DISCOVER: "Discover Data Shares",
            WizardStep.REMOTE_SHARE: "Select Data Share",
            WizardStep.REMOTE_BROWSE: "Choose the Data Folder",
            WizardStep.REMOTE_VALIDATE: "Validate Data Share",
            WizardStep.LIBRARY_SYNC: "Library Sync",
            WizardStep.CACHE: "Cache Settings",
            WizardStep.REVIEW: "Review Setup",
            WizardStep.APPLY: "Configure ROMCloud",
            WizardStep.DONE: "Setup Complete",
        }
        return titles[self.step]

    @property
    def context_lines(self) -> tuple[str, str]:
        context = STEP_CONTEXT[self.step]
        if self.step == WizardStep.LOCAL_BROWSE:
            context = {
                "source": WizardStepContext(
                    "Choose the local folder that contains your Batocera system folders.",
                    "ROMCloud reads your games in place and does not move the source files.",
                ),
                "remote_data": WizardStepContext(
                    "Choose a writable folder for synchronized saves and other shared data.",
                    "This folder stays separate from your ROM library and game cache.",
                ),
                "cache": WizardStepContext(
                    "Choose where ROMCloud should keep local game copies.",
                    "Next, you can set the maximum cache size and reserved free space.",
                ),
            }.get(self.local_browse_purpose, context)
        return context.primary, context.secondary

    @property
    def progress(self) -> WizardProgressState | None:
        if self.runner is None:
            return None
        event = self._progress_event
        if event is None:
            return WizardProgressState(
                _RUNNING_MESSAGES.get(self.step, "ROMCloud is working…")
            )
        return WizardProgressState(
            event.message,
            status=event.status,
            stage=event.stage,
            current=event.current,
            total=event.total,
        )

    @property
    def options(self) -> list[str]:
        if self.step == WizardStep.WELCOME:
            return [
                "Resume / Repair Setup"
                if self.mode == "partial"
                else "Review / Change Setup"
                if self.mode == "configured"
                else "Start Setup"
            ]
        if self.step == WizardStep.SOURCE:
            return ["SMB network share", "Local / external directory", "SFTP server"]
        if self.step in (WizardStep.SFTP_TRUST, WizardStep.REMOTE_SFTP_TRUST):
            return ["Trust this host key"]
        if self.step == WizardStep.SHARE:
            return [share["name"] for share in self.shares]
        if self.step == WizardStep.REMOTE_DATA:
            return [
                "SMB network location",
                "Local / external directory",
                "SFTP server",
                "Skip (sync features unavailable)",
            ]
        if self.step == WizardStep.GAME_ACCESS:
            return ["Cached Storage"] if self.source_type == "sftp" else ["Cached Storage", "Direct"]
        if self.step == WizardStep.LIBRARY_SYNC:
            return ["Enable Library Sync", "Keep Library Sync disabled"]
        if self.step == WizardStep.REMOTE_AUTH:
            return [
                "Use same server and credentials",
                "Use different server or credentials",
            ]
        if self.step == WizardStep.REMOTE_SHARE:
            return [share["name"] for share in self.remote_shares]
        if self.step in (
            WizardStep.SOURCE_BROWSE,
            WizardStep.REMOTE_BROWSE,
            WizardStep.LOCAL_BROWSE,
        ) and self.runner is None:
            directories = [
                entry["name"]
                for entry in self.browser_entries
                if entry.get("is_directory")
            ]
            return ["Select this folder", "Up one folder", *[f"Folder: {name}" for name in directories]]
        if self.step == WizardStep.CACHE and self.osk is None:
            return [
                f"Browse location: {self.cache_root}",
                "Enter location manually",
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
        return osk_rects if self.osk is not None and self.osk_visible else default_rects

    def enter_text_step(self, step: WizardStep, *, show_osk: bool = True) -> None:
        self._osk_restore_index = self.selected_index
        self.step = step
        initial = {
            WizardStep.SERVER: self.server,
            WizardStep.PORT: str(self.port),
            WizardStep.USERNAME: self.username,
            WizardStep.PASSWORD: self.password,
            WizardStep.SFTP_PATH: self.source_remote_path or "/",
            WizardStep.REMOTE_LOCAL: self.remote_data_root or "/userdata/romcloud/remote",
            WizardStep.REMOTE_SERVER: self.remote_server,
            WizardStep.REMOTE_PORT: str(self.remote_port),
            WizardStep.REMOTE_USERNAME: self.remote_username,
            WizardStep.REMOTE_PASSWORD: self.remote_password,
            WizardStep.REMOTE_SFTP_PATH: self.remote_data_root or "/",
        }[step]
        self.osk = OskState(
            initial_text=initial,
            masked=step in (WizardStep.PASSWORD, WizardStep.REMOTE_PASSWORD),
        )
        self.osk_visible = show_osk
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
            step = dy if dy else dx
            count = len(self.options)
            if count:
                self.selected_index = (self.selected_index + step) % count
        elif event.action == Action.CONFIRM:
            self._confirm(
                romcloud_bin,
                show_osk=event.source not in ("mouse", "keyboard"),
            )
        elif event.action == Action.BACK:
            self.back()
        elif event.action == Action.MENU:
            self.show_details = not self.show_details

    def _handle_osk(self, event: InputEvent, rects: Sequence[Rect], romcloud_bin: str) -> None:
        assert self.osk is not None
        if event.action == Action.TEXT_INPUT and event.text:
            self.osk.insert_text(event.text)
            return
        if event.action == Action.TEXT_BACKSPACE:
            self.osk.backspace()
            return
        if event.action == Action.BACK:
            if self.osk_visible and event.source is not None:
                self.osk_visible = False
                self.selected_index = self._osk_restore_index
                return
            self._cancel_osk()
            return
        if event.action == Action.MENU:
            self.osk_visible = not self.osk_visible
            return
        if event.touch_index is not None:
            self.osk.select(event.touch_index)
        if event.action in ACTION_DIRECTIONS:
            dx, dy = ACTION_DIRECTIONS[event.action]
            if self.osk_visible:
                self.osk.move(dx, dy)
            return
        if event.action != Action.CONFIRM:
            return

        if not self.osk_visible:
            self._commit_osk(romcloud_bin)
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
                if field in ("cache_root", "cache_root_manual"):
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
            self.osk_visible = False
            return

        current = self.step
        keep_osk_visible = self.osk_visible
        if current == WizardStep.SERVER:
            self.server = value
            self.enter_text_step(WizardStep.PORT, show_osk=keep_osk_visible)
        elif current == WizardStep.PORT:
            try:
                port = int(value)
            except ValueError:
                self.error = "Port must be a number between 1 and 65535."
                self.osk.confirmed = False
                return
            if not 1 <= port <= 65535:
                self.error = "Port must be between 1 and 65535."
                self.osk.confirmed = False
                return
            self.port = port
            self.enter_text_step(WizardStep.USERNAME, show_osk=keep_osk_visible)
        elif current == WizardStep.USERNAME:
            self.username = value
            self.enter_text_step(WizardStep.PASSWORD, show_osk=keep_osk_visible)
        elif current == WizardStep.PASSWORD:
            self.password = value
            self.osk = None
            self.osk_visible = False
            if self.source_type == "sftp":
                self._start_operation(WizardStep.SFTP_TRUST, "setup-sftp-host-key", romcloud_bin)
            else:
                self._start_operation(WizardStep.DISCOVER, "setup-discover", romcloud_bin)
        elif current == WizardStep.SFTP_PATH:
            if not value.startswith("/"):
                self.error = "SFTP path must be absolute."
                self.osk.confirmed = False
                return
            self.source_remote_path = value
            self.osk = None
            self.osk_visible = False
            self._start_operation(WizardStep.DETECT, "setup-validate", romcloud_bin)
        elif current == WizardStep.REMOTE_LOCAL:
            if not value.startswith("/"):
                self.error = "ROMCloud data location must be an absolute path."
                self.osk.confirmed = False
                return
            self.remote_data_type = "local"
            self.remote_data_root = value
            self.osk = None
            self.osk_visible = False
            self.step = self._post_storage_step()
            self.selected_index = 0
        elif current == WizardStep.REMOTE_SERVER:
            self.remote_server = value
            self.enter_text_step(WizardStep.REMOTE_PORT, show_osk=keep_osk_visible)
        elif current == WizardStep.REMOTE_PORT:
            try:
                port = int(value)
            except ValueError:
                self.error = "Port must be a number between 1 and 65535."
                self.osk.confirmed = False
                return
            if not 1 <= port <= 65535:
                self.error = "Port must be between 1 and 65535."
                self.osk.confirmed = False
                return
            self.remote_port = port
            self.enter_text_step(WizardStep.REMOTE_USERNAME, show_osk=keep_osk_visible)
        elif current == WizardStep.REMOTE_USERNAME:
            self.remote_username = value
            self.enter_text_step(WizardStep.REMOTE_PASSWORD, show_osk=keep_osk_visible)
        elif current == WizardStep.REMOTE_PASSWORD:
            self.remote_password = value
            self.remote_reuse_source_credentials = False
            self.osk = None
            self.osk_visible = False
            if self.remote_data_type == "sftp":
                self._start_operation(
                    WizardStep.REMOTE_SFTP_TRUST, "setup-sftp-host-key", romcloud_bin
                )
            else:
                self._start_operation(
                    WizardStep.REMOTE_DISCOVER, "setup-discover", romcloud_bin
                )
        elif current == WizardStep.REMOTE_SFTP_PATH:
            if not value.startswith("/"):
                self.error = "SFTP path must be absolute."
                self.osk.confirmed = False
                return
            self.remote_data_root = value
            self.osk = None
            self.osk_visible = False
            self._start_operation(WizardStep.REMOTE_VALIDATE, "setup-validate", romcloud_bin)

    def _cancel_osk(self) -> None:
        if self.cache_osk_field is not None:
            self.cache_osk_field = None
            self.osk = None
            self.osk_visible = False
            self.selected_index = self._osk_restore_index
            return
        previous = {
            WizardStep.SERVER: WizardStep.SOURCE,
            WizardStep.PORT: WizardStep.SERVER,
            WizardStep.USERNAME: WizardStep.PORT,
            WizardStep.PASSWORD: WizardStep.USERNAME,
            WizardStep.SFTP_PATH: WizardStep.SFTP_TRUST,
            WizardStep.REMOTE_LOCAL: WizardStep.REMOTE_DATA,
            WizardStep.REMOTE_SERVER: WizardStep.REMOTE_AUTH,
            WizardStep.REMOTE_PORT: WizardStep.REMOTE_SERVER,
            WizardStep.REMOTE_USERNAME: WizardStep.REMOTE_PORT,
            WizardStep.REMOTE_PASSWORD: WizardStep.REMOTE_USERNAME,
            WizardStep.REMOTE_SFTP_PATH: WizardStep.REMOTE_SFTP_TRUST,
        }[self.step]
        if previous in TEXT_STEPS:
            self.enter_text_step(previous)
        else:
            self.osk = None
            self.osk_visible = False
            self.step = previous
            self.selected_index = self._osk_restore_index

    def _confirm(self, romcloud_bin: str, *, show_osk: bool = True) -> None:
        self.error = ""
        self.technical_error = ""
        self.notice = ""
        if self.step == WizardStep.WELCOME:
            self.step = WizardStep.SOURCE
        elif self.step == WizardStep.SOURCE:
            if self.selected_index == 0:
                self.source_type = "smb"
                self.enter_text_step(WizardStep.SERVER, show_osk=show_osk)
            elif self.selected_index == 1:
                self.source_type = "local"
                self._start_local_browse("source", self.rom_root if self.mode != "fresh" else "/userdata", romcloud_bin)
            else:
                self.source_type = "sftp"
                self.server = ""
                self.port = 22
                self.username = ""
                self.password = ""
                self.source_remote_path = "/"
                self.enter_text_step(WizardStep.SERVER, show_osk=show_osk)
        elif self.step == WizardStep.SFTP_TRUST:
            self.enter_text_step(WizardStep.SFTP_PATH, show_osk=show_osk)
        elif self.step == WizardStep.DISCOVER:
            self._start_operation(WizardStep.DISCOVER, "setup-discover", romcloud_bin)
        elif self.step == WizardStep.SHARE and self.shares:
            self.share = self.shares[self.selected_index]["name"]
            self.source_remote_path = ""
            self._start_operation(WizardStep.SOURCE_BROWSE, "setup-browse-smb", romcloud_bin)
        elif self.step in (WizardStep.SOURCE_BROWSE, WizardStep.REMOTE_BROWSE, WizardStep.LOCAL_BROWSE):
            self._confirm_browser(romcloud_bin)
        elif self.step == WizardStep.DETECT:
            self._start_operation(WizardStep.DETECT, "setup-validate", romcloud_bin)
        elif self.step == WizardStep.SYSTEMS:
            self.step = WizardStep.GAME_ACCESS
            self.selected_index = 0 if self.game_access_mode == "smart_cache" else 1
        elif self.step == WizardStep.GAME_ACCESS:
            self.game_access_mode = "smart_cache" if self.selected_index == 0 else "direct_nas"
            self.step = WizardStep.REMOTE_DATA
            self.selected_index = 0
        elif self.step == WizardStep.REMOTE_DATA:
            if self.selected_index == 0:
                self.remote_data_type = "smb"
                self.step = WizardStep.REMOTE_AUTH
                self.selected_index = 0
            elif self.selected_index == 1:
                self._start_local_browse("remote_data", self.remote_data_root or "/userdata", romcloud_bin)
            elif self.selected_index == 2:
                self.remote_data_type = "sftp"
                self.remote_server = ""
                self.remote_port = 22
                self.remote_username = ""
                self.remote_password = ""
                self.remote_data_root = "/"
                self.remote_reuse_source_credentials = False
                self.enter_text_step(WizardStep.REMOTE_SERVER, show_osk=show_osk)
            else:
                self.remote_data_type = "none"
                self.library_sync_enabled = False
                self.step = (
                    WizardStep.CACHE
                    if self.game_access_mode == "smart_cache"
                    else WizardStep.REVIEW
                )
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
                self.enter_text_step(WizardStep.REMOTE_SERVER, show_osk=show_osk)
        elif self.step == WizardStep.REMOTE_DISCOVER:
            self._start_operation(
                WizardStep.REMOTE_DISCOVER, "setup-discover", romcloud_bin
            )
        elif self.step == WizardStep.REMOTE_SHARE and self.remote_shares:
            self.remote_share = self.remote_shares[self.selected_index]["name"]
            self.remote_remote_path = ""
            self._start_operation(
                WizardStep.REMOTE_BROWSE, "setup-browse-smb", romcloud_bin
            )
        elif self.step == WizardStep.REMOTE_VALIDATE:
            self._start_operation(
                WizardStep.REMOTE_VALIDATE, "setup-validate", romcloud_bin
            )
        elif self.step == WizardStep.LIBRARY_SYNC:
            self.library_sync_enabled = self.selected_index == 0
            self.step = (
                WizardStep.CACHE
                if self.game_access_mode == "smart_cache"
                else WizardStep.REVIEW
            )
            self.selected_index = 0
        elif self.step == WizardStep.CACHE:
            if self.selected_index == 0:
                self._start_local_browse("cache", self.cache_root, romcloud_bin)
            elif self.selected_index < len(CACHE_FIELDS):
                self.cache_osk_field = CACHE_FIELDS[self.selected_index]
                initial = str(
                    self.cache_root
                    if self.cache_osk_field == "cache_root_manual"
                    else getattr(self, self.cache_osk_field)
                )
                self.osk = OskState(initial_text=initial)
                self.osk_visible = show_osk
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
            WizardStep.LOCAL_BROWSE: (
                WizardStep.SOURCE
                if self.local_browse_purpose == "source"
                else WizardStep.REMOTE_DATA
                if self.local_browse_purpose == "remote_data"
                else WizardStep.CACHE
            ),
            WizardStep.DISCOVER: WizardStep.PASSWORD,
            WizardStep.SFTP_TRUST: WizardStep.PASSWORD,
            WizardStep.SFTP_PATH: WizardStep.SFTP_TRUST,
            WizardStep.SHARE: WizardStep.PASSWORD,
            WizardStep.SOURCE_BROWSE: WizardStep.SHARE,
            WizardStep.DETECT: (
                WizardStep.SFTP_PATH if self.source_type == "sftp" else WizardStep.SHARE
            ),
            WizardStep.SYSTEMS: (
                WizardStep.SFTP_PATH if self.source_type == "sftp" else WizardStep.SHARE
            ),
            WizardStep.GAME_ACCESS: WizardStep.SYSTEMS,
            WizardStep.REMOTE_DATA: WizardStep.GAME_ACCESS,
            WizardStep.REMOTE_AUTH: WizardStep.REMOTE_DATA,
            WizardStep.REMOTE_DISCOVER: (
                WizardStep.REMOTE_AUTH
                if self.remote_reuse_source_credentials
                else WizardStep.REMOTE_PASSWORD
            ),
            WizardStep.REMOTE_SFTP_TRUST: WizardStep.REMOTE_PASSWORD,
            WizardStep.REMOTE_SFTP_PATH: WizardStep.REMOTE_SFTP_TRUST,
            WizardStep.REMOTE_SHARE: (
                WizardStep.REMOTE_AUTH
                if self.remote_reuse_source_credentials
                else WizardStep.REMOTE_PASSWORD
            ),
            WizardStep.REMOTE_BROWSE: WizardStep.REMOTE_SHARE,
            WizardStep.REMOTE_VALIDATE: (
                WizardStep.REMOTE_SFTP_PATH
                if self.remote_data_type == "sftp"
                else WizardStep.REMOTE_SHARE
            ),
            WizardStep.LIBRARY_SYNC: WizardStep.REMOTE_DATA,
            WizardStep.CACHE: (
                WizardStep.LIBRARY_SYNC
                if self.remote_data_type != "none"
                else WizardStep.REMOTE_DATA
            ),
            WizardStep.REVIEW: (
                WizardStep.CACHE
                if self.game_access_mode == "smart_cache"
                else WizardStep.LIBRARY_SYNC
                if self.remote_data_type != "none"
                else WizardStep.REMOTE_DATA
            ),
            WizardStep.APPLY: WizardStep.REVIEW,
            WizardStep.DONE: WizardStep.REVIEW,
        }.get(self.step)
        if previous in TEXT_STEPS:
            self.enter_text_step(previous)
        elif previous is not None:
            self.step = previous
            self.selected_index = 0
            self.error = ""
            self.technical_error = ""
            self.notice = ""

    def cancel_pending(self) -> None:
        if self.runner is not None:
            self.runner.cancel()
            self.runner = None

    def select(self, index: int) -> None:
        count = max(1, len(self.options))
        self.selected_index = max(0, min(index, count - 1))

    def update_direction(self, action: Action, rects: Sequence[Rect]) -> None:
        if action not in ACTION_DIRECTIONS:
            return
        dx, dy = ACTION_DIRECTIONS[action]
        if self.osk is not None:
            if self.osk_visible:
                self.osk.move(dx, dy)
        else:
            step = dy if dy else dx
            count = len(self.options)
            if count:
                self.selected_index = (self.selected_index + step) % count

    def poll(self) -> list:
        if self.runner is None:
            return []
        drained = self.runner.poll()
        for line in drained:
            event = self.activity.ingest(line.text)
            if event is not None:
                self._progress_event = event
        if not self.runner.is_finished:
            return drained
        result = operation_result(self.runner)
        self.runner = None
        if not result.ok:
            self.technical_error = result.error
            self.error = _FAILURE_MESSAGES.get(
                self.step,
                "ROMCloud could not complete this step. Review details and retry.",
            )
            if self._progress_event is None or self._progress_event.status != "error":
                self.activity.append(
                    ActivityEvent(
                        datetime.now().strftime("%H:%M:%S"),
                        "setup",
                        self.step.value,
                        "error",
                        self.error,
                        detail=self.technical_error,
                    )
                )
            return drained

        if self.step == WizardStep.DISCOVER:
            self.shares = [dict(item) for item in result.data.get("shares", [])]
            if not self.shares:
                self.error = "No accessible shares were found."
                return drained
            self.step = WizardStep.SHARE
            self.selected_index = 0
            count = len(self.shares)
            self.notice = (
                f"Connection successful — {count:,} "
                f"share{'s' if count != 1 else ''} found."
            )
        elif self.step == WizardStep.SFTP_TRUST:
            self.sftp_host_key_type = str(result.data.get("host_key_type", ""))
            self.sftp_host_key_fingerprint = str(result.data.get("host_key_fingerprint", ""))
            if not self.sftp_host_key_fingerprint:
                self.error = "The server did not provide a host-key fingerprint."
                return drained
            self.selected_index = 0
            self.notice = "Verify the fingerprint, then choose Trust this host key."
        elif self.step == WizardStep.DETECT:
            self.systems = [str(system) for system in result.data.get("systems", [])]
            self.source_validation = dict(result.data.get("validation", {}))
            self.step = WizardStep.SYSTEMS
            self.selected_index = 0
            count = len(self.systems)
            self.notice = (
                f"Library check complete — {count:,} "
                f"system{'s' if count != 1 else ''} found."
            )
        elif self.step in (
            WizardStep.SOURCE_BROWSE,
            WizardStep.REMOTE_BROWSE,
            WizardStep.LOCAL_BROWSE,
        ):
            self.browser_path = str(result.data.get("path", ""))
            self.browser_entries = [
                dict(entry) for entry in result.data.get("entries", [])
            ]
            self.selected_index = 0
            self.notice = "Folder loaded. Choose this folder or open another folder."
        elif self.step == WizardStep.REMOTE_DISCOVER:
            self.remote_shares = [dict(item) for item in result.data.get("shares", [])]
            if not self.remote_shares:
                self.error = "No accessible data shares were found."
                return drained
            self.step = WizardStep.REMOTE_SHARE
            self.selected_index = 0
            count = len(self.remote_shares)
            self.notice = (
                f"Connection successful — {count:,} "
                f"share{'s' if count != 1 else ''} found."
            )
        elif self.step == WizardStep.REMOTE_SFTP_TRUST:
            self.remote_sftp_host_key_type = str(result.data.get("host_key_type", ""))
            self.remote_sftp_host_key_fingerprint = str(result.data.get("host_key_fingerprint", ""))
            if not self.remote_sftp_host_key_fingerprint:
                self.error = "The server did not provide a host-key fingerprint."
                return drained
            self.selected_index = 0
            self.notice = "Verify the fingerprint, then choose Trust this host key."
        elif self.step == WizardStep.REMOTE_VALIDATE:
            self.remote_validation = dict(result.data.get("validation", {}))
            self.step = self._post_storage_step()
            self.selected_index = 0
            self.notice = "Shared-data folder is accessible. A write test will run when setup is applied."
        elif self.step == WizardStep.APPLY:
            self.applied_summary = dict(result.data)
            self.password = ""
            self.remote_password = ""
            self.step = WizardStep.DONE
            self.selected_index = 0
            self.notice = "ROMCloud setup is complete."
        return drained

    def request_payload(self) -> dict[str, Any]:
        return {
            "progress": True,
            "source_type": self.source_type,
            "game_access_mode": self.game_access_mode,
            "server": self.server,
            "share": self.share,
            "source_remote_path": self.source_remote_path,
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
            "remote_remote_path": self.remote_remote_path,
            "sftp_host_key_fingerprint": self.sftp_host_key_fingerprint,
            "remote_sftp_host_key_fingerprint": self.remote_sftp_host_key_fingerprint,
            "remote_username": self.remote_username,
            "remote_password": self.remote_password,
            "remote_port": self.remote_port,
            "remote_reuse_source_credentials": self.remote_reuse_source_credentials,
            "library_sync_enabled": self.library_sync_enabled,
            "purpose": (
                "remote_data"
                if self.step in (
                    WizardStep.REMOTE_DISCOVER,
                    WizardStep.REMOTE_SHARE,
                    WizardStep.REMOTE_BROWSE,
                    WizardStep.REMOTE_VALIDATE,
                    WizardStep.REMOTE_SFTP_TRUST,
                    WizardStep.REMOTE_SFTP_PATH,
                )
                else "source"
            ),
        }

    def _start_operation(self, step: WizardStep, action: str, romcloud_bin: str) -> None:
        self.step = step
        self.error = ""
        self.technical_error = ""
        self.notice = ""
        self._progress_event = None
        self.runner = start_backend_operation(romcloud_bin, action, self.request_payload())

    def _start_local_browse(self, purpose: str, path: str, romcloud_bin: str) -> None:
        self.local_browse_purpose = purpose
        self.browser_path = path
        self.browser_entries = []
        self.step = WizardStep.LOCAL_BROWSE
        self.error = ""
        self.technical_error = ""
        self.notice = ""
        self._progress_event = None
        self.runner = start_backend_operation(
            romcloud_bin,
            "setup-browse-local",
            {"path": path, "progress": True},
        )

    def _start_current_browser_operation(self, romcloud_bin: str) -> None:
        action = (
            "setup-browse-local"
            if self.step == WizardStep.LOCAL_BROWSE
            else "setup-browse-smb"
        )
        payload = (
            {"path": self.browser_path, "progress": True}
            if action == "setup-browse-local"
            else self.request_payload()
        )
        self.error = ""
        self.technical_error = ""
        self.notice = ""
        self._progress_event = None
        self.runner = start_backend_operation(romcloud_bin, action, payload)

    def _confirm_browser(self, romcloud_bin: str) -> None:
        if self.runner is not None:
            return
        if self.selected_index == 0:
            if self.step == WizardStep.SOURCE_BROWSE:
                self.source_remote_path = self.browser_path
                self._start_operation(WizardStep.DETECT, "setup-validate", romcloud_bin)
            elif self.step == WizardStep.REMOTE_BROWSE:
                self.remote_remote_path = self.browser_path
                self._start_operation(
                    WizardStep.REMOTE_VALIDATE, "setup-validate", romcloud_bin
                )
            elif self.local_browse_purpose == "source":
                self.rom_root = self.browser_path
                self._start_operation(WizardStep.DETECT, "setup-validate", romcloud_bin)
            elif self.local_browse_purpose == "remote_data":
                self.remote_data_type = "local"
                self.remote_data_root = self.browser_path
                self.step = self._post_storage_step()
                self.selected_index = 0
            else:
                self.cache_root = self.browser_path
                self.step = WizardStep.CACHE
                self.selected_index = 0
            return

        if self.selected_index == 1:
            if self.step == WizardStep.LOCAL_BROWSE:
                from pathlib import Path

                current = Path(self.browser_path)
                self.browser_path = str(current.parent)
            else:
                parts = [part for part in self.browser_path.split("/") if part]
                self.browser_path = "/".join(parts[:-1])
                if self.step == WizardStep.SOURCE_BROWSE:
                    self.source_remote_path = self.browser_path
                else:
                    self.remote_remote_path = self.browser_path
            self._start_current_browser_operation(romcloud_bin)
            return

        directories = [
            entry for entry in self.browser_entries if entry.get("is_directory")
        ]
        directory = directories[self.selected_index - 2]
        if self.step == WizardStep.LOCAL_BROWSE:
            self.browser_path = str(directory.get("path", ""))
        else:
            name = str(directory["name"])
            self.browser_path = "/".join(
                part for part in (self.browser_path, name) if part
            )
            if self.step == WizardStep.SOURCE_BROWSE:
                self.source_remote_path = self.browser_path
            else:
                self.remote_remote_path = self.browser_path
        self._start_current_browser_operation(romcloud_bin)

    def _post_storage_step(self) -> WizardStep:
        return WizardStep.LIBRARY_SYNC
