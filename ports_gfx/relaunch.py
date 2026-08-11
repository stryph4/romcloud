"""One-shot handoff from an updated GUI to the installed Ports launcher."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable


RELAUNCH_FAILURE_LOG = Path(
    "/userdata/system/romcloud/logs/gui-relaunch.log"
)

PopenFunc = Callable[..., object]


def canonical_graphical_launcher(romcloud_bin: str) -> Path:
    """The installer-managed graphical wrapper beside ``romcloud``."""
    return Path(romcloud_bin).resolve().with_name("romcloud-ports")


@dataclass(frozen=True)
class RelaunchResult:
    attempted: bool
    launched: bool
    launcher: Path
    error: str = ""


class GuiRelaunchCoordinator:
    """Records a terminal update outcome and permits one launcher request.

    The coordinator is marked only from the update operation's successful
    final JSON result.  ``launch_once`` is called later, after Pygame and GUI
    diagnostics have been closed by the old process.
    """

    def __init__(self, romcloud_bin: str) -> None:
        self.launcher = canonical_graphical_launcher(romcloud_bin)
        self.terminal = False
        self.progress_complete = False
        self._claimed = False

    @property
    def relaunch_pending(self) -> bool:
        return self.terminal and not self._claimed

    def mark_update_succeeded(self, *, progress_complete: bool) -> bool:
        if self.terminal or not progress_complete:
            return False
        self.progress_complete = True
        self.terminal = True
        return True

    def mark_update_failed(self) -> None:
        # A failed update is deliberately non-terminal for the GUI and never
        # creates a replacement process request.
        return

    def launch_once(
        self,
        *,
        popen: PopenFunc = subprocess.Popen,
        failure_log_path: Path = RELAUNCH_FAILURE_LOG,
    ) -> RelaunchResult:
        if not self.relaunch_pending:
            return RelaunchResult(False, False, self.launcher)
        self._claimed = True
        try:
            popen(
                [str(self.launcher)],
                close_fds=True,
                start_new_session=True,
            )
        except Exception as exc:  # noqa: BLE001 - old GUI must still terminate
            error = _single_line(str(exc)) or type(exc).__name__
            _persist_failure(failure_log_path, self.launcher, error)
            return RelaunchResult(True, False, self.launcher, error)
        return RelaunchResult(True, True, self.launcher)


def relaunch_failure_message(result: RelaunchResult) -> str:
    return (
        "ROMCloud updated successfully, but automatic relaunch failed: "
        f"{result.error}. Reopen ROMCloud from the Batocera Ports menu."
    )


def _persist_failure(path: Path, launcher: Path, error: str) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with path.open("a", encoding="utf-8") as stream:
            stream.write(
                f"{stamp} update succeeded; GUI relaunch failed "
                f"launcher={launcher} error={error}; "
                "Reopen ROMCloud manually from the Batocera Ports menu\n"
            )
    except OSError:
        pass


def _single_line(value: str) -> str:
    return " ".join(str(value).replace("\r", " ").replace("\n", " ").split())
