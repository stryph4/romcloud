"""Interactive SMB source setup wizard — presentation layer only.

All actual discovery/authentication/validation logic lives in
:class:`romcloud.services.smb_discovery.SMBDiscoveryService`. This
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

from romcloud.services.smb_discovery import (
    DEFAULT_SMB_PORT,
    SMBCredentials,
    SMBDiscoveryService,
    SMBErrorKind,
    SMBServerTarget,
    ShareInfo,
    SystemDetectionResult,
)
from romcloud.ui.prompts import prompt_password


_ADVANCED_LABEL = "Advanced: enter share manually"
_MANUAL_ENTRY_LABEL = _ADVANCED_LABEL

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
        if manual_only:
            share = click.prompt("Share name")
        else:
            click.echo("\nSelect an SMB share:")
            choices = [s.name for s in shares] + [_ADVANCED_LABEL]
            try:
                selection = _numbered_select(choices)
            except (click.Abort, KeyboardInterrupt):
                return None

            if selection == _ADVANCED_LABEL:
                share = click.prompt("Share name")
            else:
                share = selection

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


def _numbered_select(choices: list[str]) -> str:
    click.echo("")
    for i, c in enumerate(choices, start=1):
        click.echo(f"{i}) {c}")

    choice = click.prompt(
        f"Select share [1-{len(choices)}]",
        type=click.IntRange(1, len(choices)),
        default=1,
    )
    return choices[choice - 1]
