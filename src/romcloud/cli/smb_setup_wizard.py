"""Interactive SMB source setup wizard — presentation layer only.

All actual discovery/authentication/validation logic lives in
:class:`romcloud.core.services.smb_discovery.SMBDiscoveryService`. This
module only prompts the user and renders results with Click — it must
never perform SMB I/O directly, so a future graphical UI can reuse the same
service without any duplicated logic.

Lifecycle (see the service module and ``romcloud configure`` for the full
picture): the wizard only *stages* a validated result in memory
(:class:`SMBSetupResult`); it is the caller's (``configure_cmd``'s)
responsibility to persist configuration/credentials, and only after the
user's final confirmation for the *entire* wizard (not just the SMB
portion).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import click

from romcloud.core.services.smb_discovery import (
    DEFAULT_SMB_PORT,
    SMBCredentials,
    SMBDiscoveryService,
    SMBErrorKind,
    SMBServerTarget,
    ShareInfo,
    SystemDetectionResult,
)
from romcloud.ui.prompts import prompt_password
import sys
from typing import Sequence


_ADVANCED_LABEL = "Advanced: enter share manually"

# Historically tests and callers referred to both `_ADVANCED_LABEL` and
# `_MANUAL_ENTRY_LABEL`; keep them identical for backward compatibility.
_MANUAL_ENTRY_LABEL = _ADVANCED_LABEL

# Sentinel returned by `_interactive_select` when interaction isn't available
# (so callers can distinguish between "no interactive support" and
# "user cancelled").
_NO_INTERACTIVE = object()

_ERROR_MESSAGES = {
    SMBErrorKind.SERVER_NOT_FOUND: "Server not found (DNS resolution failed).",
    SMBErrorKind.CONNECTION_REFUSED: "Could not connect (connection refused or SMB unavailable).",
    SMBErrorKind.TIMEOUT: "Timed out waiting for a response.",
    SMBErrorKind.AUTH_FAILED: "Authentication failed — check username/password.",
    SMBErrorKind.NO_SHARES_FOUND: "No accessible shares were found for this user.",
    SMBErrorKind.ACCESS_DENIED: "Access denied to that share.",
    SMBErrorKind.SHARE_UNAVAILABLE: "That share is not available (it may not exist).",
    SMBErrorKind.TOOL_UNAVAILABLE: "Share enumeration/validation tooling is not available on this system.",
    SMBErrorKind.UNEXPECTED: "An unexpected SMB error occurred.",
}


def _error_message(error_kind: Optional[SMBErrorKind], detail: str) -> str:
    base = _ERROR_MESSAGES.get(error_kind, _ERROR_MESSAGES[SMBErrorKind.UNEXPECTED])
    return f"{base} ({detail})" if detail else base


@dataclass(frozen=True)
class SMBSetupResult:
    """A fully validated SMB source, staged in memory — nothing has been
    persisted yet."""

    server: str
    port: int
    share: str
    username: str
    password: str
    detected_systems: tuple[str, ...]


def run_smb_setup_wizard(discovery: SMBDiscoveryService) -> Optional[SMBSetupResult]:
    """Run the interactive SMB discovery/setup flow.

    Returns a validated :class:`SMBSetupResult`, or ``None`` if the user
    cancels at any point — callers must leave any existing configuration
    and credentials completely unchanged in that case.
    """
    server = click.prompt("Server")
    port = DEFAULT_SMB_PORT

    click.echo("\nChecking server reachability...")
    reach = discovery.validate_server(SMBServerTarget(host=server, port=port))
    if not reach.ok:
        click.echo(f"Could not reach {server}: {reach.detail}")
        return None

    username = click.prompt("Username")
    password = prompt_password("Password")

    target = SMBServerTarget(host=server, port=port)
    credentials = SMBCredentials(username=username, password=password)

    click.echo("\nConnecting...")
    auth = discovery.authenticate(target, credentials)
    if not auth.ok:
        click.echo(_error_message(auth.error_kind, auth.detail))
        return None
    click.echo("Connected.")

    shares_result = discovery.list_shares(target, credentials)
    manual_only = False
    shares: tuple[ShareInfo, ...] = ()
    if not shares_result.ok:
        click.echo(f"\nShare enumeration unavailable: {_error_message(shares_result.error_kind, shares_result.detail)}")
        click.echo("Falling back to manual share entry.")
        manual_only = True
    else:
        shares = shares_result.shares

    while True:
        manual_entry_mode = False
        if manual_only:
            share = click.prompt("Share name")
            manual_entry_mode = True
        else:
            # Attempt an interactive up/down selector using simple key reads
            click.echo("\nSelect an SMB share:")
            choices = [s.name for s in shares] + [_ADVANCED_LABEL]
            # Prefer interactive selector when running in a real TTY and
            # Click's getchar is available (no curses dependency).
            selection = None
            try:
                # If the interactive function has been monkeypatched in tests,
                # invoke the patched callable directly so tests can simulate
                # interactive behavior even when the runner isn't a TTY.
                if _interactive_select is not _ORIG_INTERACTIVE:
                    selection = _interactive_select(choices)
                else:
                    # Call the original implementation (it may return the
                    # _NO_INTERACTIVE sentinel when a TTY isn't available).
                    selection = _interactive_select(choices)
            except KeyboardInterrupt:
                # Treat Ctrl-C (or a test-triggered KeyboardInterrupt) as
                # user cancellation.
                return None
            except Exception:
                selection = None

            # If the user explicitly cancelled, propagate that upwards.
            if selection is None:
                if sys.stdin.isatty():
                    return None
                # Non-TTY or error -> fall back to numbered prompt
                selection = _numbered_select(choices)
            elif selection is _NO_INTERACTIVE:
                # Explicit sentinel: fall back to numbered prompt
                selection = _numbered_select(choices)

            if selection == _ADVANCED_LABEL:
                share = click.prompt("Share name")
                manual_entry_mode = True
            else:
                share = selection

        # If a non-advanced, non-manual share value was supplied that doesn't
        # match any known share, treat it as an accidental input (e.g. stray
        # 'y' or an escape sequence) and reprompt rather than passing it to
        # the discovery layer which may raise KeyError in tests.
        if not manual_only and not manual_entry_mode and share not in [s.name for s in shares] and share != _ADVANCED_LABEL:
            click.echo("\nInvalid selection, please choose a share from the list.")
            continue

        click.echo(f"\nValidating //{server}/{share}...")
        validation = discovery.validate_share(target, credentials, share)
        if not validation.ok:
            click.echo(f"Could not access share: {_error_message(validation.error_kind, validation.detail)}")
            if not click.confirm("Try a different share?", default=True):
                return None
            continue

        detection: SystemDetectionResult = discovery.detect_systems(validation)
        click.echo(f"\nConnected to //{server}/{share}")
        click.echo("\nDetected systems:\n")
        for system in detection.detected_systems:
            click.echo(f"  \u2713 {system}")
        click.echo(f"\n{detection.count} systems detected.")

        if click.confirm("\nUse this library?", default=True):
            return SMBSetupResult(
                server=server,
                port=port,
                share=share,
                username=username,
                password=password,
                detected_systems=detection.detected_systems,
            )

        if not click.confirm("Try a different share?", default=True):
            return None


def _interactive_select(choices: Sequence[str]) -> Optional[str]:
    """Interactive up/down selector using single-character reads.

    Returns the chosen string, or None if interaction is not possible or
    the user cancelled.
    """
    # Ensure we have a TTY; return a sentinel when interactive mode
    # isn't available so callers can fall back to a numbered prompt.
    if not sys.stdin.isatty():
        return _NO_INTERACTIVE

    # Initial index
    index = 0

    # Print initial menu
    for i, c in enumerate(choices):
        prefix = ">" if i == index else " "
        click.echo(f"{prefix} {c}")

    while True:
        ch = click.getchar(echo=False)
        if not ch:
            continue
        # Ctrl-C
        if ch == "\x03":
            raise KeyboardInterrupt
        # Enter confirms
        if ch in ("\r", "\n"):
            return choices[index]
        # Escape sequences for arrows: ESC [ A/B
        if ch == "\x1b":
            # Peek next two chars
            ch2 = click.getchar(echo=False)
            if ch2 == "[":
                ch3 = click.getchar(echo=False)
                if ch3 == "A":
                    index = (index - 1) % len(choices)
                elif ch3 == "B":
                    index = (index + 1) % len(choices)
                else:
                    # ignore other sequences
                    pass
            else:
                # Single ESC -> cancel
                return None
        elif ch.lower() in ("k",):
            index = (index - 1) % len(choices)
        elif ch.lower() in ("j",):
            index = (index + 1) % len(choices)
        else:
            # ignore other keys
            pass

        # Re-render menu: move cursor up N lines and rewrite
        # Move cursor up by number of choices
        click.echo(f"\x1b[{len(choices)}A", nl=False)
        for i, c in enumerate(choices):
            prefix = ">" if i == index else " "
            # Clear line then write
            click.echo(f"\x1b[2K{prefix} {c}")


# Keep a reference to the original implementation so tests that monkeypatch
# `_interactive_select` can be detected and the patched callable invoked
# directly (CliRunner isn't a TTY, so the original would otherwise return
# a non-interactive sentinel).
_ORIG_INTERACTIVE = _interactive_select


def _numbered_select(choices: Sequence[str]) -> str:
    click.echo("")
    for i, c in enumerate(choices, start=1):
        click.echo(f"{i}) {c}")

    # Prompt as text so users can either enter a number or type the
    # share/menu label directly (keeps behavior friendly in non-TTY
    # environments and matches prior UX expectations).
    while True:
        raw = click.prompt("Select share", default="1", type=str)
        raw = raw.strip()

        # Empty -> default
        if raw == "":
            return choices[0]

        # Ignore obvious terminal control sequences (ESC/CSI) that might
        # have been sent by a non-interactive harness attempting to drive
        # arrow keys.
        if raw.startswith("\x1b") or any(ord(ch) < 32 for ch in raw):
            # reprompt
            continue

        # Numeric selection
        if raw.isdigit():
            idx = int(raw)
            if 1 <= idx <= len(choices):
                return choices[idx - 1]
            # fall through to treat as text if out of range

        # Exact match to one of the displayed choices
        for c in choices:
            if raw == c:
                return c

        # Otherwise treat as a manual share name (user typed a share)
        return raw
