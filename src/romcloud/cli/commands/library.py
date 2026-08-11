"""Manual Smart Cache library-presentation commands."""

from __future__ import annotations

import click

from romcloud.cli.context import get_container
from romcloud.core.capabilities import Capability, OperatingMode
from romcloud.core.exceptions import ROMCloudError
from romcloud.infrastructure.capabilities import capability_policy
from romcloud.infrastructure.library_view import operating_mode
from romcloud.integrations.batocera.game_access import set_operating_mode


@click.group("library")
def library_group() -> None:
    """Select Smart Cache NAS or Offline operating mode."""


def _require_smart_cache(ctx: click.Context):  # noqa: ANN202
    container = get_container(ctx)
    try:
        capability_policy(container.config).require(
            Capability.OFFLINE_MODE, "Change operating mode"
        )
    except ROMCloudError as exc:
        raise click.ClickException(str(exc)) from exc
    return container


def _set(ctx: click.Context, mode: OperatingMode) -> None:
    container = _require_smart_cache(ctx)
    try:
        report = set_operating_mode(container.config, mode)
    except ROMCloudError as exc:
        raise click.ClickException(str(exc)) from exc
    label = "Offline Mode" if mode is OperatingMode.OFFLINE else "NAS Mode"
    click.echo(
        f"{label} is active: {report.visible} visible proxy file(s). "
        "Update game lists or restart EmulationStation to see the change."
    )


@library_group.command("status")
@click.pass_context
def library_status(ctx: click.Context) -> None:
    """Show the current Smart Cache library presentation."""
    container = get_container(ctx)
    policy = capability_policy(container.config)
    if not policy.offline_mode_supported:
        click.echo("Offline Mode: unavailable (Direct/NAS mode)")
        return
    click.echo(
        "Operating mode: "
        + (
            "Offline Mode (cached games only)"
            if operating_mode(container.config) is OperatingMode.OFFLINE
            else "NAS Mode (full library and remote features)"
        )
    )


@library_group.command("offline")
@click.pass_context
def library_offline(ctx: click.Context) -> None:
    """Show only ROMCloud games with valid local cached assets."""
    _set(ctx, OperatingMode.OFFLINE)


@library_group.command("nas")
@click.pass_context
def library_nas(ctx: click.Context) -> None:
    """Reconnect and restore the full Smart Cache proxy library."""
    _set(ctx, OperatingMode.NAS)


@library_group.command("online", hidden=True)
@click.pass_context
def library_online_compat(ctx: click.Context) -> None:
    """Compatibility alias for NAS Mode."""
    _set(ctx, OperatingMode.NAS)
