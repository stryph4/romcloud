"""Subprocess/JSON client — the ONLY way ports_gfx talks to ROMCloud.

``ports_gfx`` runs under Batocera's system Python; ROMCloud's actual
backend (catalog, cache, config, SMB, etc.) lives entirely in the isolated
venv and is only reachable through the installed ``romcloud`` CLI binary.
This module enforces that boundary: it never imports ``romcloud``, and only
shells out to ``<romcloud_bin> uidata <action>``, parsing a single JSON
object from stdout (see ``romcloud.cli.commands.uidata`` on the backend
side for the exact contract).

Every failure mode (missing binary, timeout, non-JSON output, malformed
shape) is captured as a :class:`BackendResult` with ``ok=False`` — this
module never raises, so the graphical UI can always render *something*
instead of crashing.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field
from typing import Any, Callable

from ports_gfx.operation import OperationRunner

RunFunc = Callable[..., "subprocess.CompletedProcess[str]"]

DEFAULT_TIMEOUT = 20.0
REFRESH_TIMEOUT = 120.0
ACTION_TIMEOUTS: dict[str, float] = {
    "refresh": REFRESH_TIMEOUT,
}


@dataclass(frozen=True)
class BackendResult:
    """Outcome of a single ``romcloud uidata <action>`` call."""

    ok: bool
    data: dict[str, Any] = field(default_factory=dict)
    error: str = ""


def call_backend(
    romcloud_bin: str,
    action: str,
    *,
    timeout: float | None = None,
    run: RunFunc = subprocess.run,
) -> BackendResult:
    """Invoke ``<romcloud_bin> uidata <action>`` and parse its JSON stdout.

    *run* is injectable (defaults to :func:`subprocess.run`) so this can be
    unit-tested without a real ``romcloud`` binary or subprocess.
    """
    effective_timeout = DEFAULT_TIMEOUT if timeout is None else timeout
    effective_timeout = ACTION_TIMEOUTS.get(action, effective_timeout)
    try:
        proc = run(
            [romcloud_bin, "uidata", action],
            capture_output=True,
            text=True,
            timeout=effective_timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return BackendResult(ok=False, error=str(exc))
    except Exception as exc:  # noqa: BLE001 — must never propagate to the UI loop
        return BackendResult(ok=False, error=f"unexpected error: {exc}")

    stdout = (proc.stdout or "").strip()
    if not stdout:
        detail = (proc.stderr or "").strip()
        suffix = f": {detail}" if detail else ""
        return BackendResult(ok=False, error=f"no output from romcloud (exit {proc.returncode}){suffix}")

    # The backend contract is "exactly one JSON line to stdout", but take
    # the last non-empty line defensively in case anything upstream ever
    # emits extra noise ahead of it.
    last_line = stdout.splitlines()[-1]
    try:
        payload = json.loads(last_line)
    except ValueError:
        return BackendResult(ok=False, error="malformed response from romcloud")

    if not isinstance(payload, dict):
        return BackendResult(ok=False, error="unexpected response shape from romcloud")

    ok = bool(payload.get("ok", False))
    if ok:
        return BackendResult(ok=True, data=payload)
    return BackendResult(ok=False, data=payload, error=str(payload.get("error", "unknown error")))


def start_backend_operation(
    romcloud_bin: str,
    action: str,
    payload: dict[str, Any] | None = None,
    *,
    extra_args: tuple[str, ...] = (),
    popen=None,  # noqa: ANN001
    max_runtime: float | None = None,
    timeout_message: str = "operation timed out",
    clock=None,  # noqa: ANN001
) -> OperationRunner:
    """Start a JSON uidata request without blocking the pygame event loop."""
    kwargs: dict[str, Any] = {
        "max_runtime": max_runtime,
        "timeout_message": timeout_message,
    }
    if payload is not None:
        kwargs["stdin_text"] = json.dumps(payload)
    if popen is not None:
        kwargs["popen"] = popen
    if clock is not None:
        kwargs["clock"] = clock
    runner = OperationRunner([romcloud_bin, "uidata", action, *extra_args], **kwargs)
    runner.start()
    return runner


def operation_result(runner: OperationRunner) -> BackendResult:
    """Parse the final JSON response captured by a finished operation."""
    stdout_lines = [line.text for line in runner.lines if line.stream == "stdout"]
    if not stdout_lines:
        return BackendResult(ok=False, error=runner.error or "no output from romcloud")
    try:
        payload = json.loads(stdout_lines[-1])
    except ValueError:
        return BackendResult(ok=False, error="malformed response from romcloud")
    if not isinstance(payload, dict):
        return BackendResult(ok=False, error="unexpected response shape from romcloud")
    if payload.get("ok"):
        return BackendResult(ok=True, data=payload)
    return BackendResult(ok=False, data=payload, error=str(payload.get("error", runner.error or "unknown error")))
