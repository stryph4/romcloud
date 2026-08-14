"""Batocera game lifecycle hook for background SaveSync."""

from __future__ import annotations

import errno
import os
import shutil
import signal
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping, Optional

HOOK_PATH = Path("/userdata/system/scripts/romcloud-autosync")
_MENU_LOOP_PID_NAME = "savesync-menu-loop.pid"
ES_READINESS_TIMEOUT_SECONDS = 5.0
_ES_READINESS_POLL_SECONDS = 0.05
_ES_READINESS_MAX_POLL_SECONDS = 0.5
_DISPLAY_PROBE_TIMEOUT_SECONDS = 0.75


@dataclass(frozen=True)
class EmulationStationReadiness:
    """Result of the bounded post-emulator display handoff."""

    ready: bool
    signal: str
    detail: str
    elapsed_seconds: float
    attempts: int


def lifecycle_caller_pid(
    environment: Optional[Mapping[str, str]] = None,
) -> Optional[int]:
    """Return the exact emulatorlauncher PID captured by the hook, if valid."""
    env = os.environ if environment is None else environment
    raw = env.get("ROMCLOUD_AUTOSYNC_CALLER_PID", "")
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return None
    return value if value > 1 else None


def wait_for_emulationstation_display(
    caller_pid: Optional[int],
    *,
    timeout: float = ES_READINESS_TIMEOUT_SECONDS,
    poll_interval: float = _ES_READINESS_POLL_SECONDS,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    proc_root: Path = Path("/proc"),
    probe: Optional[Callable[[], tuple[bool, str]]] = None,
) -> EmulationStationReadiness:
    """Wait for configgen to exit and ES to own the active window.

    Batocera keeps the EmulationStation process alive while a game runs, and
    its global ``/tmp/emulationstation.ready`` marker is not cleared during
    that interval.  Process existence therefore cannot prove that the
    post-game renderer has returned.  The lifecycle caller disappearing
    establishes that configgen finished its gameStop scripts and video-mode
    restoration; an active-window observation then proves ES reclaimed the
    graphical session.  Two consecutive observations avoid launching on a
    transient compositor update.
    """
    started = clock()
    deadline = started + max(0.0, timeout)
    delay = max(0.01, poll_interval)
    display_probe = probe or (
        lambda: probe_emulationstation_display(proc_root=proc_root)
    )
    caller_finished = caller_pid is None
    attempts = 0
    consecutive_ready = 0
    last_signal = (
        "lifecycle-caller-unknown" if caller_finished else "caller-active"
    )
    last_detail = (
        "no exact lifecycle caller PID was supplied"
        if caller_finished
        else f"waiting for emulatorlauncher pid {caller_pid} to exit"
    )

    while True:
        now = clock()
        if not caller_finished:
            caller_finished = not _emulatorlauncher_pid_matches(
                caller_pid, proc_root=proc_root
            )
            if caller_finished:
                last_signal = "lifecycle-caller-exited"
                last_detail = f"emulatorlauncher pid {caller_pid} exited"

        if caller_finished:
            attempts += 1
            ready, detail = display_probe()
            if ready:
                consecutive_ready += 1
                last_signal = detail
                last_detail = detail
                if consecutive_ready >= 2:
                    return EmulationStationReadiness(
                        ready=True,
                        signal=last_signal,
                        detail=last_detail,
                        elapsed_seconds=max(0.0, clock() - started),
                        attempts=attempts,
                    )
            else:
                consecutive_ready = 0
                last_signal = "display-not-ready"
                last_detail = detail

        now = clock()
        if now >= deadline:
            return EmulationStationReadiness(
                ready=False,
                signal=last_signal,
                detail=last_detail,
                elapsed_seconds=max(0.0, now - started),
                attempts=attempts,
            )
        sleep(min(delay, max(0.0, deadline - now)))
        delay = min(_ES_READINESS_MAX_POLL_SECONDS, delay * 1.5)


def probe_emulationstation_display(
    *,
    environment: Optional[Mapping[str, str]] = None,
    proc_root: Path = Path("/proc"),
    which: Callable[[str], Optional[str]] = shutil.which,
    run: Callable[..., subprocess.CompletedProcess] = subprocess.run,
) -> tuple[bool, str]:
    """Return whether ES is the compositor/window-manager active window."""
    env = os.environ if environment is None else environment
    failures: list[str] = []

    if env.get("WAYLAND_DISPLAY"):
        wlrctl = which("wlrctl")
        if wlrctl:
            for match in ("app_id:emulationstation", "title:EmulationStation"):
                try:
                    result = run(
                        [wlrctl, "toplevel", "find", match, "state:active"],
                        stdin=subprocess.DEVNULL,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        check=False,
                        timeout=_DISPLAY_PROBE_TIMEOUT_SECONDS,
                        env=dict(env),
                    )
                except (OSError, subprocess.SubprocessError) as exc:
                    failures.append(f"wlrctl probe failed: {exc}")
                    break
                if result.returncode == 0:
                    return True, f"wayland-active-es ({match})"
            else:
                failures.append("wlrctl found no active EmulationStation window")
        else:
            failures.append("wlrctl unavailable")

    if env.get("DISPLAY"):
        xdotool = which("xdotool")
        if xdotool:
            try:
                result = run(
                    [xdotool, "getactivewindow", "getwindowpid"],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    check=False,
                    timeout=_DISPLAY_PROBE_TIMEOUT_SECONDS,
                    text=True,
                    env=dict(env),
                )
            except (OSError, subprocess.SubprocessError) as exc:
                failures.append(f"xdotool probe failed: {exc}")
            else:
                active_pid = _last_positive_int(result.stdout or "")
                if result.returncode == 0 and active_pid is not None:
                    if _emulationstation_pid_matches(active_pid, proc_root=proc_root):
                        return True, f"x11-active-es (pid {active_pid})"
                    failures.append(f"X11 active window belongs to pid {active_pid}")
                else:
                    failures.append("xdotool did not return an active-window PID")
        else:
            failures.append("xdotool unavailable")

    if not failures:
        failures.append("no supported Wayland/X11 active-window signal")
    return False, "; ".join(failures)


def _emulatorlauncher_pid_matches(
    pid: Optional[int], *, proc_root: Path = Path("/proc")
) -> bool:
    if pid is None or pid <= 1:
        return False
    argv = _proc_argv(pid, proc_root=proc_root)
    return any("emulatorlauncher" in Path(value).name for value in argv)


def _emulationstation_pid_matches(
    pid: int, *, proc_root: Path = Path("/proc")
) -> bool:
    try:
        comm = (proc_root / str(pid) / "comm").read_text(
            encoding="utf-8", errors="replace"
        ).strip()
    except OSError:
        comm = ""
    if comm == "emulationstation":
        return True
    return any(
        Path(value).name == "emulationstation"
        for value in _proc_argv(pid, proc_root=proc_root)
    )


def _proc_argv(pid: int, *, proc_root: Path) -> tuple[str, ...]:
    try:
        raw = (proc_root / str(pid) / "cmdline").read_bytes()
    except OSError:
        return ()
    return tuple(
        part.decode("utf-8", errors="replace")
        for part in raw.split(b"\0")
        if part
    )


def _last_positive_int(value: str) -> Optional[int]:
    for token in reversed(value.split()):
        try:
            number = int(token)
        except ValueError:
            continue
        if number > 1:
            return number
    return None


def menu_loop_pid_path(data_root: Path) -> Path:
    return Path(data_root) / _MENU_LOOP_PID_NAME


def record_menu_loop_pid(data_root: Path, pid: int | None = None) -> None:
    """Publish ownership only after the resident singleton lock is held."""
    owner = os.getpid() if pid is None else pid
    path = menu_loop_pid_path(data_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(f"{owner}\n", encoding="ascii")
    temporary.replace(path)


def clear_menu_loop_pid(data_root: Path, pid: int | None = None) -> None:
    """Remove only this process's ownership record, never a newer owner's."""
    owner = os.getpid() if pid is None else pid
    path = menu_loop_pid_path(data_root)
    try:
        recorded = int(path.read_text(encoding="ascii").strip())
    except (OSError, ValueError):
        return
    if recorded == owner:
        path.unlink(missing_ok=True)


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError as exc:
        return exc.errno != errno.ESRCH
    return True


def _menu_loop_cmdline_matches(
    pid: int, *, proc_root: Path = Path("/proc")
) -> bool:
    try:
        raw = (proc_root / str(pid) / "cmdline").read_bytes()
    except OSError:
        return False
    argv = [
        part.decode("utf-8", errors="replace")
        for part in raw.split(b"\0")
        if part
    ]
    return argv[1:] == [
        "-m",
        "romcloud.cli.main",
        "_autosync",
        "menu-loop",
    ]


def running_menu_loop_pid(
    data_root: Path, *, proc_root: Path = Path("/proc")
) -> Optional[int]:
    """Return only an exactly identified ROMCloud resident process."""
    path = menu_loop_pid_path(data_root)
    try:
        pid = int(path.read_text(encoding="ascii").strip())
    except (OSError, ValueError):
        path.unlink(missing_ok=True)
        return None
    if _pid_alive(pid) and _menu_loop_cmdline_matches(pid, proc_root=proc_root):
        return pid
    path.unlink(missing_ok=True)
    return None


def spawn_menu_loop(
    data_root: Path,
    *,
    python_executable: Optional[str] = None,
    popen: Callable[..., subprocess.Popen] = subprocess.Popen,
) -> int:
    """Detach the resident loop without doing any SaveSync/provider work."""
    existing = running_menu_loop_pid(data_root)
    if existing is not None:
        return existing
    process = popen(
        [
            python_executable or sys.executable,
            "-m",
            "romcloud.cli.main",
            "_autosync",
            "menu-loop",
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    return process.pid


def spawn_remote_reconnect(
    *,
    python_executable: Optional[str] = None,
    popen: Callable[..., subprocess.Popen] = subprocess.Popen,
) -> int:
    """Detach one reconnect trigger without provider or SaveSync work."""
    process = popen(
        [
            python_executable or sys.executable,
            "-m",
            "romcloud.cli.main",
            "_autosync",
            "remote-reconnect",
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    return process.pid


def _signal_owned_process(pid: int, sig: int) -> None:
    getpgid = getattr(os, "getpgid", None)
    killpg = getattr(os, "killpg", None)
    if getpgid is not None and killpg is not None:
        try:
            pgid = getpgid(pid)
        except OSError:
            pgid = None
        if pgid == pid:
            killpg(pgid, sig)
            return
    os.kill(pid, sig)


def stop_menu_loop(
    data_root: Path,
    *,
    grace_period: float = 1.0,
    poll_interval: float = 0.1,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> bool:
    """Boundedly stop only the verified ROMCloud resident menu loop."""
    pid = running_menu_loop_pid(data_root)
    if pid is None:
        return False
    try:
        _signal_owned_process(pid, signal.SIGTERM)
    except ProcessLookupError:
        clear_menu_loop_pid(data_root, pid)
        return False
    deadline = clock() + max(0.0, grace_period)
    while clock() < deadline:
        if not _pid_alive(pid):
            break
        sleep(min(poll_interval, max(0.0, deadline - clock())))
    else:
        try:
            _signal_owned_process(pid, getattr(signal, "SIGKILL", signal.SIGTERM))
        except (ProcessLookupError, PermissionError):
            pass
    clear_menu_loop_pid(data_root, pid)
    return True


def hook_content(romcloud_bin: Path) -> str:
    binary = str(romcloud_bin).replace('"', '\\"')
    lifecycle_log = str(
        Path(romcloud_bin).parent.parent / "logs" / "auto-savesync-lifecycle.log"
    ).replace('"', '\\"')
    return (
        "#!/bin/bash\n"
        f'export ROMCLOUD_BIN="{binary}"\n'
        f'ROMCLOUD_AUTOSYNC_LOG="{lifecycle_log}"\n'
        'mkdir -p "$(dirname "$ROMCLOUD_AUTOSYNC_LOG")" 2>/dev/null || true\n'
        'case "$1" in\n'
        "  gameStart)\n"
        '    "$ROMCLOUD_BIN" _autosync game-start "$2" "$3" "$4" "$5" '
        ">/dev/null 2>&1 || true\n"
        "    ;;\n"
        "  gameStop)\n"
        '    printf \'%s pid=%s parent_pid=%s event="game_stop_hook_entered"\\n\' '
        '"$(date -Iseconds 2>/dev/null || date)" "$$" '
        '"$PPID" '
        '>> "$ROMCLOUD_AUTOSYNC_LOG" 2>/dev/null || true\n'
        '    if [[ ! -x "$ROMCLOUD_BIN" ]]; then\n'
        '      printf \'%s pid=%s event="game_stop_handoff_failed" '
        'reason="romcloud_bin_unavailable"\\n\' '
        '"$(date -Iseconds 2>/dev/null || date)" "$$" '
        '>> "$ROMCLOUD_AUTOSYNC_LOG" 2>/dev/null || true\n'
        "    else\n"
        '      ROMCLOUD_AUTOSYNC_CALLER_PID="$PPID" nohup "$ROMCLOUD_BIN" '
        '_autosync game-stop "$2" "$3" "$4" "$5" '
        '>> "$ROMCLOUD_AUTOSYNC_LOG" 2>&1 </dev/null &\n'
        "      ROMCLOUD_AUTOSYNC_STATUS=$?\n"
        "      ROMCLOUD_AUTOSYNC_WORKER_PID=$!\n"
        '      if [[ "$ROMCLOUD_AUTOSYNC_STATUS" -eq 0 ]]; then\n'
        '        printf \'%s pid=%s caller_pid=%s worker_pid=%s '
        'event="game_stop_handoff_started"\\n\' '
        '"$(date -Iseconds 2>/dev/null || date)" "$$" "$PPID" '
        '"$ROMCLOUD_AUTOSYNC_WORKER_PID" '
        '>> "$ROMCLOUD_AUTOSYNC_LOG" 2>/dev/null || true\n'
        "      else\n"
        '        printf \'%s pid=%s event="game_stop_handoff_failed" '
        'status=%s\\n\' '
        '"$(date -Iseconds 2>/dev/null || date)" "$$" '
        '"$ROMCLOUD_AUTOSYNC_STATUS" '
        '>> "$ROMCLOUD_AUTOSYNC_LOG" 2>/dev/null || true\n'
        "      fi\n"
        "    fi\n"
        '    nohup "$ROMCLOUD_BIN" _autosync menu-loop '
        ">/dev/null 2>&1 </dev/null &\n"
        '    printf \'%s pid=%s event="game_stop_hook_returned"\\n\' '
        '"$(date -Iseconds 2>/dev/null || date)" "$$" '
        '>> "$ROMCLOUD_AUTOSYNC_LOG" 2>/dev/null || true\n'
        "    ;;\n"
        "  emulationstationStart|systemStart|frontendStart)\n"
        '    nohup "$ROMCLOUD_BIN" _autosync menu-loop '
        ">/dev/null 2>&1 </dev/null &\n"
        "    ;;\n"
        "esac\n"
        "exit 0\n"
    )


def install_hook(
    romcloud_bin: Path, *, hook_path: Optional[Path] = None
) -> Path:
    """Atomically install the managed hook and make it executable."""
    hook_path = Path(hook_path or HOOK_PATH)
    hook_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = hook_path.with_name(f".{hook_path.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(hook_content(romcloud_bin), encoding="utf-8")
    temporary.chmod(0o755)
    temporary.replace(hook_path)
    return hook_path
