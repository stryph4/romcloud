"""Manual Smart Cache library-presentation commands."""

from __future__ import annotations

import click

from romcloud.cli.context import get_container
from romcloud.core.capabilities import Capability
from romcloud.core.exceptions import ROMCloudError
from romcloud.infrastructure.capabilities import capability_policy
from romcloud.infrastructure.library_view import offline_library_enabled
from romcloud.integrations.batocera.game_access import set_offline_library_mode


@click.group("library")
def library_group() -> None:
    """Select Smart Cache Online or Offline operating mode."""


def _require_smart_cache(ctx: click.Context):  # noqa: ANN202
    container = get_container(ctx)
    try:
        capability_policy(container.config).require(
            Capability.OFFLINE_MODE, "Change operating mode"
        )
    except ROMCloudError as exc:
        raise click.ClickException(str(exc)) from exc
    return container


def _set(ctx: click.Context, enabled: bool) -> None:
    container = _require_smart_cache(ctx)
    try:
        report = set_offline_library_mode(container.config, enabled)
    except ROMCloudError as exc:
        raise click.ClickException(str(exc)) from exc
    label = "Offline Mode" if enabled else "Online Mode"
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
            if offline_library_enabled(container.config)
            else "Online Mode (full library and network features)"
        )
    )


@library_group.command("offline")
@click.pass_context
def library_offline(ctx: click.Context) -> None:
    """Show only ROMCloud games with valid local cached assets."""
    _set(ctx, True)


@library_group.command("online")
@click.pass_context
def library_online(ctx: click.Context) -> None:
    """Restore the full Smart Cache proxy library."""
    _set(ctx, False)
