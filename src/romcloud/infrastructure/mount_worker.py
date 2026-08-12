"""Background mount worker — the boot-safe, non-blocking half of the SMB
mount integration.

Core rule: **"ROMCloud may fail; Batocera must not."** Real-hardware testing
showed that calling the blocking mount logic (:mod:`romcloud.infrastructure.mount`)
directly from a Batocera custom service's ``start`` action can hang/disrupt
boot — the service framework runs ``start`` synchronously as part of the
boot sequence, and DNS/network/Tailscale/CIFS can all take a long time (or
never) to become ready.

The fix implemented here: the service's ``start`` action
(``romcloud mount boot-start`` — see :mod:`romcloud.cli.commands.mount``)
never waits on anything. It only:

1. Checks whether the source is already mounted (fast, local) — done if so.
2. Checks whether a worker is already running (fast, local) — done if so.
3. Spawns *this module's* worker (``romcloud mount worker``) as a fully
   detached background process (new session, stdin closed, stdout/stderr to
   a ROMCloud-owned log file) and returns immediately, without waiting for
   it.

The worker itself does the actual waiting (bounded by a finite timeout —
never indefinite) and mounting, entirely in the background, guarded by an
atomic single-instance lock (``os.O_CREAT | os.O_EXCL`` — no ``flock(1)`` or
other external tool required, so it works on a stripped-down Batocera).
Any failure (missing NAS, wrong password, DNS/network/Tailscale down,
mount.cifs failure, malformed config) is caught, logged clearly (never with
credentials), and results in a clean worker exit — never a crash, never a
hang, never anything visible to Batocera's boot sequence.

This module owns exactly these ROMCloud-specific files under
``{romcloud_home}``:

- ``run/mount-worker.pid``            — single-instance lock (contains the PID)
- ``run/mount-worker.status.json``    — last known state, for diagnostics
- ``run/mount-endpoint-cache.json``   — last resolved endpoint, a boot-time
  fast-path hint only (see :mod:`romcloud.infrastructure.mount_endpoint_cache`)
- ``logs/mount-worker.log``           — the spawned worker's stdout/stderr
"""

from __future__ import annotations

import errno
import json
import os
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from romcloud.core.exceptions import ConfigurationError, ROMCloudError, ProviderNotReachableError
from romcloud.infrastructure.config import SMBConfig, paths_overlap
from romcloud.infrastructure import mount as mountlib
from romcloud.infrastructure import mount_endpoint_cache
from romcloud.infrastructure import credentials
from romcloud.infrastructure.credentials import (
    cifs_credentials_path,
    credential_lock_state,
    load_remote_data_smb_password,
    load_smb_password,
    remote_data_cifs_credentials_path,
)
from romcloud.infrastructure.logging import get_logger

log = get_logger("mount.worker")

# Overall boot retry budget the detached worker owns end-to-end.
DEFAULT_RETRY_TIMEOUT = 300.0
DEFAULT_RETRY_INITIAL_DELAY = 2.0
DEFAULT_RETRY_MAX_DELAY = 30.0

# A single mount/reachability attempt must stay short so the retry loop can
# actually retry within the overall budget above — never equal to it.
DEFAULT_ATTEMPT_TIMEOUT = 8.0
DEFAULT_ATTEMPT_INTERVAL = 2.0


@dataclass(frozen=True)
class ConfiguredMount:
    """One local view of the configured SMB share."""

    label: str
    mount_point: str
    read_only: bool
    smb: SMBConfig
    credential_kind: str


def configured_mounts(config) -> tuple[ConfiguredMount, ...]:
    """Return the intentional SMB mount views used by ROMCloud.

    The catalog/cache view stays read-only. General remote data gets its own
    independently configured read-write target and credentials.
    """
    targets: list[ConfiguredMount] = []
    if config.smb is not None:
        targets.append(
            ConfiguredMount(
                "ROM catalog", config.source.rom_root, True, config.smb, "source"
            )
        )

    remote_data = getattr(config, "remote_data", None)
    if remote_data is not None and remote_data.provider == "smb":
        if remote_data.smb is None:
            raise ConfigurationError("SMB remote data requires its own SMB target")
        remote_mount = Path(remote_data.root)
        if config.smb is not None:
            rom_mount = Path(config.source.rom_root)
            if paths_overlap(remote_mount, rom_mount):
                raise ConfigurationError(
                    "remote_data.root must be separate from source.rom_root so the "
                    "ROM catalog mount can remain read-only"
                )
        targets.append(
            ConfiguredMount(
                "ROMCloud remote data",
                remote_data.root,
                False,
                remote_data.smb,
                "remote_data",
            )
        )
    return tuple(targets)


def all_configured_mounts_are_mounted(config) -> bool:
    targets = configured_mounts(config)
    return bool(targets) and all(_configured_mount_is_ready(item) for item in targets)


def _configured_mount_is_ready(item: ConfiguredMount) -> bool:
    return mountlib.is_target_mounted_cifs(
        item.mount_point,
        # The source worker may deliberately use its cached IP alias. The
        # share still identifies the configured source. Writable remote data
        # has no alias fast path, so require its exact configured server too.
        server=None if item.read_only else item.smb.server,
        share=item.smb.share,
        read_only=item.read_only,
        remote_path=getattr(item.smb, "remote_path", ""),
    )


def credentials_for_mount(config, target: ConfiguredMount) -> Optional[str]:
    """Resolve the independently stored password for a mount.

    Raises :class:`ConfigurationError` (never with the password itself)
    when a credential envelope exists but cannot be decrypted on this
    hardware — distinct from simply having no password configured, so
    callers can surface an accurate recovery message instead of the
    generic "no password stored" one.
    """
    section = "smb" if target.credential_kind == "source" else "remote_data_smb"
    password = (
        load_smb_password(config.credentials_path)
        if target.credential_kind == "source"
        else load_remote_data_smb_password(config.credentials_path)
    )
    if password is None and credential_lock_state(config.credentials_path, section) == "locked":
        raise ConfigurationError(
            f"Stored network credentials for {target.label} cannot be unlocked on this "
            "hardware. Run `romcloud configure` to re-enter the SMB password."
        )
    return password


def mount_configured_target(
    config,
    target: ConfiguredMount,
    password: str,
    *,
    mount_fn: Optional[Callable[..., "mountlib.MountOutcome"]] = None,
    **mount_kwargs,
) -> "mountlib.MountOutcome":
    """Mount *target* using a short-lived CIFS credentials file that is
    always removed afterward, regardless of outcome — see
    :func:`romcloud.infrastructure.credentials.ephemeral_cifs_credentials_file`.
    """
    mount_fn = mount_fn or mountlib.mount_cifs_source
    directory = config.credentials_path.parent
    prefix = f".romcloud-cifs-{target.credential_kind}-"
    with credentials.ephemeral_cifs_credentials_file(
        directory, target.smb.username, password, prefix=prefix
    ) as creds_path:
        return mount_fn(
            server=target.smb.server,
            share=target.smb.share,
            mount_point=target.mount_point,
            credentials_path=creds_path,
            read_only=target.read_only,
            remote_path=getattr(target.smb, "remote_path", ""),
            port=target.smb.port,
            **mount_kwargs,
        )


# ── path helpers ──────────────────────────────────────────────────────────────


def romcloud_home_from_config(config) -> Path:
    """Derive ``ROMCLOUD_HOME`` (e.g. ``/userdata/system/romcloud``) from the
    loaded config — mirrors ``AppConfig.credentials_path``'s derivation."""
    return Path(config.data_path).parent


def run_dir(romcloud_home: Path) -> Path:
    return romcloud_home / "run"


def lock_path(romcloud_home: Path) -> Path:
    return run_dir(romcloud_home) / "mount-worker.pid"


def status_path(romcloud_home: Path) -> Path:
    return run_dir(romcloud_home) / "mount-worker.status.json"


def worker_log_path(romcloud_home: Path) -> Path:
    return romcloud_home / "logs" / "mount-worker.log"


# ── status persistence (diagnostics) ─────────────────────────────────────────


@dataclass(frozen=True)
class WorkerStatusInfo:
    state: str  # "waiting" | "success" | "failed"
    timestamp: str
    detail: str = ""


def read_worker_status(romcloud_home: Path) -> Optional[WorkerStatusInfo]:
    path = status_path(romcloud_home)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return WorkerStatusInfo(
        state=str(data.get("state", "unknown")),
        timestamp=str(data.get("timestamp", "")),
        detail=str(data.get("detail", "")),
    )


def _write_worker_status(romcloud_home: Path, state: str, detail: str = "") -> None:
    path = status_path(romcloud_home)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "state": state,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "detail": detail,
    }
    try:
        path.write_text(json.dumps(payload) + "\n", encoding="utf-8")
    except OSError:
        # Diagnostics are best-effort; never let a status write failure
        # affect the worker's actual outcome.
        log.warning("Could not write worker status file at %s", path)


# ── single-instance lock, with stale-lock recovery ───────────────────────────


class WorkerAlreadyRunning(Exception):
    """Raised internally when another worker already holds the lock."""


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, just owned by someone else
    except OSError as exc:
        if exc.errno == errno.ESRCH:
            return False
        return True
    return True


def _worker_cmdline_matches(pid: int, *, proc_root: Path) -> bool:
    """Return True when *pid* looks like ROMCloud's mount worker process."""
    cmdline_path = proc_root / str(pid) / "cmdline"
    try:
        raw = cmdline_path.read_bytes()
    except OSError:
        return False

    argv = [part.decode("utf-8", errors="replace") for part in raw.split(b"\0") if part]
    if len(argv) < 4:
        return False
    return argv[1:] == ["-m", "romcloud.cli.main", "mount", "worker"]


def is_worker_running(romcloud_home: Path, *, proc_root: Path = Path("/proc")) -> Optional[int]:
    """Return the running worker's PID, or None.

    If the lock file references a PID that's no longer alive (a stale lock
    left behind by a crash/reboot), it is removed automatically — recovery
    is always safe since a dead PID can never be misidentified as "our"
    live process.
    """
    path = lock_path(romcloud_home)
    if not path.exists():
        return None

    try:
        raw = path.read_text(encoding="utf-8").strip()
        pid = int(raw)
    except (OSError, ValueError):
        log.info("Removing unreadable/corrupt mount-worker lock at %s", path)
        path.unlink(missing_ok=True)
        return None

    if _pid_alive(pid) and _worker_cmdline_matches(pid, proc_root=proc_root):
        return pid

    log.info("Removing stale mount-worker lock (pid %d is no longer our worker)", pid)
    path.unlink(missing_ok=True)
    return None


class _WorkerLock:
    """Atomic (``O_CREAT|O_EXCL``) PID lock file — no ``flock(1)`` dependency,
    so this works even on a stripped-down Batocera image."""

    def __init__(self, romcloud_home: Path) -> None:
        self._romcloud_home = romcloud_home
        self._path = lock_path(romcloud_home)
        self._acquired = False

    def __enter__(self) -> "_WorkerLock":
        self._path.parent.mkdir(parents=True, exist_ok=True)
        # One stale-lock recovery pass before attempting to acquire it.
        is_worker_running(self._romcloud_home)
        try:
            fd = os.open(str(self._path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except FileExistsError:
            raise WorkerAlreadyRunning() from None
        with os.fdopen(fd, "w") as fh:
            fh.write(str(os.getpid()))
        self._acquired = True
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        if self._acquired:
            self._path.unlink(missing_ok=True)
        return False


def stop_worker(
    romcloud_home: Path,
    *,
    grace_period: float = 3.0,
    poll_interval: float = 0.2,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> bool:
    """Terminate a running worker (if any) and clean up its lock file.

    SIGTERM first, then SIGKILL if it hasn't exited within *grace_period*.
    Returns True if a (live) worker was found and stopped.
    """
    pid = is_worker_running(romcloud_home)
    if pid is None:
        return False

    try:
        _signal_worker_process_group(pid, signal.SIGTERM)
    except ProcessLookupError:
        lock_path(romcloud_home).unlink(missing_ok=True)
        return False

    deadline = clock() + grace_period
    while clock() < deadline:
        if not _pid_alive(pid):
            break
        sleep(poll_interval)
    else:
        try:
            _signal_worker_process_group(
                pid, getattr(signal, "SIGKILL", signal.SIGTERM)
            )
        except (ProcessLookupError, PermissionError):
            pass

    lock_path(romcloud_home).unlink(missing_ok=True)
    log.info("Stopped mount worker (pid %d)", pid)
    return True


def _signal_worker_process_group(pid: int, sig: int) -> None:
    """Signal the detached worker and every mount/DNS child it owns."""
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


def cleanup_runtime_state(romcloud_home: Path) -> None:
    """Remove ROMCloud's own transient runtime files (lock + status +
    cached endpoint hint).

    Deliberately does not remove the worker log file — it's useful
    diagnostic history, not live state, and keeping it costs nothing.
    Never touches anything outside ``run/``.
    """
    lock_path(romcloud_home).unlink(missing_ok=True)
    status_path(romcloud_home).unlink(missing_ok=True)
    mount_endpoint_cache.endpoint_cache_path(romcloud_home).unlink(missing_ok=True)


# ── spawning the detached background worker ──────────────────────────────────


def spawn_worker(
    romcloud_home: Path,
    *,
    python_executable: Optional[str] = None,
    popen: Callable[..., "subprocess.Popen"] = subprocess.Popen,
) -> int:
    """Launch ``romcloud mount worker`` as a fully detached background
    process and return its PID immediately — never waits for it.

    Uses ``sys.executable`` (the current venv's own python) by default, so
    this has no dependency on the ``romcloud`` wrapper being on ``PATH``.
    """
    run_dir(romcloud_home).mkdir(parents=True, exist_ok=True)
    log_path = worker_log_path(romcloud_home)
    log_path.parent.mkdir(parents=True, exist_ok=True)

    executable = python_executable or sys.executable
    log_file = open(log_path, "ab")
    try:
        proc = popen(
            [executable, "-m", "romcloud.cli.main", "mount", "worker"],
            stdin=subprocess.DEVNULL,
            stdout=log_file,
            stderr=log_file,
            start_new_session=True,
        )
    finally:
        log_file.close()
    return proc.pid


# ── the worker loop itself (runs inside the detached background process) ────


def run_worker(
    romcloud_home: Path,
    config,
    *,
    attempt_timeout: float = DEFAULT_ATTEMPT_TIMEOUT,
    attempt_interval: float = DEFAULT_ATTEMPT_INTERVAL,
    retry_timeout: float = DEFAULT_RETRY_TIMEOUT,
    retry_initial_delay: float = DEFAULT_RETRY_INITIAL_DELAY,
    retry_max_delay: float = DEFAULT_RETRY_MAX_DELAY,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
    stop_event: threading.Event | None = None,
) -> int:
    """Run the mount worker to completion — this call *blocks*, and is meant
    to execute only inside a detached background process (see
    :func:`spawn_worker`), never on Batocera's boot path directly.

    Never raises: every failure mode (no [smb] section, no password, DNS/
    network/Tailscale down, mount.cifs failure, unexpected error) is caught,
    logged clearly (never with credentials), recorded to the status file,
    and results in a clean return — "the worst result is cloud games
    unavailable."
    """
    try:
        with _WorkerLock(romcloud_home):
            return _run_worker_locked(
                romcloud_home,
                config,
                attempt_timeout,
                attempt_interval,
                retry_timeout,
                retry_initial_delay,
                retry_max_delay,
                sleep,
                clock,
                stop_event,
            )
    except WorkerAlreadyRunning:
        log.info("A mount worker is already running — exiting without doing anything.")
        return 0

def _run_worker_locked(
    romcloud_home: Path,
    config,
    attempt_timeout: float,
    attempt_interval: float,
    retry_timeout: float,
    retry_initial_delay: float,
    retry_max_delay: float,
    sleep: Callable[[float], None],
    clock: Callable[[], float],
    stop_event: threading.Event | None,
) -> int:
    try:
        targets = configured_mounts(config)
        if not targets:
            _write_worker_status(romcloud_home, "failed", "no SMB mounts configured")
            log.warning("Mount worker started but no SMB mounts are configured — exiting.")
            return 0
        if all(_configured_mount_is_ready(item) for item in targets):
            _write_worker_status(romcloud_home, "success", "already mounted")
            log.info("All configured SMB mount views are already mounted — worker exiting.")
            return 0

        details: list[str] = []
        for target in targets:
            if stop_event is not None and stop_event.is_set():
                raise ProviderNotReachableError(
                    "Mount worker cancelled because system shutdown was requested"
                )
            if _configured_mount_is_ready(target):
                details.append(f"{target.label}: already mounted")
                continue
            password = credentials_for_mount(config, target)
            if not password:
                raise ConfigurationError(
                    f"No SMB password stored for {target.label}; run `romcloud configure`"
                )
            _write_worker_status(
                romcloud_home,
                "waiting",
                f"waiting for {target.label} at {target.smb.server}:{target.smb.port}",
            )
            directory = config.credentials_path.parent
            prefix = f".romcloud-cifs-{target.credential_kind}-"
            with credentials.ephemeral_cifs_credentials_file(
                directory, target.smb.username, password, prefix=prefix
            ) as creds_path:
                outcome = _mount_with_cached_endpoint_fallback(
                    romcloud_home=romcloud_home,
                    server=target.smb.server,
                    share=target.smb.share,
                    mount_point=target.mount_point,
                    credentials_path=creds_path,
                    read_only=target.read_only,
                    port=target.smb.port,
                    remote_path=getattr(target.smb, "remote_path", ""),
                    use_endpoint_cache=target.credential_kind == "source",
                    attempt_timeout=attempt_timeout,
                    attempt_interval=attempt_interval,
                    retry_timeout=retry_timeout,
                    retry_initial_delay=retry_initial_delay,
                    retry_max_delay=retry_max_delay,
                    sleep=sleep,
                    clock=clock,
                    stop_event=stop_event,
                )
            details.append(f"{target.label}: {outcome.detail}")
        detail = "; ".join(details)
        _write_worker_status(romcloud_home, "success", detail)
        log.info("Mount worker succeeded: %s", detail)
        return 0

    except ROMCloudError as exc:
        # Exceptions raised anywhere in romcloud.infrastructure.mount are
        # already credential-safe (the password is never embedded in them).
        _write_worker_status(romcloud_home, "failed", str(exc))
        log.error("Mount worker failed: %s", exc)
        return 0
    except Exception as exc:  # noqa: BLE001 — the worker must never crash loudly
        _write_worker_status(romcloud_home, "failed", f"unexpected error: {exc}")
        log.exception("Mount worker crashed unexpectedly")
        return 0


def _mount_with_cached_endpoint_fallback(
    *,
    romcloud_home: Path,
    server: str,
    share: str,
    mount_point: str,
    credentials_path: Path,
    read_only: bool = True,
    remote_path: str = "",
    use_endpoint_cache: bool = True,
    port: int,
    attempt_timeout: float,
    attempt_interval: float,
    retry_timeout: float,
    retry_initial_delay: float,
    retry_max_delay: float,
    sleep: Callable[[float], None],
    clock: Callable[[], float],
    stop_event: threading.Event | None = None,
) -> mountlib.MountOutcome:
    """Try a cached resolved endpoint first, then fall back to the existing
    bounded hostname retry loop — the ultimate reliability safety net.

    A cached endpoint is purely a boot-time UX optimization: it is only
    tried once, for a single short ``attempt_timeout`` window, and is never
    allowed to consume any of the hostname retry budget below. If it's
    missing, for a different configured server, or simply doesn't work, it
    is skipped/ignored silently and mounting proceeds exactly as before
    this feature existed.
    """
    cached = (
        mount_endpoint_cache.read_endpoint_cache(romcloud_home)
        if use_endpoint_cache
        else None
    )
    if cached is not None and cached.server == server and cached.endpoint != server:
        log.info(
            "Trying cached endpoint %s for %r before hostname resolution",
            cached.endpoint, server,
        )
        try:
            mount_kwargs = {}
            if not read_only:
                mount_kwargs["read_only"] = False
            if remote_path:
                mount_kwargs["remote_path"] = remote_path
            outcome = mountlib.mount_cifs_source(
                server=cached.endpoint,
                share=share,
                mount_point=mount_point,
                credentials_path=credentials_path,
                port=port,
                wait_timeout=attempt_timeout,
                wait_interval=attempt_interval,
                cancel_event=stop_event,
                **mount_kwargs,
            )
            log.info("Mount worker succeeded via cached endpoint %s", cached.endpoint)
            if use_endpoint_cache:
                mount_endpoint_cache.write_endpoint_cache(
                    romcloud_home, server, cached.endpoint
                )
            return outcome
        except ROMCloudError as exc:
            log.info(
                "Cached endpoint %s did not work (%s) — falling back to %r",
                cached.endpoint, exc, server,
            )

    outcome = _mount_with_retry(
        server=server,
        share=share,
        mount_point=mount_point,
        credentials_path=credentials_path,
        read_only=read_only,
        remote_path=remote_path,
        port=port,
        attempt_timeout=attempt_timeout,
        attempt_interval=attempt_interval,
        retry_timeout=retry_timeout,
        retry_initial_delay=retry_initial_delay,
        retry_max_delay=retry_max_delay,
        sleep=sleep,
        clock=clock,
        stop_event=stop_event,
    )
    # A fresh resolution here is only a *candidate* (see
    # mount_endpoint_cache.resolve_endpoint's docstring) — it is not
    # provably the exact address the mount above used internally. Verify it
    # is independently reachable right now before trusting it for next
    # boot's fast path; an unreachable candidate is worse than no hint.
    if stop_event is not None and stop_event.is_set():
        return outcome
    resolved = mount_endpoint_cache.resolve_endpoint(server, port) if use_endpoint_cache else None
    if resolved and resolved != server and mountlib.check_reachable(resolved, port).ok:
        mount_endpoint_cache.write_endpoint_cache(romcloud_home, server, resolved)
    return outcome


def _mount_with_retry(
    *,
    server: str,
    share: str,
    mount_point: str,
    credentials_path: Path,
    read_only: bool = True,
    remote_path: str = "",
    port: int,
    attempt_timeout: float,
    attempt_interval: float,
    retry_timeout: float,
    retry_initial_delay: float,
    retry_max_delay: float,
    sleep: Callable[[float], None],
    clock: Callable[[], float],
    stop_event: threading.Event | None = None,
) -> mountlib.MountOutcome:
    """Retry a single mount/reachability attempt within an overall budget.

    The detached worker owns ``retry_timeout`` end-to-end. Each individual
    attempt is capped at ``min(attempt_timeout, <time remaining in the
    overall budget>)`` so no single attempt can ever consume the whole
    budget — that was the root cause of the boot-time bug this guards
    against.
    """
    deadline = clock() + retry_timeout
    attempt = 1
    delay = max(0.0, retry_initial_delay)

    while True:
        if stop_event is not None and stop_event.is_set():
            raise ProviderNotReachableError(
                "Mount worker cancelled because system shutdown was requested"
            )
        remaining = deadline - clock()
        if remaining <= 0:
            message = (
                f"Timed out after {attempt - 1} attempt(s) waiting for SMB target {server!r} "
                f"to become reachable: retry budget exhausted"
            )
            log.warning(
                "Mount worker retry budget exhausted before attempt %d could start", attempt
            )
            raise ProviderNotReachableError(message)

        per_attempt_timeout = min(attempt_timeout, remaining)
        log.info(
            "Mount worker attempt %d: mounting //%s/%s at %s (timeout %.1fs)",
            attempt,
            server,
            share,
            mount_point,
            per_attempt_timeout,
        )
        try:
            mount_kwargs = {}
            if not read_only:
                mount_kwargs["read_only"] = False
            if remote_path:
                mount_kwargs["remote_path"] = remote_path
            outcome = mountlib.mount_cifs_source(
                server=server,
                share=share,
                mount_point=mount_point,
                credentials_path=credentials_path,
                port=port,
                wait_timeout=per_attempt_timeout,
                wait_interval=attempt_interval,
                cancel_event=stop_event,
                **mount_kwargs,
            )
            if attempt > 1:
                log.info("Mount worker succeeded after %d attempts: %s", attempt, outcome.detail)
            return outcome
        except ProviderNotReachableError as exc:
            now = clock()
            if now >= deadline:
                message = (
                    f"Timed out after {attempt} attempt(s) waiting for SMB target {server!r} "
                    f"to become reachable: {exc}"
                )
                log.warning("Mount worker retry budget exhausted after %d attempt(s): %s", attempt, exc)
                raise ProviderNotReachableError(message) from exc

            sleep_for = min(delay, max(0.0, deadline - now))
            log.info(
                "Mount worker attempt %d failed because the source is not ready: %s; retrying in %.1fs",
                attempt,
                exc,
                sleep_for,
            )
            if stop_event is not None:
                if stop_event.wait(sleep_for):
                    raise ProviderNotReachableError(
                        "Mount worker cancelled because system shutdown was requested"
                    ) from exc
            else:
                sleep(sleep_for)
            delay = min(max(delay * 2, retry_initial_delay if retry_initial_delay > 0 else delay), retry_max_delay)
            attempt += 1


# ── combined diagnostics (for `mount status` / `healthcheck`) ───────────────


@dataclass(frozen=True)
class MountDiagnostics:
    configured: bool
    mounted: bool
    worker_pid: Optional[int]
    last_state: Optional[str]
    last_detail: str
    last_timestamp: str
    cached_endpoint: Optional[str] = None
    """The last resolved endpoint used for fast boot-time mounting, if any
    — diagnostic-only; never part of the user-facing source identity."""
    remote_data_mounted: Optional[bool] = None
    source_mounted: Optional[bool] = None

    @property
    def label(self) -> str:
        if not self.configured:
            return "not configured"
        if self.mounted:
            return "mounted"
        source_mounted = self.mounted if self.source_mounted is None else self.source_mounted
        if source_mounted and self.remote_data_mounted is False:
            return "ROM source mounted — remote-data write mount missing"
        if self.remote_data_mounted and source_mounted is False:
            return "Remote-data write mount mounted — ROM source missing"
        if self.worker_pid is not None:
            return "waiting for source (worker running)"
        if self.last_state == "failed":
            return "not mounted — last attempt failed"
        if self.last_state == "waiting":
            return "not mounted — worker exited while waiting"
        return "not mounted"


def get_diagnostics(romcloud_home: Path, config) -> MountDiagnostics:
    """Combine live mount state, worker liveness, and last recorded status
    into a single, clear diagnostic snapshot."""
    targets = configured_mounts(config)
    if not targets:
        return MountDiagnostics(
            configured=False, mounted=False, worker_pid=None,
            last_state=None, last_detail="", last_timestamp="",
        )

    source_target = next((item for item in targets if item.credential_kind == "source"), None)
    remote_target = next((item for item in targets if item.credential_kind == "remote_data"), None)
    source_mounted = _configured_mount_is_ready(source_target) if source_target is not None else None
    remote_data_mounted = (
        _configured_mount_is_ready(remote_target) if remote_target is not None else None
    )
    mounted = (source_mounted is not False) and (remote_data_mounted is not False)
    worker_pid = is_worker_running(romcloud_home)
    status = read_worker_status(romcloud_home)
    cached = mount_endpoint_cache.read_endpoint_cache(romcloud_home)
    cached_endpoint = (
        cached.endpoint
        if cached is not None and config.smb is not None and cached.server == config.smb.server
        else None
    )

    return MountDiagnostics(
        configured=True,
        mounted=mounted,
        worker_pid=worker_pid,
        last_state=status.state if status else None,
        last_detail=status.detail if status else "",
        last_timestamp=status.timestamp if status else "",
        cached_endpoint=cached_endpoint,
        remote_data_mounted=remote_data_mounted,
        source_mounted=source_mounted,
    )
