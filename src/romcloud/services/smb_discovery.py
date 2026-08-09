"""Reusable SMB source discovery/setup service.

This module implements the *product* flow described in the SMB setup
requirements: connect/authenticate → enumerate accessible shares → user
selects a share → ROMCloud validates it can actually access that share →
inspect its top-level structure for recognizable Batocera system folders.

Core UX rule: *"If ROMCloud lets you select an SMB library, ROMCloud has
already proven it can access it."* Nothing here persists any configuration
or credentials — that is the caller's job, only after the caller has
received a successful, validated result.

Architecture
------------
:class:`SMBDiscoveryService` is deliberately **not** aware of Click, stdin/
stdout, prompts, or any other presentation concern — it is a plain service
object, reused by both the CLI configure wizard
(:mod:`romcloud.cli.smb_setup_wizard`) and graphical setup bridge without
duplicating authentication/enumeration/validation logic.

All actual network I/O is delegated to an injected :class:`SMBTransport`
implementation (see :mod:`romcloud.infrastructure.smb_discovery_client` for
the concrete, subprocess-based implementation) — exactly the same
dependency-injection pattern already used by
:mod:`romcloud.infrastructure.mount` (injectable ``runner``/``resolver``/
``connector`` callables) so this is fully unit-testable with fakes and never
requires a real SMB server in the standard test suite.

This module does NOT implement a native SMBProvider or any direct SMB file
streaming — the end result of a successful setup is still the existing
mounted-SMB architecture: ``//server/share`` mounted at a local path and
read through :class:`~romcloud.infrastructure.providers.local.LocalFilesystemProvider`
(see :mod:`romcloud.infrastructure.mount`).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional, Protocol, Tuple

from romcloud.integrations.batocera.systems import BATOCERA_SYSTEMS
from romcloud.infrastructure.mount import ReachabilityResult, check_reachable

DEFAULT_SMB_PORT = 445


# ── error taxonomy ────────────────────────────────────────────────────────────


class SMBErrorKind(str, Enum):
    """Structured error categories — never derived from raw exception text
    at the call site, so callers (CLI or future GUI) can render a useful,
    specific message without parsing error strings themselves."""

    SERVER_NOT_FOUND = "server_not_found"
    """DNS resolution failed for the given hostname/IP."""

    CONNECTION_REFUSED = "connection_refused"
    """TCP/SMB connection could not be established (refused, unreachable)."""

    TIMEOUT = "timeout"
    """The operation exceeded its time budget."""

    AUTH_FAILED = "auth_failed"
    """The server was reachable but credentials were rejected."""

    NO_SHARES_FOUND = "no_shares_found"
    """Authentication succeeded but no accessible (non-administrative)
    shares were found for this user."""

    ACCESS_DENIED = "access_denied"
    """Authentication succeeded, but access to the specific selected share
    was denied."""

    SHARE_UNAVAILABLE = "share_unavailable"
    """The selected share no longer exists / disappeared server-side."""

    TOOL_UNAVAILABLE = "tool_unavailable"
    """The local SMB client tooling required for discovery is not
    installed — enumeration/validation cannot proceed. Callers should fall
    back to the manual-share entry path."""

    UNEXPECTED = "unexpected"
    """Any other SMB error not covered by a more specific category."""


# ── data types ────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class SMBServerTarget:
    host: str
    port: int = DEFAULT_SMB_PORT


@dataclass(frozen=True)
class SMBCredentials:
    """Username/password pair. Never logged, never rendered in full by
    ``repr()`` — only the username is shown."""

    username: str
    password: str

    def __repr__(self) -> str:  # pragma: no cover - trivial
        return f"SMBCredentials(username={self.username!r}, password='***')"


@dataclass(frozen=True)
class ShareInfo:
    """A single SMB share, as discovered via enumeration."""

    name: str
    kind: str = "disk"  # "disk" | "printer" | "ipc" | "other"
    comment: str = ""


@dataclass(frozen=True)
class AuthResult:
    ok: bool
    error_kind: Optional[SMBErrorKind] = None
    detail: str = ""


@dataclass(frozen=True)
class ListSharesResult:
    ok: bool
    shares: Tuple[ShareInfo, ...] = ()
    error_kind: Optional[SMBErrorKind] = None
    detail: str = ""


@dataclass(frozen=True)
class ShareValidationResult:
    """Proof (or not) that the selected share can actually be accessed —
    the result of listing its top-level contents."""

    ok: bool
    share: str
    error_kind: Optional[SMBErrorKind] = None
    detail: str = ""
    top_level_entries: Tuple[str, ...] = ()


@dataclass(frozen=True)
class SystemDetectionResult:
    """Which top-level share entries look like recognized Batocera system
    folders, per :data:`romcloud.integrations.batocera.systems.BATOCERA_SYSTEMS` — the same
    mapping already used for catalog scanning, so detection here can never
    drift from what will actually be recognized once the share is mounted
    and scanned for real."""

    detected_systems: Tuple[str, ...]
    unrecognized_entries: Tuple[str, ...]

    @property
    def count(self) -> int:
        return len(self.detected_systems)


# ── transport abstraction ────────────────────────────────────────────────────


class SMBTransport(Protocol):
    """Low-level SMB operations. :class:`SMBDiscoveryService` never talks to
    a socket, subprocess, or SMB library directly — everything goes through
    an injected implementation of this protocol. See
    :mod:`romcloud.infrastructure.smb_discovery_client` for the real,
    subprocess-based implementation, and the test suite for fakes."""

    def authenticate(self, target: SMBServerTarget, credentials: SMBCredentials) -> AuthResult:
        """Prove *credentials* are accepted by *target*, without
        necessarily enumerating or accessing any particular share."""
        ...

    def list_shares(self, target: SMBServerTarget, credentials: SMBCredentials) -> ListSharesResult:
        """Enumerate all shares visible to *credentials* on *target*."""
        ...

    def list_share_directory(
        self,
        target: SMBServerTarget,
        credentials: SMBCredentials,
        share: str,
        path: str = "",
    ) -> ShareValidationResult:
        """List the top-level entries of *share* (optionally under *path*),
        proving read access. Used for both share validation and system
        detection — a single round trip serves both purposes."""
        ...


# ── administrative-share filtering ───────────────────────────────────────────

# Deliberately an exact-name allowlist-style exclusion, never a pattern like
# "ends with $" — legitimate user shares can also end in `$`, and the
# product requirement is explicit: "do not aggressively hide legitimate
# user shares."
_ADMINISTRATIVE_SHARE_NAMES = frozenset({"ipc$", "admin$", "print$", "c$", "d$", "e$", "f$"})


def is_administrative_share(share: ShareInfo) -> bool:
    """True for well-known Windows/Samba administrative shares that should
    not normally be offered to users in the share-selection menu."""
    if share.kind != "disk":
        return True
    return share.name.lower() in _ADMINISTRATIVE_SHARE_NAMES


# ── the service ───────────────────────────────────────────────────────────────


class SMBDiscoveryService:
    """Reusable, presentation-agnostic SMB source discovery/setup service."""

    def __init__(
        self,
        transport: SMBTransport,
        *,
        reachability_checker=check_reachable,
    ) -> None:
        self._transport = transport
        self._check_reachable = reachability_checker

    def validate_server(self, target: SMBServerTarget) -> ReachabilityResult:
        """Confirm the server resolves and is reachable — checked before
        ever touching credentials or attempting any SMB operation."""
        return self._check_reachable(target.host, target.port)

    def authenticate(self, target: SMBServerTarget, credentials: SMBCredentials) -> AuthResult:
        """Prove the supplied credentials are accepted by the server."""
        return self._transport.authenticate(target, credentials)

    def list_shares(
        self,
        target: SMBServerTarget,
        credentials: SMBCredentials,
        *,
        include_administrative: bool = False,
    ) -> ListSharesResult:
        """Enumerate shares accessible to *credentials*, filtering obvious
        administrative shares by default (see :func:`is_administrative_share`)."""
        result = self._transport.list_shares(target, credentials)
        if not result.ok or include_administrative:
            return result

        visible = tuple(s for s in result.shares if not is_administrative_share(s))
        if not visible:
            return ListSharesResult(
                ok=False,
                shares=(),
                error_kind=SMBErrorKind.NO_SHARES_FOUND,
                detail="No accessible shares were found for this user.",
            )
        return ListSharesResult(ok=True, shares=visible)

    def validate_share(
        self,
        target: SMBServerTarget,
        credentials: SMBCredentials,
        share: str,
    ) -> ShareValidationResult:
        """Prove the selected (or manually entered) share can actually be
        listed/read. This is the one operation that must succeed before any
        configuration or credentials may be persisted."""
        return self._transport.list_share_directory(target, credentials, share)

    def detect_systems(self, validation: ShareValidationResult) -> SystemDetectionResult:
        """Classify a validated share's top-level entries into recognized
        Batocera systems vs everything else. Never requires every directory
        to be recognized — unrecognized entries are simply reported
        separately, not treated as an error."""
        detected = []
        unrecognized = []
        for entry in validation.top_level_entries:
            if entry.strip().lower() in BATOCERA_SYSTEMS:
                detected.append(entry)
            else:
                unrecognized.append(entry)
        return SystemDetectionResult(
            detected_systems=tuple(sorted(detected)),
            unrecognized_entries=tuple(sorted(unrecognized)),
        )
