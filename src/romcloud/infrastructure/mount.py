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

import os
import socket
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

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


# ── reachability ──────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ReachabilityResult:
    ok: bool
    stage: str  # "dns" | "tcp" | "ok"
    detail: str


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
    try:
        resolver(host, port)
    except OSError as exc:
        return ReachabilityResult(False, "dns", f"DNS resolution failed for {host!r}: {exc}")

    try:
        conn = connector((host, port), timeout)
        close = getattr(conn, "close", None)
        if close is not None:
            close()
    except OSError as exc:
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
) -> ReachabilityResult:
    """Retry :func:`check_reachable` until it succeeds or *timeout_total* elapses.

    Used to wait gracefully for the network/Tailscale link to come up at
    boot before attempting to mount. *check*/*sleep*/*clock* are injectable
    so tests never actually sleep or touch the network.
    """
    deadline = clock() + timeout_total
    result = check(host, port)
    while not result.ok:
        if clock() >= deadline:
            return result
        sleep(interval)
        result = check(host, port)
    return result


# ── mount / unmount ────────────────────────────────────────────────────────────


def build_mount_argv(
    server: str,
    share: str,
    mount_point: str,
    credentials_path: Path,
    *,
    read_only: bool = True,
) -> list[str]:
    """Build the `mount -t cifs` argv. Never includes the password.

    Read-only by default, per ROMCloud's default safety posture for ROM
    sources — pass ``read_only=False`` only for sources that must be
    writable (e.g. save sync, out of scope here).
    """
    options = f"credentials={credentials_path},{'ro' if read_only else 'rw'}"
    return ["mount", "-t", "cifs", f"//{server}/{share}", str(mount_point), "-o", options]


def build_unmount_argv(mount_point: str) -> list[str]:
    return ["umount", str(mount_point)]


def _classify_mount_failure(stderr: str) -> Exception:
    lowered = stderr.lower()
    if any(marker in lowered for marker in _AUTH_FAILURE_MARKERS):
        return ProviderAuthError(f"SMB authentication failed: {stderr.strip()}")
    if any(marker in lowered for marker in _NETWORK_FAILURE_MARKERS):
        return ProviderNotReachableError(f"SMB source unreachable: {stderr.strip()}")
    return MountError(f"mount failed: {stderr.strip()}")


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
    port: int = _DEFAULT_SMB_PORT,
    wait_timeout: float = 60.0,
    wait_interval: float = 2.0,
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
        log.info("%s is already mounted — nothing to do", mount_point)
        return MountOutcome(mounted=True, already_mounted=True, detail="already mounted")

    reach = wait_until_reachable(
        server, port=port, timeout_total=wait_timeout, interval=wait_interval
    )
    if not reach.ok:
        raise ProviderNotReachableError(
            f"Timed out waiting for SMB source {server!r} to become reachable "
            f"({reach.stage}): {reach.detail}"
        )

    Path(mount_point).mkdir(parents=True, exist_ok=True)
    argv = build_mount_argv(server, share, mount_point, credentials_path, read_only=read_only)
    log.info("Mounting %s at %s", f"//{server}/{share}", mount_point)
    result = runner(argv, capture_output=True, text=True)
    if result.returncode != 0:
        raise _classify_mount_failure(result.stderr or result.stdout or "unknown error")

    log.info("Mounted %s at %s", f"//{server}/{share}", mount_point)
    return MountOutcome(mounted=True, already_mounted=False, detail="mounted")


def unmount_cifs_source(
    mount_point: str,
    *,
    runner: Callable[..., "subprocess.CompletedProcess[str]"] = subprocess.run,
    proc_mounts_path: str = _DEFAULT_PROC_MOUNTS,
) -> bool:
    """Unmount *mount_point*. Returns False (no-op) if it wasn't mounted."""
    if not is_target_mounted(mount_point, proc_mounts_path=proc_mounts_path):
        return False

    result = runner(build_unmount_argv(mount_point), capture_output=True, text=True)
    if result.returncode != 0:
        raise MountError(f"Failed to unmount {mount_point}: {(result.stderr or '').strip()}")

    log.info("Unmounted %s", mount_point)
    return True
