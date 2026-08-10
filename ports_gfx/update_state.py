"""Non-blocking GUI state for startup and manual update checks."""

from __future__ import annotations

from ports_gfx.client import BackendResult, operation_result
from ports_gfx.operation import OperationLine, OperationRunner, OperationState


class UpdateCheckState:
    def __init__(self) -> None:
        self.runner: OperationRunner | None = None
        self.status = "idle"
        self.update_available = False
        self.current_version = "unknown"
        self.available_version = ""
        self.error = ""

    @property
    def checking(self) -> bool:
        return self.status == "checking"

    @property
    def banner(self) -> str:
        if not self.update_available:
            return ""
        return f"Update available v{self.available_version}"

    def start(self, romcloud_bin: str, *, popen=None) -> None:  # noqa: ANN001
        if self.checking:
            return
        kwargs = {} if popen is None else {"popen": popen}
        self.runner = OperationRunner(
            [romcloud_bin, "uidata", "update-check"], **kwargs
        )
        self.runner.start()
        self.status = "checking"
        self.error = ""

    def poll(self) -> list[OperationLine]:
        if self.runner is None or self.status != "checking":
            return []
        lines = self.runner.poll()
        if self.runner.is_finished:
            self._finish(operation_result(self.runner))
        return lines

    def _finish(self, result: BackendResult) -> None:
        if not result.ok:
            self.status = "error"
            self.error = result.error
            self.update_available = False
            return
        self.current_version = str(result.data.get("current_version", "unknown"))
        self.available_version = str(result.data.get("available_version", ""))
        self.update_available = bool(result.data.get("update_available", False))
        self.status = "available" if self.update_available else "current"

    @classmethod
    def completed(cls, result: BackendResult) -> "UpdateCheckState":
        state = cls()
        state._finish(result)
        return state


def update_controls_disabled(operation_state: OperationState | None) -> bool:
    """Conflicting controls stay disabled only while installation is active."""
    return operation_state in (OperationState.STARTING, OperationState.RUNNING)
