"""Mounted-source management — generic Linux/CIFS mount handling.

Deliberately independent of :class:`~romcloud.core.storage.StorageProvider`:
today's hardware source is a CIFS share mounted at a local path and read by
:class:`~romcloud.infrastructure.providers.local.LocalFilesystemProvider` — mounting is
an *operational* concern (is the share attached to the filesystem yet?), not
a *storage-provider* concern (how do I list/read files?). Keeping the two
separated means a future native ``SMBProvider`` (talking SMB directly, no
mount required) can replace this module entirely without any change to
:mod:`~romcloud.integrations.batocera.catalog` or :mod:`~romcloud.services.cache`.

Batocera 42 does not ship the ``mountpoint`` command, so mount-state
detection is done by parsing ``/proc/mounts`` — a facility guaranteed to
exist on any Linux kernel, unlike desktop-oriented utilities.

Credentials are never placed on the mount command line (which would leak
into ``ps`` output and any process-list logging). Instead a ``mount.cifs``
``-o credentials=<file>`` file is used — see
:func:`romcloud.infrastructure.credentials.write_cifs_credentials_file`.
Nothing in this module ever logs or raises with a password embedded in the
message.
"""

from __future__ import annotations

import ipaddress
import json
import os
import socket
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from romcloud.core.exceptions import MountError, ProviderAuthError, ProviderNotReachableError
from romcloud.infrastructure.logging import get_logger

log = get_logger("mount")

_DEFAULT_PROC_MOUNTS = "/proc/mounts"
_DEFAULT_SMB_PORT = 445

_AUTH_FAILURE_MARKERS = (
    "permission denied",
    "logon failure",
    "access denied",
    "mount error(13)",
)
_NETWORK_FAILURE_MARKERS = (
    "no route to host",
    "network is unreachable",
    "connection timed out",
    "host is down",
    "mount error(101)",
    "mount error(110)",
    "mount error(112)",
)


# ── mount-state detection ────────────────────────────────────────────────────


def _parse_mounted_targets(mounts_text: str) -> set[str]:
    """Return the set of mount-point paths listed in `/proc/mounts` content."""
    targets: set[str] = set()
    for line in mounts_text.splitlines():
        parts = line.split()
        if len(parts) < 2:
            continue
        # /proc/mounts escapes spaces etc. as octal (e.g. \040); undo it.
        targets.add(parts[1].encode().decode("unicode_escape"))
    return targets


def _mount_options(target: str, mounts_text: str) -> Optional[set[str]]:
    """Return mount options for *target*, or ``None`` when it is not mounted."""
    normalized = os.path.normpath(target)
    for line in mounts_text.splitlines():
        parts = line.split()
        if len(parts) < 4:
            continue
        mount_target = parts[1].encode().decode("unicode_escape")
        if os.path.normpath(mount_target) == normalized:
            return set(parts[3].split(","))
    return None


def _mount_record(target: str, mounts_text: str) -> Optional[tuple[str, str, set[str]]]:
    """Return ``(source, filesystem_type, options)`` for a mounted target."""
    normalized = os.path.normpath(target)
    for line in mounts_text.splitlines():
        parts = line.split()
        if len(parts) < 4:
            continue
        mount_target = parts[1].encode().decode("unicode_escape")
        if os.path.normpath(mount_target) == normalized:
            source = parts[0].encode().decode("unicode_escape")
            return source, parts[2].lower(), set(parts[3].split(","))
    return None


def is_mounted(target: str, mounts_text: str) -> bool:
    """Pure check: is *target* listed as a mount point in *mounts_text*?

    *mounts_text* is the raw content of `/proc/mounts` (or an equivalent),
    passed in explicitly so this is trivially unit-testable.
    """
    normalized = os.path.normpath(target)
    return normalized in _parse_mounted_targets(mounts_text)


def is_target_mounted(target: str, *, proc_mounts_path: str = _DEFAULT_PROC_MOUNTS) -> bool:
    """Read `/proc/mounts` (or *proc_mounts_path*) and check if *target* is mounted."""
    try:
        with open(proc_mounts_path, "r", encoding="utf-8") as fh:
            mounts_text = fh.read()
    except OSError as exc:
        raise MountError(f"Cannot read {proc_mounts_path}: {exc}") from exc
    return is_mounted(target, mounts_text)


def is_mounted_writable(target: str, mounts_text: str) -> bool:
    """Return True only when *target* is mounted with the ``rw`` option."""
    options = _mount_options(target, mounts_text)
    return options is not None and "rw" in options


def is_target_mounted_writable(
    target: str, *, proc_mounts_path: str = _DEFAULT_PROC_MOUNTS
) -> bool:
    """Read the mount table and require a real read-write mount at *target*."""
    try:
        with open(proc_mounts_path, "r", encoding="utf-8") as fh:
            mounts_text = fh.read()
    except OSError as exc:
        raise MountError(f"Cannot read {proc_mounts_path}: {exc}") from exc
    return is_mounted_writable(target, mounts_text)


def is_target_mounted_read_only(
    target: str, *, proc_mounts_path: str = _DEFAULT_PROC_MOUNTS
) -> bool:
    """Read the mount table and require a real read-only mount at *target*."""
    try:
        with open(proc_mounts_path, "r", encoding="utf-8") as fh:
            mounts_text = fh.read()
    except OSError as exc:
        raise MountError(f"Cannot read {proc_mounts_path}: {exc}") from exc
    options = _mount_options(target, mounts_text)
    return options is not None and "ro" in options


def is_mounted_cifs_target(
    target: str,
    mounts_text: str,
    *,
    server: Optional[str] = None,
    share: Optional[str] = None,
    read_only: Optional[bool] = None,
    remote_path: Optional[str] = None,
) -> bool:
    """Require a CIFS mount at *target* with the expected identity and mode.

    ``server=None`` accepts any server spelling while still requiring the
    expected share. The read-only ROM source uses that form because its boot
    fast path may mount through a cached IP address. Writable remote data
    always supplies both server and share.
    """
    record = _mount_record(target, mounts_text)
    if record is None:
        return False
    source, filesystem_type, options = record
    if filesystem_type != "cifs" or not source.startswith("//"):
        return False
    try:
        mounted_server, mounted_share = source[2:].split("/", 1)
    except ValueError:
        return False
    if server is not None and mounted_server.casefold() != server.casefold():
        return False
    if (
        share is not None
        and mounted_share.rstrip("/").casefold() != share.rstrip("/").casefold()
    ):
        return False
    if read_only is True and "ro" not in options:
        return False
    if read_only is False and "rw" not in options:
        return False
    if remote_path:
        expected_prefix = f"prefixpath={remote_path}"
        if expected_prefix not in options:
            return False
    return True


def is_target_mounted_cifs(
    target: str,
    *,
    server: Optional[str] = None,
    share: Optional[str] = None,
    read_only: Optional[bool] = None,
    remote_path: Optional[str] = None,
    proc_mounts_path: str = _DEFAULT_PROC_MOUNTS,
) -> bool:
    """Read the mount table and apply :func:`is_mounted_cifs_target`."""
    try:
        with open(proc_mounts_path, "r", encoding="utf-8") as fh:
            mounts_text = fh.read()
    except OSError as exc:
        raise MountError(f"Cannot read {proc_mounts_path}: {exc}") from exc
    return is_mounted_cifs_target(
        target,
        mounts_text,
        server=server,
        share=share,
        read_only=read_only,
        remote_path=remote_path,
    )


# ── reachability ──────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ReachabilityResult:
    ok: bool
    stage: str  # "dns" | "tcp" | "cancelled" | "ok"
    detail: str


def _terminate_probe_process(process: "subprocess.Popen[str]") -> None:
    """Best-effort, bounded cleanup for a DNS helper process."""
    try:
        process.kill()
    except OSError:
        return
    try:
        process.wait(timeout=0.2)
    except (OSError, subprocess.TimeoutExpired):
        # A kernel-stuck child must never make its caller wait forever. It
        # inherits the ROMCloud operation/worker process group, so GUI or
        # system shutdown can still terminate the entire owned group.
        pass


def _resolve_default_bounded(host: str, port: int, timeout: float) -> list[object]:
    """Resolve through a short-lived owned process so libc DNS is abandonable."""
    script = (
        "import json,socket,sys\n"
        "try:\n"
        " print(json.dumps(socket.getaddrinfo(sys.argv[1], int(sys.argv[2]), "
        "type=socket.SOCK_STREAM)))\n"
        "except OSError as exc:\n"
        " print(str(exc), file=sys.stderr)\n"
        " raise SystemExit(1)\n"
    )
    try:
        process = subprocess.Popen(
            [sys.executable, "-c", script, host, str(port)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    except OSError as exc:
        raise OSError(f"could not start bounded DNS resolver: {exc}") from exc
    try:
        stdout, stderr = process.communicate(timeout=max(0.01, timeout))
    except subprocess.TimeoutExpired as exc:
        _terminate_probe_process(process)
        raise TimeoutError(f"DNS resolution timed out after {timeout:.1f}s") from exc
    if process.returncode != 0:
        raise OSError((stderr or "DNS resolver failed").strip())
    try:
        result = json.loads(stdout)
    except (TypeError, json.JSONDecodeError) as exc:
        raise OSError("DNS resolver returned an invalid response") from exc
    if not isinstance(result, list) or not result:
        raise OSError("DNS resolver returned no addresses")
    return result


def _resolve_in_daemon_thread(
    resolver: Callable[[str, int], object], host: str, port: int, timeout: float
) -> object:
    """Bound an injected resolver that cannot generally be process-serialized.

    Production ``socket.getaddrinfo`` uses the owned-process path above.
    This test/extension seam is explicitly safe for a short-lived backend
    process to abandon because its thread is daemonized.
    """
    completed = threading.Event()
    outcome: list[object] = []

    def resolve() -> None:
        try:
            outcome.append(resolver(host, port))
        except BaseException as exc:  # noqa: BLE001 - transferred to caller
            outcome.append(exc)
        finally:
            completed.set()

    threading.Thread(target=resolve, name="romcloud-dns-probe", daemon=True).start()
    if not completed.wait(max(0.01, timeout)):
        raise TimeoutError(f"DNS resolution timed out after {timeout:.1f}s")
    result = outcome[0]
    if isinstance(result, BaseException):
        raise result
    return result


def resolve_addresses_bounded(
    host: str,
    port: int,
    *,
    timeout: float = 3.0,
    resolver: Callable[[str, int], object] = socket.getaddrinfo,
) -> object:
    """Resolve addresses within a deadline, including libc ``getaddrinfo``."""
    if resolver is socket.getaddrinfo:
        return _resolve_default_bounded(host, port, timeout)
    return _resolve_in_daemon_thread(resolver, host, port, timeout)


def check_reachable(
    host: str,
    port: int = _DEFAULT_SMB_PORT,
    timeout: float = 3.0,
    *,
    resolver: Callable[[str, int], object] = socket.getaddrinfo,
    connector: Callable[..., object] = socket.create_connection,
) -> ReachabilityResult:
    """Check DNS resolution then TCP reachability for *host*:*port*.

    *resolver*/*connector* are injectable so this can be unit-tested without
    any real network access.
    """
    deadline = time.monotonic() + max(0.01, timeout)
    try:
        if resolver is socket.getaddrinfo:
            try:
                ipaddress.ip_address(host)
            except ValueError:
                addresses = resolve_addresses_bounded(host, port, timeout=timeout)
            else:
                addresses = socket.getaddrinfo(
                    host, port, type=socket.SOCK_STREAM, flags=socket.AI_NUMERICHOST
                )
        else:
            addresses = resolve_addresses_bounded(
                host, port, timeout=timeout, resolver=resolver
            )
    except (OSError, TimeoutError) as exc:
        return ReachabilityResult(False, "dns", f"DNS resolution failed for {host!r}: {exc}")

    try:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError(f"reachability check timed out after {timeout:.1f}s")
        if connector is not socket.create_connection:
            conn = connector((host, port), remaining)
            close = getattr(conn, "close", None)
            if close is not None:
                close()
        else:
            connected = False
            last_error: OSError | TimeoutError | None = None
            for family, socktype, proto, _canonname, sockaddr in addresses:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    last_error = TimeoutError(
                        f"reachability check timed out after {timeout:.1f}s"
                    )
                    break
                conn = socket.socket(int(family), int(socktype), int(proto))
                try:
                    conn.settimeout(remaining)
                    conn.connect(tuple(sockaddr))
                    connected = True
                    break
                except OSError as exc:
                    last_error = exc
                finally:
                    conn.close()
            if not connected:
                raise last_error or OSError("DNS resolver returned no usable address")
    except (OSError, TimeoutError) as exc:
        return ReachabilityResult(False, "tcp", f"Cannot reach {host}:{port}: {exc}")

    return ReachabilityResult(True, "ok", "")


def wait_until_reachable(
    host: str,
    port: int = _DEFAULT_SMB_PORT,
    *,
    timeout_total: float = 60.0,
    interval: float = 2.0,
    check: Callable[[str, int], ReachabilityResult] = check_reachable,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
    cancel_event=None,  # noqa: ANN001 - threading.Event-compatible test seam
) -> ReachabilityResult:
    """Retry :func:`check_reachable` until it succeeds or *timeout_total* elapses.

    Used to wait gracefully for the network/Tailscale link to come up at
    boot before attempting to mount. *check*/*sleep*/*clock* are injectable
    so tests never actually sleep or touch the network.
    """
    deadline = clock() + max(0.0, timeout_total)
    if cancel_event is not None and cancel_event.is_set():
        return ReachabilityResult(False, "cancelled", "Reachability wait was cancelled")

    def run_check() -> ReachabilityResult:
        if check is check_reachable:
            remaining = deadline - clock()
            if remaining <= 0:
                return ReachabilityResult(
                    False,
                    "tcp",
                    f"Reachability deadline expired for {host}:{port}",
                )
            return check_reachable(host, port, timeout=min(3.0, remaining))
        return check(host, port)

    result = run_check()
    while not result.ok:
        now = clock()
        if now >= deadline:
            return result
        sleep_for = min(max(0.0, interval), max(0.0, deadline - now))
        if cancel_event is not None:
            if cancel_event.wait(sleep_for):
                return ReachabilityResult(False, "cancelled", "Reachability wait was cancelled")
        else:
            sleep(sleep_for)
        if cancel_event is not None and cancel_event.is_set():
            return ReachabilityResult(False, "cancelled", "Reachability wait was cancelled")
        result = run_check()
    return result


# ── mount / unmount ────────────────────────────────────────────────────────────


def build_mount_argv(
    server: str,
    share: str,
    mount_point: str,
    credentials_path: Path,
    *,
    read_only: bool = True,
    remote_path: str = "",
) -> list[str]:
    """Build the `mount -t cifs` argv. Never includes the password.

    Read-only by default, per ROMCloud's default safety posture for ROM
    sources. General ROMCloud remote data explicitly passes
    ``read_only=False`` for its independently configured writable mount.
    """
    options = f"credentials={credentials_path},{'ro' if read_only else 'rw'}"
    if remote_path:
        options += f",prefixpath={remote_path}"
    return ["mount", "-t", "cifs", f"//{server}/{share}", str(mount_point), "-o", options]


def build_unmount_argv(mount_point: str) -> list[str]:
    return ["umount", str(mount_point)]


def _classify_mount_failure(stderr: str) -> Exception:
    lowered = stderr.lower()
    if any(marker in lowered for marker in _AUTH_FAILURE_MARKERS):
        return ProviderAuthError(f"SMB authentication failed: {stderr.strip()}")
    if any(marker in lowered for marker in _NETWORK_FAILURE_MARKERS):
        return ProviderNotReachableError(f"SMB target unreachable: {stderr.strip()}")
    return MountError(f"mount failed: {stderr.strip()}")


def _run_owned_command_bounded(
    argv: list[str], timeout: float
) -> "subprocess.CompletedProcess[str]":
    """Run a mount helper without an unbounded kill-and-wait timeout path."""
    process = subprocess.Popen(
        argv,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        try:
            process.kill()
        except OSError:
            pass
        try:
            process.wait(timeout=0.2)
        except (OSError, subprocess.TimeoutExpired):
            # Do not turn a kernel-stuck mount helper into an unbounded wait.
            # It remains in the caller's owned process group for GUI/system
            # shutdown to terminate as a unit.
            for stream in (process.stdout, process.stderr):
                if stream is not None:
                    try:
                        stream.close()
                    except OSError:
                        pass
        raise exc
    return subprocess.CompletedProcess(
        argv, process.returncode, stdout=stdout, stderr=stderr
    )


@dataclass(frozen=True)
class MountOutcome:
    mounted: bool
    already_mounted: bool
    detail: str


def mount_cifs_source(
    server: str,
    share: str,
    mount_point: str,
    credentials_path: Path,
    *,
    read_only: bool = True,
    remote_path: str = "",
    port: int = _DEFAULT_SMB_PORT,
    wait_timeout: float = 60.0,
    wait_interval: float = 2.0,
    command_timeout: float = 15.0,
    cancel_event=None,  # noqa: ANN001 - threading.Event-compatible test seam
    runner: Callable[..., "subprocess.CompletedProcess[str]"] = subprocess.run,
    proc_mounts_path: str = _DEFAULT_PROC_MOUNTS,
) -> MountOutcome:
    """Mount *server*/*share* at *mount_point*, waiting for reachability first.

    Idempotent: a no-op (returns immediately) if *mount_point* is already
    mounted, so this is safe to call repeatedly (e.g. every boot, or from a
    supervisor that restarts the mount service).

    Raises
    ------
    ProviderNotReachableError
        DNS/network failure, or a mount-time network error.
    ProviderAuthError
        The mount succeeded in reaching the server but credentials were rejected.
    MountError
        Any other mount failure.
    """
    if is_target_mounted(mount_point, proc_mounts_path=proc_mounts_path):
        identity_matches = is_target_mounted_cifs(
            mount_point,
            server=None if read_only else server,
            share=share,
            read_only=read_only,
            remote_path=remote_path,
            proc_mounts_path=proc_mounts_path,
        )
        if not identity_matches:
            expected = "read-only" if read_only else "read-write"
            raise MountError(
                f"{mount_point} is already mounted with the wrong mode or SMB source; "
                f"unmount it before mounting the required {expected} "
                f"//{server}/{share} view"
            )
        log.info("%s is already mounted — nothing to do", mount_point)
        return MountOutcome(mounted=True, already_mounted=True, detail="already mounted")

    operation_deadline = time.monotonic() + max(0.01, wait_timeout)
    reach = wait_until_reachable(
        server,
        port=port,
        timeout_total=wait_timeout,
        interval=wait_interval,
        cancel_event=cancel_event,
    )
    if not reach.ok:
        if reach.stage == "cancelled":
            raise ProviderNotReachableError("SMB connection attempt was cancelled")
        raise ProviderNotReachableError(
            f"Timed out waiting for SMB target {server!r} to become reachable "
            f"({reach.stage}): {reach.detail}"
        )

    Path(mount_point).mkdir(parents=True, exist_ok=True)
    argv = build_mount_argv(
        server,
        share,
        mount_point,
        credentials_path,
        read_only=read_only,
        remote_path=remote_path,
    )
    log.info("Mounting %s at %s", f"//{server}/{share}", mount_point)
    remaining = operation_deadline - time.monotonic()
    if remaining <= 0:
        raise ProviderNotReachableError(
            f"Timed out before SMB mount command could start for {server!r}"
        )
    effective_timeout = min(max(0.01, command_timeout), remaining)
    try:
        if runner is subprocess.run:
            result = _run_owned_command_bounded(argv, effective_timeout)
        else:
            result = runner(
                argv,
                capture_output=True,
                text=True,
                timeout=effective_timeout,
            )
    except subprocess.TimeoutExpired as exc:
        raise ProviderNotReachableError(
            f"SMB mount command timed out after {effective_timeout:.1f}s for {server!r}"
        ) from exc
    if result.returncode != 0:
        raise _classify_mount_failure(result.stderr or result.stdout or "unknown error")

    log.info("Mounted %s at %s", f"//{server}/{share}", mount_point)
    return MountOutcome(mounted=True, already_mounted=False, detail="mounted")


def unmount_cifs_source(
    mount_point: str,
    *,
    runner: Callable[..., "subprocess.CompletedProcess[str]"] = subprocess.run,
    proc_mounts_path: str = _DEFAULT_PROC_MOUNTS,
    command_timeout: float = 10.0,
    lazy: bool = False,
) -> bool:
    """Unmount *mount_point*. Returns False (no-op) if it wasn't mounted."""
    if not is_target_mounted(mount_point, proc_mounts_path=proc_mounts_path):
        return False

    argv = build_unmount_argv(mount_point)
    if lazy:
        argv.insert(1, "-l")
    try:
        if runner is subprocess.run:
            result = _run_owned_command_bounded(
                argv, max(0.01, command_timeout)
            )
        else:
            result = runner(
                argv,
                capture_output=True,
                text=True,
                timeout=max(0.01, command_timeout),
            )
    except subprocess.TimeoutExpired as exc:
        raise MountError(
            f"Timed out after {command_timeout:.1f}s while unmounting {mount_point}"
        ) from exc
    if result.returncode != 0:
        raise MountError(f"Failed to unmount {mount_point}: {(result.stderr or '').strip()}")

    log.info("Unmounted %s", mount_point)
    return True
