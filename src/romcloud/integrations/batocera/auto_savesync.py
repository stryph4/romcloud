"""Batocera game lifecycle hook for background SaveSync."""

from __future__ import annotations

import errno
import os
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Callable, Optional

HOOK_PATH = Path("/userdata/system/scripts/romcloud-autosync")
_MENU_LOOP_PID_NAME = "savesync-menu-loop.pid"


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
    return (
        "#!/bin/bash\n"
        f'ROMCLOUD_BIN="{binary}"\n'
        'case "$1" in\n'
        "  gameStart)\n"
        '    "$ROMCLOUD_BIN" _autosync game-start "$2" "$3" "$4" "$5" '
        ">/dev/null 2>&1 || true\n"
        "    ;;\n"
        "  gameStop)\n"
        '    nohup "$ROMCLOUD_BIN" _autosync game-stop "$2" "$3" "$4" "$5" '
        ">/dev/null 2>&1 </dev/null &\n"
        '    nohup "$ROMCLOUD_BIN" _autosync menu-loop '
        ">/dev/null 2>&1 </dev/null &\n"
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
