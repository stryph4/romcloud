"""Reusable non-blocking subprocess runner for long-running backend actions.

``ports_gfx.client.call_backend`` is a good fit for quick, one-shot
``romcloud uidata <action>`` JSON calls (status/healthcheck/cache-status) —
it blocks the caller for at most a short, bounded timeout. It is a poor
fit for an operation that can legitimately run for minutes (a large
catalog refresh): any fixed timeout is either too short (a real operation
gets reported as "failed" while it is still working) or an awkward guess
that still isn't actually long enough for every library size.

:class:`OperationRunner` instead launches a subprocess and never waits on
it inline. Two background reader threads (one per stream) push completed
lines onto a thread-safe queue as they arrive; the pygame event loop calls
:meth:`OperationRunner.poll` once per frame to drain whatever is currently
available (never blocking) and to notice the process has exited. This is
the same "launch it, then have the caller keep polling" shape as
``romcloud.infrastructure.mount_worker`` uses for a detached background
mount — just in-process with threads instead of an OS-level detached
process, since the operation screen needs the output live rather than
only a final status file.

No timeout is applied to a long local subprocess by default — an operation is
"running" for as long as the process is alive unless its caller supplies an
explicit network-operation deadline. Every runner also owns a bounded
terminate/kill path for application shutdown.
"""

from __future__ import annotations

import os
import queue
import signal
import subprocess
import threading
import time
from collections import deque
from dataclasses import dataclass
from enum import Enum
from typing import Callable, Optional

PopenFunc = Callable[..., "subprocess.Popen[str]"]

DEFAULT_MAX_LINES = 500
"""Bounded output history — long-running operations must never grow
unbounded memory just because a refresh emits a lot of lines."""


class OperationState(Enum):
    STARTING = "starting"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


@dataclass(frozen=True)
class OperationLine:
    """One captured line of subprocess output, tagged by which stream it
    came from so the UI can distinguish stdout progress from stderr."""

    stream: str  # "stdout" or "stderr"
    text: str


class OperationRunner:
    """Launches one subprocess and exposes its output/state without ever
    blocking the caller.

    *popen* is injectable (defaults to :func:`subprocess.Popen`) so this
    can be unit-tested with a real short-lived subprocess without needing
    the actual ``romcloud`` binary.
    """

    def __init__(
        self,
        argv: list[str],
        *,
        popen: PopenFunc = subprocess.Popen,
        max_lines: int = DEFAULT_MAX_LINES,
        stdin_text: str | None = None,
        max_runtime: float | None = None,
        timeout_message: str = "operation timed out",
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._argv = list(argv)
        self._popen = popen
        self._process: Optional["subprocess.Popen[str]"] = None
        self._queue: "queue.Queue[OperationLine]" = queue.Queue()
        self._threads: list[threading.Thread] = []
        self._lines: "deque[OperationLine]" = deque(maxlen=max_lines)
        self._state = OperationState.STARTING
        self._returncode: Optional[int] = None
        self._error = ""
        self._started = False
        self._stdin_text = stdin_text
        self._max_runtime = max_runtime
        self._timeout_message = timeout_message
        self._clock = clock
        self._started_at: float | None = None

    @property
    def state(self) -> OperationState:
        return self._state

    @property
    def lines(self) -> list[OperationLine]:
        return list(self._lines)

    @property
    def error(self) -> str:
        return self._error

    @property
    def returncode(self) -> Optional[int]:
        return self._returncode

    @property
    def is_finished(self) -> bool:
        return self._state in (OperationState.SUCCEEDED, OperationState.FAILED)

    def start(self) -> None:
        """Launch the subprocess. Never blocks and never raises — a
        missing binary or any other launch failure is recorded as
        ``FAILED`` with a message, exactly like every other failure mode
        in this UI's subprocess boundary."""
        if self._started:
            return
        self._started = True
        try:
            kwargs = {
                "stdout": subprocess.PIPE,
                "stderr": subprocess.PIPE,
                "text": True,
                "bufsize": 1,
                "start_new_session": os.name == "posix",
            }
            if self._stdin_text is not None:
                kwargs["stdin"] = subprocess.PIPE
            self._process = self._popen(self._argv, **kwargs)
        except OSError as exc:
            self._state = OperationState.FAILED
            self._error = str(exc)
            return
        except Exception as exc:  # noqa: BLE001 — must never propagate to the UI loop
            self._state = OperationState.FAILED
            self._error = f"unexpected error: {exc}"
            return

        self._state = OperationState.RUNNING
        self._started_at = self._clock()
        if self._stdin_text is not None and self._process.stdin is not None:
            try:
                self._process.stdin.write(self._stdin_text)
                self._process.stdin.close()
            except OSError as exc:
                self.cancel(reason=f"could not send request: {exc}")
                return
        self._threads = [
            threading.Thread(target=self._pump, args=(self._process.stdout, "stdout"), daemon=True),
            threading.Thread(target=self._pump, args=(self._process.stderr, "stderr"), daemon=True),
        ]
        for thread in self._threads:
            thread.start()

    def _pump(self, stream, name: str) -> None:  # noqa: ANN001
        if stream is None:
            return
        try:
            for raw_line in iter(stream.readline, ""):
                line = raw_line.rstrip("\n")
                if line:
                    self._queue.put(OperationLine(stream=name, text=line))
        except Exception:  # noqa: BLE001 — a reader thread must never crash the process
            pass
        finally:
            try:
                stream.close()
            except Exception:  # noqa: BLE001
                pass

    def _drain(self) -> list[OperationLine]:
        drained: list[OperationLine] = []
        while True:
            try:
                line = self._queue.get_nowait()
            except queue.Empty:
                break
            drained.append(line)
            self._lines.append(line)
        return drained

    def _signal_owned_process(self, sig: int, *, force: bool) -> None:
        assert self._process is not None
        pid = getattr(self._process, "pid", None)
        if os.name == "posix" and isinstance(pid, int):
            try:
                pgid = os.getpgid(pid)
                if pgid == pid:
                    os.killpg(pgid, sig)
                    return
            except (OSError, ProcessLookupError):
                return
        action = getattr(self._process, "kill" if force else "terminate", None)
        if action is not None:
            action()

    def cancel(self, *, reason: str = "cancelled", grace_period: float = 0.2) -> None:
        """Boundedly terminate the operation and every subprocess it owns."""
        if self._state != OperationState.RUNNING or self._process is None:
            return
        try:
            self._signal_owned_process(signal.SIGTERM, force=False)
        except (OSError, ProcessLookupError):
            pass
        wait = getattr(self._process, "wait", None)
        if wait is not None:
            try:
                wait(timeout=max(0.0, grace_period))
            except (OSError, subprocess.TimeoutExpired, TypeError):
                try:
                    self._signal_owned_process(signal.SIGKILL, force=True)
                except (OSError, ProcessLookupError):
                    pass
        self._state = OperationState.FAILED
        self._returncode = getattr(self._process, "returncode", None)
        self._error = reason

    def poll(self) -> list[OperationLine]:
        """Call once per frame: drains whatever output has arrived so far
        (never blocks) and, once the process has exited, records the
        final state. Returns just the lines newly drained this call."""
        if self._state != OperationState.RUNNING:
            return []

        if (
            self._max_runtime is not None
            and self._started_at is not None
            and self._clock() - self._started_at >= self._max_runtime
        ):
            self.cancel(reason=self._timeout_message)
            return []

        drained = self._drain()

        if self._process is not None and self._process.poll() is not None:
            # The reader threads may still have a few buffered lines even
            # after the process itself has exited — give them a moment to
            # finish, then drain once more so the last output isn't lost
            # to a race against this poll.
            for thread in self._threads:
                thread.join(timeout=0.5)
            drained.extend(self._drain())

            self._returncode = self._process.returncode
            self._state = OperationState.SUCCEEDED if self._returncode == 0 else OperationState.FAILED
            if self._state == OperationState.FAILED and not self._error:
                self._error = f"exited with code {self._returncode}"

        return drained
