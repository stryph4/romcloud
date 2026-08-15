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
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable


STATE_FILENAME = "manager-state.json"
LOCK_FILENAME = "manager.lock"
START_LOCK_FILENAME = "manager-start.lock"
DEFAULT_MANAGER_HOST = "0.0.0.0"
DEFAULT_MANAGER_PORT = 8765
PATH_BROWSER_NAMES = (
    "chromium",
    "chromium-browser",
    "google-chrome",
    "google-chrome-stable",
)
BATOCERA_BROWSER_PATHS = (
    Path("/userdata/system/add-ons/google-chrome/GoogleChrome.AppImage"),
    Path("/userdata/system/add-ons/chromium/Chromium.AppImage"),
)
BATOCERA_BROWSER_DIRECTORIES = tuple(path.parent for path in BATOCERA_BROWSER_PATHS)


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


@contextmanager
def manager_start_lock(data_path: str | Path):
    """Serialize bounded start/reuse decisions across simultaneous callers."""

    path = Path(data_path) / "web" / START_LOCK_FILENAME
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+b")
    try:
        if os.name == "nt":
            import msvcrt

            if path.stat().st_size == 0:
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
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
    # Keep the bookmarkable mDNS name primary; numeric LAN addresses remain
    # explicit fallbacks for networks where .local resolution is unavailable.
    return [*[name for name in names if name not in addresses], *addresses]


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
    owned_pid: Callable[[int, str], bool] | None = None,
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
    instance_id = str(state.get("instance_id") or "")
    process_alive = pid_alive(pid)
    ownership_ok = not instance_id or (
        process_alive and (owned_pid or _owned_manager_pid)(pid, instance_id)
    )
    running = process_alive and ownership_ok and port_open(host, port)
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
    """Serialize startup so repeated service/UI calls stay idempotent."""

    with manager_start_lock(data_path):
        return _start_manager_unlocked(
            romcloud_bin,
            data_path,
            host=host,
            port=port,
            popen=popen,
            status_reader=status_reader,
            port_open=port_open,
            sleep=sleep,
        )


def _start_manager_unlocked(
    romcloud_bin: str | Path,
    data_path: str | Path,
    *,
    host: str,
    port: int,
    popen,
    status_reader,
    port_open: Callable[[str, int], bool],
    sleep: Callable[[float], None],
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

    def append_event(message: str) -> None:
        timestamp = datetime.now(timezone.utc).isoformat()
        with log_path.open("ab") as event_handle:
            event_handle.write(f"[{timestamp}] startup: {message}\n".encode())

    append_event(f"spawn requested host={host} port={port}")
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
            append_event(
                f"ready pid={state.get('pid', process.pid if hasattr(process, 'pid') else 'unknown')} "
                f"host={state.get('host', host)} port={state.get('port', port)}"
            )
            return {**state, "started": True}
        returncode = process.poll()
        if returncode is not None:
            append_event(f"failed: child exited with status {returncode}")
            raise RuntimeError(
                f"Library Manager exited during startup; see {log_path}"
            )
        sleep(0.1)
    terminate = getattr(process, "terminate", None)
    if terminate is not None:
        terminate()
    append_event("failed: readiness timeout after 5 seconds")
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


def issue_pairing_code(
    data_path: str | Path,
    *,
    opener=urllib.request.urlopen,
) -> dict[str, object]:
    result = _manager_request(
        data_path,
        "/api/auth/pairing-code",
        method="POST",
        body={},
        opener=opener,
    )
    state = manager_status(data_path)
    return {**result, "url": str(state.get("url", ""))}


def _browser_candidates(
    *,
    data_path: str | Path | None,
    which: Callable[[str], str | None],
    configured: str | None,
    persistent_paths: Iterable[str | Path],
) -> list[dict[str, str]]:
    """Return ordered candidates without treating a wrapper script as a browser."""

    candidates: list[dict[str, str]] = []
    if configured:
        candidates.append(
            {"path": configured, "source": "configured", "ownership": "user-installed"}
        )
    for name in PATH_BROWSER_NAMES:
        resolved = which(name)
        candidates.append(
            {
                "path": resolved or name,
                "source": f"PATH:{name}",
                "ownership": "user-installed",
                "resolved": "true" if resolved else "false",
            }
        )
    for item in persistent_paths:
        candidates.append(
            {
                "path": str(item),
                "source": "Batocera persistent add-on",
                "ownership": "user-installed",
            }
        )
    if data_path is not None:
        from romcloud.web.browser_runtime import managed_browser

        managed = managed_browser(data_path)
        if managed:
            candidates.append(
                {
                    "path": managed,
                    "source": "ROMCloud managed runtime",
                    "ownership": "romcloud-managed",
                }
            )

    unique: list[dict[str, str]] = []
    seen: set[str] = set()
    for candidate in candidates:
        key = os.path.realpath(candidate["path"])
        if key in seen:
            continue
        seen.add(key)
        unique.append(candidate)
    return unique


def _probe_local_browser(
    executable: str,
    *,
    run=subprocess.run,
    timeout: float = 5.0,
) -> dict[str, object]:
    """Prove that a candidate starts and identifies as Chromium-compatible."""

    path = Path(executable)
    if not path.is_file():
        return {"compatible": False, "reason": "executable was not found"}
    if os.name != "nt" and not os.access(path, os.X_OK):
        return {"compatible": False, "reason": "file is not executable"}
    try:
        result = run(
            [str(path), "--version"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return {"compatible": False, "reason": f"version probe timed out after {timeout:g}s"}
    except OSError as exc:
        return {"compatible": False, "reason": f"version probe could not start: {exc}"}
    output = " ".join(
        part.strip() for part in (result.stdout or "", result.stderr or "") if part.strip()
    )
    if result.returncode != 0:
        detail = output[:240] or "no diagnostic output"
        return {
            "compatible": False,
            "reason": f"version probe exited {result.returncode}: {detail}",
        }
    if not any(
        marker in output.casefold()
        for marker in ("chromium", "google chrome", "chrome for testing", "headlesschrome")
    ):
        return {
            "compatible": False,
            "reason": f"unsupported browser identity: {output[:240] or 'empty output'}",
        }
    return {"compatible": True, "version": output[:240]}


def discover_local_browser(
    *,
    data_path: str | Path | None = None,
    which=shutil.which,
    run=subprocess.run,
    configured: str | None = None,
    persistent_paths: Iterable[str | Path] | None = None,
) -> dict[str, object]:
    """Resolve and capability-test the first usable local Chromium runtime."""

    configured = os.environ.get("ROMCLOUD_BROWSER") if configured is None else configured
    if persistent_paths is None:
        discovered_paths = list(BATOCERA_BROWSER_PATHS)
        for directory in BATOCERA_BROWSER_DIRECTORIES:
            if directory.is_dir():
                discovered_paths.extend(sorted(directory.glob("*.AppImage")))
        persistent_paths = discovered_paths
    diagnostics: list[dict[str, object]] = []
    for candidate in _browser_candidates(
        data_path=data_path,
        which=which,
        configured=configured,
        persistent_paths=persistent_paths,
    ):
        if candidate.get("resolved") == "false":
            probe = {"compatible": False, "reason": "command was not found on PATH"}
        else:
            probe = _probe_local_browser(candidate["path"], run=run)
        diagnostic = {**candidate, **probe}
        diagnostic.pop("resolved", None)
        diagnostics.append(diagnostic)
        if probe["compatible"]:
            return {"browser": diagnostic, "diagnostics": diagnostics}
    return {"browser": None, "diagnostics": diagnostics}


def find_local_browser(
    *, data_path: str | Path | None = None, which=shutil.which, run=subprocess.run
) -> str | None:
    """Compatibility wrapper returning only the selected executable path."""

    result = discover_local_browser(data_path=data_path, which=which, run=run)
    browser = result.get("browser")
    return str(browser["path"]) if isinstance(browser, dict) else None


def local_browser_runtime_status(data_path: str | Path) -> dict[str, object]:
    """Report both managed lifecycle state and the browser Open Here will use."""

    from romcloud.web.browser_runtime import runtime_status

    managed = runtime_status(data_path)
    discovery = discover_local_browser(data_path=data_path)
    browser = discovery.get("browser")
    return {
        **managed,
        "available": isinstance(browser, dict),
        "active_browser": browser,
        "discovery_diagnostics": discovery["diagnostics"],
        "remove_available": bool(
            isinstance(browser, dict) and browser.get("ownership") == "romcloud-managed"
        ),
    }


@contextmanager
def _local_browser_lock(data_path: str | Path):
    path = Path(data_path) / "web" / "local-browser.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+b")
    try:
        if os.name != "nt":
            import fcntl

            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                raise RuntimeError("Open Here is already running.") from exc
        yield
    finally:
        if os.name != "nt":
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
        handle.close()


def launch_local_browser(
    data_path: str | Path,
    *,
    browser: str | None = None,
    allow_no_sandbox: bool = False,
    popen=subprocess.Popen,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, object]:
    """Launch a kiosk browser and return only after it exits cleanly."""

    discovery = None if browser else discover_local_browser(data_path=data_path)
    selected = discovery.get("browser") if discovery else None
    executable = browser or (str(selected["path"]) if isinstance(selected, dict) else None)
    log_path = Path(data_path).parent / "logs" / "browser-open.log"
    if not executable:
        diagnostics = "; ".join(
            f"{item['source']} ({item['path']}): {item['reason']}"
            for item in (discovery or {}).get("diagnostics", [])
        )
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("ab") as log_handle:
            log_handle.write(
                f"[{datetime.now(timezone.utc).isoformat()}] browser resolution failed: {diagnostics}\n".encode()
            )
        raise RuntimeError(
            "Open Here requires a Chromium-compatible local browser runtime; "
            "none was found. Managed installation is not enabled until Chrome for "
            "Testing passes Batocera dependency and sandbox validation. Remote "
            f"Library Browser access remains available. Probes: {diagnostics}. See {log_path}"
        )
    ownership = selected.get("ownership") if isinstance(selected, dict) else "explicit"
    if allow_no_sandbox and ownership != "user-installed":
        raise RuntimeError(
            "Disabling the browser sandbox is allowed only as an explicit fallback "
            "for a user-installed browser, never for a ROMCloud-managed runtime."
        )
    launch = _manager_request(
        data_path,
        "/api/auth/local-launch",
        method="POST",
        body={},
    )
    from romcloud.web.tls import manager_certificate_spki_pin

    certificate_pin = manager_certificate_spki_pin(data_path)
    profile = Path(data_path) / "web" / "local-browser-profile"
    profile.mkdir(parents=True, exist_ok=True)
    local_url = str(manager_status(data_path).get("local_url", ""))
    separator = "&" if "?" in local_url else "?"
    controller_url = f"{local_url}{separator}interaction=controller"
    argv = [
        executable,
        "--kiosk",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-session-crashed-bubble",
        f"--ignore-certificate-errors-spki-list={certificate_pin}",
        f"--user-data-dir={profile}",
        controller_url,
    ]
    if allow_no_sandbox:
        argv.insert(1, "--no-sandbox")
    environment = os.environ.copy()
    log_path.parent.mkdir(parents=True, exist_ok=True)
    launch_id = str(launch.get("launch_id", ""))
    with _local_browser_lock(data_path):
        with log_path.open("a+b") as log_handle:
            timestamp = datetime.now(timezone.utc).isoformat()
            display = environment.get("DISPLAY", "<unset>")
            xauthority = environment.get("XAUTHORITY", "<unset>")
            wayland = environment.get("WAYLAND_DISPLAY", "<unset>")
            xdg_runtime = environment.get("XDG_RUNTIME_DIR", "<unset>")
            log_handle.write(
                (
                    f"[{timestamp}] Open Here launch browser={executable} uid={getattr(os, 'getuid', lambda: 'unknown')()} "
                    f"DISPLAY={display} XAUTHORITY={xauthority} WAYLAND_DISPLAY={wayland} "
                    f"XDG_RUNTIME_DIR={xdg_runtime} sandbox_flags="
                    f"{'--no-sandbox (explicit user opt-in)' if allow_no_sandbox else 'none'}\n"
                ).encode()
            )
            for diagnostic in (discovery or {}).get("diagnostics", []):
                log_handle.write(
                    (
                        f"[{timestamp}] probe source={diagnostic['source']} path={diagnostic['path']} "
                        f"compatible={diagnostic['compatible']} detail="
                        f"{diagnostic.get('version') or diagnostic.get('reason')}\n"
                    ).encode()
                )
            log_handle.flush()
            child_output_start = log_handle.tell()
            # Stay in the uidata operation's process group so cancelling the native
            # screen also terminates the browser it owns.
            try:
                process = popen(
                    argv,
                    stdin=subprocess.DEVNULL,
                    stdout=log_handle,
                    stderr=subprocess.STDOUT,
                    env=environment,
                )
            except OSError as exc:
                log_handle.write(
                    f"[{datetime.now(timezone.utc).isoformat()}] browser spawn failed: {exc}\n".encode()
                )
                raise RuntimeError(f"Open Here browser could not start; see {log_path}") from exc
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
            returncode = process.poll()
            if returncode not in (None, 0):
                log_handle.flush()
                log_handle.seek(child_output_start)
                child_output = log_handle.read().decode(errors="replace")
                log_handle.seek(0, os.SEEK_END)
                log_handle.write(
                    f"[{datetime.now(timezone.utc).isoformat()}] browser exited status={returncode}\n".encode()
                )
                if (
                    not allow_no_sandbox
                    and ownership == "user-installed"
                    and "running as root without --no-sandbox is not supported"
                    in child_output.casefold()
                ):
                    raise RuntimeError(
                        "The user-installed Chrome runtime refused to run as root with "
                        "its sandbox. Open Here did not disable it. You may explicitly "
                        "choose 'Open Here Without Sandbox', but that removes Chromium "
                        f"process isolation for this session. See {log_path}"
                    )
                raise RuntimeError(
                    f"Open Here browser exited with status {returncode}; see {log_path}"
                )
    return {
        "closed": True,
        "browser": executable,
        "browser_ownership": ownership,
        "launch_mechanism": argv[:-1],
        "log": str(log_path),
    }
