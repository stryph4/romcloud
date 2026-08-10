"""Concrete SMB discovery transport — shells out to Samba's ``smbclient``.

Follows the exact same architectural pattern as
:mod:`romcloud.infrastructure.mount`: real work is done via well-known,
already-battle-tested system binaries (there, ``mount``/``umount``; here,
``smbclient``), invoked through an injectable ``runner`` callable so the
entire transport is unit-testable with canned
:class:`subprocess.CompletedProcess` results — no real network or SMB
server required.

Why ``smbclient`` and not a Python SMB library
-----------------------------------------------
Enumerating the *list of shares* a user can see (as opposed to reading
files inside one already-known share) requires an SRVSVC ``NetrShareEnum``
RPC call — a nontrivial DCE/RPC exchange. ``smbclient -L`` already performs
this exact operation and is part of the same Samba tooling family as
``mount.cifs``/``cifs-utils``, which this project already depends on for
the proven, real-hardware-tested mounted-SMB architecture. Shelling out to
it (with all credentials passed via a short-lived, mode-0600 authentication
file — never on the command line or in an environment variable) avoids
hand-rolling RPC parsing and any large third-party dependency.

Assumption requiring real Batocera hardware validation
--------------------------------------------------------
This assumes the ``smbclient`` binary (Samba client tools) is present on
Batocera 42 in addition to the ``cifs-utils`` package already relied upon
for mounting. If it is *not* present, share enumeration and validation
degrade gracefully (:data:`~romcloud.services.smb_discovery.SMBErrorKind.TOOL_UNAVAILABLE`)
and the CLI wizard falls back to manual share entry — but manual entry's
own validation step also requires ``smbclient`` today. This should be
confirmed on real hardware; see README for the noted assumption.
"""

from __future__ import annotations

import os
import re
import stat
import subprocess
import tempfile
from pathlib import Path
from typing import Callable

from romcloud.services.smb_discovery import (
    AuthResult,
    ListSharesResult,
    SMBCredentials,
    SMBErrorKind,
    SMBDirectoryEntry,
    SMBServerTarget,
    ShareInfo,
    ShareValidationResult,
)
from romcloud.infrastructure.logging import get_logger

log = get_logger("smb_discovery_client")

_SMBCLIENT_BIN = "smbclient"
_DEFAULT_TIMEOUT = 15.0

RunnerType = Callable[..., "subprocess.CompletedProcess[str]"]

_AUTH_FAILURE_MARKERS = (
    "nt_status_logon_failure",
    "nt_status_password_expired",
    "nt_status_account_disabled",
    "nt_status_account_locked_out",
)
_SHARE_ACCESS_DENIED_MARKERS = ("nt_status_access_denied",)
_SHARE_UNAVAILABLE_MARKERS = (
    "nt_status_bad_network_name",
    "nt_status_object_path_not_found",
    "nt_status_object_name_not_found",
)
_CONNECTION_REFUSED_MARKERS = (
    "nt_status_connection_refused",
    "nt_status_io_timeout",
    "connection refused",
    "no route to host",
    "network is unreachable",
)
_SERVER_NOT_FOUND_MARKERS = (
    "failed to resolve",
    "name or service not known",
    "getaddrinfo failed",
    "unable to resolve",
)


# ── credential handling ──────────────────────────────────────────────────────


def _write_auth_file(username: str, password: str) -> Path:
    """Write a short-lived ``smbclient -A`` authentication file (mode 0600).

    Keeps the password out of argv (never visible via ``ps``) and out of
    any environment variable. The caller is responsible for deleting this
    file immediately after use (see callers below, always in a
    ``try/finally``).
    """
    fd, path_str = tempfile.mkstemp(prefix="romcloud-smb-auth-")
    path = Path(path_str)
    try:
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)  # 0600, before any content is written
        content = f"username = {username}\npassword = {password}\n"
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(content)
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    return path


# ── argv builders ─────────────────────────────────────────────────────────────


def build_list_shares_argv(target: SMBServerTarget, auth_file: Path) -> list[str]:
    return [
        _SMBCLIENT_BIN,
        "-L",
        f"//{target.host}",
        "-p",
        str(target.port),
        "-A",
        str(auth_file),
        "-g",
    ]


def build_authenticate_argv(target: SMBServerTarget, auth_file: Path) -> list[str]:
    """Connect and immediately quit — proves credentials are accepted
    without needing to enumerate or touch any particular share."""
    return [
        _SMBCLIENT_BIN,
        f"//{target.host}/IPC$",
        "-p",
        str(target.port),
        "-A",
        str(auth_file),
        "-c",
        "quit",
    ]


def build_list_directory_argv(target: SMBServerTarget, auth_file: Path, share: str, path: str = "") -> list[str]:
    safe_path = path.replace("\\", "\\\\").replace('"', '\\"')
    command = f'cd "{safe_path}"; ls' if safe_path else "ls"
    return [
        _SMBCLIENT_BIN,
        f"//{target.host}/{share}",
        "-p",
        str(target.port),
        "-A",
        str(auth_file),
        "-c",
        command,
    ]


# ── error classification ─────────────────────────────────────────────────────


def _classify_error(returncode: int, stdout: str, stderr: str, *, context: str) -> SMBErrorKind:
    """Classify a failed ``smbclient`` invocation.

    *context* is ``"auth"`` (connecting/enumerating — no specific share
    selected yet) or ``"share"`` (accessing an already-selected share) —
    the same NT_STATUS_ACCESS_DENIED text means "bad credentials" in the
    first case but "this share is restricted" in the second.
    """
    combined = f"{stdout}\n{stderr}".lower()

    if any(marker in combined for marker in _SERVER_NOT_FOUND_MARKERS):
        return SMBErrorKind.SERVER_NOT_FOUND
    if any(marker in combined for marker in _AUTH_FAILURE_MARKERS):
        return SMBErrorKind.AUTH_FAILED
    if any(marker in combined for marker in _SHARE_UNAVAILABLE_MARKERS):
        return SMBErrorKind.SHARE_UNAVAILABLE
    if any(marker in combined for marker in _SHARE_ACCESS_DENIED_MARKERS):
        return SMBErrorKind.AUTH_FAILED if context == "auth" else SMBErrorKind.ACCESS_DENIED
    if any(marker in combined for marker in _CONNECTION_REFUSED_MARKERS):
        return SMBErrorKind.CONNECTION_REFUSED
    return SMBErrorKind.UNEXPECTED


def _error_detail(stdout: str, stderr: str) -> str:
    """A short, credential-free detail string for logs/errors.

    ``smbclient`` never echoes the supplied password in its own output, so
    this is safe to surface directly — but it is still deliberately kept
    short (first non-empty line) to avoid dumping noisy protocol traces.
    """
    for line in (stderr or stdout or "").splitlines():
        line = line.strip()
        if line:
            return line
    return "smbclient reported failure with no further detail"


# ── output parsing ────────────────────────────────────────────────────────────

_ADMIN_KIND_MAP = {
    "disk": "disk",
    "printer": "printer",
    "ipc": "ipc",
}


def parse_share_list(stdout: str) -> list[ShareInfo]:
    """Parse ``smbclient -L ... -g`` grepable output.

    Each share line looks like ``Disk|ShareName|Comment``. Share names may
    legitimately be empty of a comment; only ``Disk``/``Printer``/``IPC``
    typed lines are shares (other ``-g`` lines describe the server itself).
    """
    shares: list[ShareInfo] = []
    for line in stdout.splitlines():
        parts = line.split("|")
        if len(parts) < 2:
            continue
        kind_raw = parts[0].strip().lower()
        if kind_raw not in _ADMIN_KIND_MAP:
            continue
        name = parts[1].strip()
        if not name:
            continue
        comment = parts[2].strip() if len(parts) > 2 else ""
        shares.append(ShareInfo(name=name, kind=_ADMIN_KIND_MAP[kind_raw], comment=comment))
    return shares


_LS_LINE_RE = re.compile(r"^\s*(?P<name>.+?)\s{2,}(?P<attrs>[A-Za-z]*[DAHSRN][A-Za-z]*)\s+\d+\s+.+$")


def parse_directory_entries(stdout: str) -> list[SMBDirectoryEntry]:
    """Parse both files and directories from ``smbclient ls`` output."""
    entries: list[SMBDirectoryEntry] = []
    for line in stdout.splitlines():
        match = _LS_LINE_RE.match(line)
        if not match:
            continue
        name = match.group("name").strip()
        if name in (".", ".."):
            continue
        entries.append(
            SMBDirectoryEntry(name=name, is_directory="D" in match.group("attrs"))
        )
    return entries


def parse_directory_listing(stdout: str) -> list[str]:
    """Parse ``smbclient -c ls`` interactive output into directory names.

    Only entries flagged with the ``D`` (directory) attribute are
    returned; ``.``/``..`` are skipped. Non-matching lines (blank lines,
    the trailing "N blocks of size ..." summary, etc.) are ignored rather
    than raising — ``smbclient`` output formatting can vary slightly across
    Samba versions.
    """
    return [entry.name for entry in parse_directory_entries(stdout) if entry.is_directory]


# ── transport implementation ─────────────────────────────────────────────────


class SmbclientTransport:
    """Concrete :class:`~romcloud.services.smb_discovery.SMBTransport`
    implementation, backed by the ``smbclient`` CLI."""

    def __init__(self, *, runner: RunnerType = subprocess.run, timeout: float = _DEFAULT_TIMEOUT) -> None:
        self._runner = runner
        self._timeout = timeout

    def _run(self, argv: list[str]) -> tuple[int, str, str, SMBErrorKind | None]:
        """Run *argv*, returning ``(returncode, stdout, stderr, tool_error)``.

        ``tool_error`` is set (and the other fields are meaningless) when
        ``smbclient`` itself could not be found or the call timed out.
        """
        try:
            result = self._runner(argv, capture_output=True, text=True, timeout=self._timeout)
        except FileNotFoundError:
            return 0, "", "", SMBErrorKind.TOOL_UNAVAILABLE
        except subprocess.TimeoutExpired:
            return 0, "", "", SMBErrorKind.TIMEOUT
        return result.returncode, result.stdout or "", result.stderr or "", None

    def authenticate(self, target: SMBServerTarget, credentials: SMBCredentials) -> AuthResult:
        auth_file = _write_auth_file(credentials.username, credentials.password)
        try:
            argv = build_authenticate_argv(target, auth_file)
            returncode, stdout, stderr, tool_error = self._run(argv)
        finally:
            auth_file.unlink(missing_ok=True)

        if tool_error is not None:
            return AuthResult(ok=False, error_kind=tool_error, detail=_tool_error_detail(tool_error))
        if returncode != 0:
            kind = _classify_error(returncode, stdout, stderr, context="auth")
            return AuthResult(ok=False, error_kind=kind, detail=_error_detail(stdout, stderr))
        return AuthResult(ok=True)

    def list_shares(self, target: SMBServerTarget, credentials: SMBCredentials) -> ListSharesResult:
        auth_file = _write_auth_file(credentials.username, credentials.password)
        try:
            argv = build_list_shares_argv(target, auth_file)
            returncode, stdout, stderr, tool_error = self._run(argv)
        finally:
            auth_file.unlink(missing_ok=True)

        if tool_error is not None:
            return ListSharesResult(ok=False, error_kind=tool_error, detail=_tool_error_detail(tool_error))
        if returncode != 0:
            kind = _classify_error(returncode, stdout, stderr, context="auth")
            return ListSharesResult(ok=False, error_kind=kind, detail=_error_detail(stdout, stderr))

        shares = parse_share_list(stdout)
        if not shares:
            return ListSharesResult(
                ok=False,
                error_kind=SMBErrorKind.NO_SHARES_FOUND,
                detail="No shares were found for this user.",
            )
        return ListSharesResult(ok=True, shares=tuple(shares))

    def list_share_directory(
        self,
        target: SMBServerTarget,
        credentials: SMBCredentials,
        share: str,
        path: str = "",
    ) -> ShareValidationResult:
        auth_file = _write_auth_file(credentials.username, credentials.password)
        try:
            argv = build_list_directory_argv(target, auth_file, share, path)
            returncode, stdout, stderr, tool_error = self._run(argv)
        finally:
            auth_file.unlink(missing_ok=True)

        if tool_error is not None:
            return ShareValidationResult(
                ok=False, share=share, error_kind=tool_error, detail=_tool_error_detail(tool_error)
            )
        if returncode != 0:
            kind = _classify_error(returncode, stdout, stderr, context="share")
            return ShareValidationResult(ok=False, share=share, error_kind=kind, detail=_error_detail(stdout, stderr))

        entries = parse_directory_entries(stdout)
        return ShareValidationResult(
            ok=True,
            share=share,
            top_level_entries=tuple(
                entry.name for entry in entries if entry.is_directory
            ),
            entries=tuple(entries),
        )


def _tool_error_detail(kind: SMBErrorKind) -> str:
    if kind is SMBErrorKind.TOOL_UNAVAILABLE:
        return "smbclient is not installed — share enumeration/validation is unavailable."
    if kind is SMBErrorKind.TIMEOUT:
        return "Timed out waiting for smbclient to respond."
    return "Unknown smbclient error."


def build_default_smb_discovery_service():
    """Factory for the real, subprocess-backed discovery service — used by
    the CLI wizard and (in future) a graphical setup UI."""
    from romcloud.services.smb_discovery import SMBDiscoveryService

    return SMBDiscoveryService(transport=SmbclientTransport())
