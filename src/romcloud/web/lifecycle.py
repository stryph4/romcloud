"""Lifecycle/status bridge for the existing browser manager command."""

from __future__ import annotations

import json
import os
import secrets
import socket
import subprocess
import time
from pathlib import Path
from typing import Callable


STATE_FILENAME = "manager-state.json"
DEFAULT_MANAGER_HOST = "0.0.0.0"
DEFAULT_MANAGER_PORT = 8765


def manager_state_path(data_path: str | Path) -> Path:
    return Path(data_path) / "web" / STATE_FILENAME


def network_display_host(host: str) -> str:
    if host not in {"0.0.0.0", "::"}:
        return host
    hostname = socket.gethostname().strip().lower() or "batocera"
    try:
        addresses = socket.getaddrinfo(
            hostname, None, family=socket.AF_INET, type=socket.SOCK_STREAM
        )
        for address in addresses:
            candidate = str(address[4][0])
            if candidate and not candidate.startswith("127."):
                return candidate
    except OSError:
        pass
    if hostname in {"localhost", "localhost.localdomain"}:
        hostname = "batocera"
    return hostname if "." in hostname else f"{hostname}.local"


def manager_runtime_state(
    *,
    host: str,
    port: int,
    token: str,
    scheme: str,
    pid: int,
) -> dict[str, object]:
    display_host = network_display_host(host)
    return {
        "running": True,
        "host": host,
        "port": port,
        "scheme": scheme,
        "url": f"{scheme}://{display_host}:{port}/",
        "local_url": f"{scheme}://127.0.0.1:{port}/",
        "token": token,
        "pid": pid,
    }


def write_manager_state(data_path: str | Path, state: dict[str, object]) -> Path:
    path = manager_state_path(data_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
    try:
        temporary.chmod(0o600)
    except OSError:
        pass
    temporary.replace(path)
    return path


def read_manager_state(data_path: str | Path) -> dict[str, object]:
    path = manager_state_path(data_path)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def clear_manager_state(data_path: str | Path, *, pid: int | None = None) -> None:
    path = manager_state_path(data_path)
    if pid is not None:
        state = read_manager_state(data_path)
        if int(state.get("pid", 0) or 0) != pid:
            return
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except (OSError, ValueError):
        return False
    return True


def _port_open(host: str, port: int) -> bool:
    probe_host = "127.0.0.1" if host in {"0.0.0.0", "::"} else host
    try:
        with socket.create_connection((probe_host, port), timeout=0.2):
            return True
    except OSError:
        return False


def manager_status(
    data_path: str | Path,
    *,
    pid_alive: Callable[[int], bool] = _pid_alive,
    port_open: Callable[[str, int], bool] = _port_open,
) -> dict[str, object]:
    state = read_manager_state(data_path)
    if not state:
        return {"running": False}
    try:
        pid = int(state.get("pid", 0))
        port = int(state.get("port", DEFAULT_MANAGER_PORT))
        host = str(state.get("host", DEFAULT_MANAGER_HOST))
    except (TypeError, ValueError):
        return {"running": False}
    running = pid_alive(pid) and port_open(host, port)
    return {**state, "running": running}


def start_manager(
    romcloud_bin: str | Path,
    data_path: str | Path,
    *,
    host: str = DEFAULT_MANAGER_HOST,
    port: int = DEFAULT_MANAGER_PORT,
    popen=subprocess.Popen,
    status_reader=None,
    port_open: Callable[[str, int], bool] = _port_open,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, object]:
    """Start the existing ``romcloud manager`` command and surface its state."""

    read_status = status_reader or (lambda: manager_status(data_path))
    current = read_status()
    if current.get("running"):
        return {**current, "started": False}
    if port_open(host, port):
        raise RuntimeError(f"Port {port} is already in use by another service")

    token = secrets.token_urlsafe(24)
    log_path = Path(data_path).parent / "logs" / "browser-manager.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    argv = [
        str(romcloud_bin),
        "manager",
        "--host",
        host,
        "--port",
        str(port),
        "--token",
        token,
        "--quiet",
    ]
    with log_path.open("ab") as log_handle:
        process = popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            close_fds=True,
        )

    for _ in range(50):
        state = read_status()
        if state.get("running"):
            return {**state, "started": True}
        if process.poll() is not None:
            raise RuntimeError(
                f"Library Manager exited during startup; see {log_path}"
            )
        sleep(0.1)
    raise RuntimeError(f"Library Manager did not become ready; see {log_path}")
