"""Lifecycle/status bridge for the existing browser manager command."""

from __future__ import annotations

import json
import os
import secrets
import shutil
import signal
import socket
import ssl
import subprocess
import time
import urllib.request
from contextlib import contextmanager
from pathlib import Path
from typing import Callable
from urllib.parse import urlencode


STATE_FILENAME = "manager-state.json"
LOCK_FILENAME = "manager.lock"
DEFAULT_MANAGER_HOST = "0.0.0.0"
DEFAULT_MANAGER_PORT = 8765


def manager_state_path(data_path: str | Path) -> Path:
    return Path(data_path) / "web" / STATE_FILENAME


@contextmanager
def manager_instance_lock(data_path: str | Path):
    """Hold the single manager ownership lock for this data directory."""

    path = Path(data_path) / "web" / LOCK_FILENAME
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+b")
    try:
        if os.name == "nt":
            import msvcrt

            if path.stat().st_size == 0:
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                raise RuntimeError("Library Manager is already running.") from exc
        else:
            import fcntl

            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                raise RuntimeError("Library Manager is already running.") from exc
        yield
    finally:
        try:
            if os.name == "nt":
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except (OSError, NameError):
            pass
        handle.close()


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


def network_hosts(host: str) -> list[str]:
    if host not in {"0.0.0.0", "::"}:
        return [host]
    hostname = socket.gethostname().strip().lower() or "batocera"
    names = [hostname if "." in hostname else f"{hostname}.local"]
    addresses: list[str] = []
    try:
        for address in socket.getaddrinfo(hostname, None, socket.AF_INET):
            candidate = str(address[4][0])
            if candidate and not candidate.startswith("127.") and candidate not in addresses:
                addresses.append(candidate)
    except OSError:
        pass
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.connect(("192.0.2.1", 9))
            candidate = str(probe.getsockname()[0])
            if candidate and not candidate.startswith("127.") and candidate not in addresses:
                addresses.insert(0, candidate)
    except OSError:
        pass
    return [*addresses, *[name for name in names if name not in addresses]]


def manager_runtime_state(
    *,
    host: str,
    port: int,
    token: str,
    scheme: str,
    pid: int,
    instance_id: str | None = None,
) -> dict[str, object]:
    display_hosts = network_hosts(host)
    display_host = display_hosts[0]
    return {
        "running": True,
        "host": host,
        "port": port,
        "scheme": scheme,
        "url": f"{scheme}://{display_host}:{port}/",
        "remote_urls": [f"{scheme}://{item}:{port}/" for item in display_hosts],
        "lan_ip": next((item for item in display_hosts if item.replace(".", "").isdigit()), None),
        "local_hostname": next((item for item in display_hosts if item.endswith(".local")), None),
        "local_url": f"{scheme}://127.0.0.1:{port}/",
        "token": token,
        "pid": pid,
        "instance_id": instance_id,
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
    include_secret: bool = False,
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
    result = {**state, "running": running}
    if not include_secret:
        result.pop("token", None)
    return result


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
    instance_id = secrets.token_urlsafe(18)
    log_path = Path(data_path).parent / "logs" / "browser-manager.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    argv = [
        str(romcloud_bin),
        "manager",
        "--host",
        host,
        "--port",
        str(port),
        "--quiet",
    ]
    environment = os.environ.copy()
    environment["ROMCLOUD_MANAGER_TOKEN"] = token
    environment["ROMCLOUD_MANAGER_INSTANCE"] = instance_id
    with log_path.open("ab") as log_handle:
        process = popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            close_fds=True,
            env=environment,
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
    terminate = getattr(process, "terminate", None)
    if terminate is not None:
        terminate()
    raise RuntimeError(f"Library Manager did not become ready; see {log_path}")


def stop_manager(
    data_path: str | Path,
    *,
    kill: Callable[[int, int], None] = os.kill,
    sleep: Callable[[float], None] = time.sleep,
    pid_alive: Callable[[int], bool] = _pid_alive,
    timeout: float = 5.0,
    owned_pid: Callable[[int, str], bool] | None = None,
) -> bool:
    """Stop only the process recorded as ROMCloud's manager, boundedly."""

    state = read_manager_state(data_path)
    try:
        pid = int(state.get("pid", 0))
    except (TypeError, ValueError):
        pid = 0
    if not pid_alive(pid):
        clear_manager_state(data_path)
        return False
    instance_id = str(state.get("instance_id") or "")
    verify_owned = owned_pid or _owned_manager_pid
    if not verify_owned(pid, instance_id):
        clear_manager_state(data_path, pid=pid)
        return False
    kill(pid, signal.SIGTERM)
    deadline = time.monotonic() + timeout
    while pid_alive(pid) and time.monotonic() < deadline:
        sleep(0.05)
    if pid_alive(pid):
        kill(pid, signal.SIGKILL)
    clear_manager_state(data_path, pid=pid)
    return True


def _owned_manager_pid(pid: int, instance_id: str) -> bool:
    if os.name != "posix":
        return True
    if instance_id:
        try:
            environment = Path(f"/proc/{pid}/environ").read_bytes().split(b"\0")
        except OSError:
            return False
        expected = f"ROMCLOUD_MANAGER_INSTANCE={instance_id}".encode()
        return expected in environment
    try:
        command = Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ")
    except OSError:
        return False
    return b"romcloud" in command and b"manager" in command


def _manager_request(
    data_path: str | Path,
    path: str,
    *,
    method: str = "GET",
    body: dict[str, object] | None = None,
    opener=urllib.request.urlopen,
) -> dict[str, object]:
    state = read_manager_state(data_path)
    token = str(state.get("token", ""))
    local_url = str(state.get("local_url", ""))
    if not token or not local_url:
        raise RuntimeError("Library Manager authentication state is unavailable.")
    payload = None if body is None else json.dumps(body).encode("utf-8")
    headers = {"Authorization": f"Bearer {token}"}
    if payload is not None:
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(
        local_url.rstrip("/") + path,
        data=payload,
        headers=headers,
        method=method,
    )
    context = ssl._create_unverified_context() if local_url.startswith("https://") else None
    with opener(request, timeout=2, context=context) as response:
        result = json.load(response)
    if not isinstance(result, dict):
        raise RuntimeError("Library Manager returned an invalid response.")
    return result


def issue_browser_bootstrap(
    data_path: str | Path,
    *,
    kind: str,
    opener=urllib.request.urlopen,
) -> dict[str, object]:
    if kind not in {"local", "remote"}:
        raise ValueError("Bootstrap kind must be local or remote.")
    result = _manager_request(
        data_path,
        "/api/auth/bootstrap",
        method="POST",
        body={"kind": kind},
        opener=opener,
    )
    state = manager_status(data_path)
    base = str(state.get("local_url" if kind == "local" else "url", ""))
    parameter = "bootstrap" if kind == "local" else "pair"
    return {**result, "url": f"{base}?{urlencode({parameter: result['code']})}"}


def find_local_browser(*, which=shutil.which) -> str | None:
    """Resolve the Chromium-compatible kiosk runtime used on Batocera."""

    configured = os.environ.get("ROMCLOUD_BROWSER")
    candidates = [configured] if configured else []
    candidates.extend(("chromium", "chromium-browser", "google-chrome", "google-chrome-stable"))
    for candidate in candidates:
        if not candidate:
            continue
        resolved = which(candidate)
        if resolved:
            return resolved
        path = Path(candidate)
        if path.is_file() and os.access(path, os.X_OK):
            return str(path)
    return None


def launch_local_browser(
    data_path: str | Path,
    *,
    browser: str | None = None,
    popen=subprocess.Popen,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, object]:
    """Launch a kiosk browser and return only after it exits cleanly."""

    executable = browser or find_local_browser()
    if not executable:
        raise RuntimeError(
            "Open Here requires a Chromium-compatible local browser runtime; "
            "none was found. Set ROMCLOUD_BROWSER to its executable path."
        )
    handoff = issue_browser_bootstrap(data_path, kind="local")
    from romcloud.web.tls import manager_certificate_spki_pin

    certificate_pin = manager_certificate_spki_pin(data_path)
    profile = Path(data_path) / "web" / "local-browser-profile"
    profile.mkdir(parents=True, exist_ok=True)
    argv = [
        executable,
        "--kiosk",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-session-crashed-bubble",
        f"--ignore-certificate-errors-spki-list={certificate_pin}",
        f"--user-data-dir={profile}",
        str(handoff["url"]),
    ]
    # Stay in the uidata operation's process group so cancelling the native
    # screen also terminates the browser it owns.
    process = popen(argv)
    launch_id = str(handoff.get("launch_id", ""))
    try:
        while process.poll() is None:
            if launch_id:
                status = _manager_request(
                    data_path, f"/api/local-session-status/{launch_id}"
                )
                if status.get("exit_requested"):
                    process.terminate()
                    try:
                        process.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        process.kill()
                    break
            sleep(0.25)
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
    return {"closed": True, "browser": executable, "launch_mechanism": argv[:-1]}
