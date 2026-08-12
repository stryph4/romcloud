"""Manual authoritative operating-mode commands."""

from __future__ import annotations

import click

from romcloud.cli.context import get_container
from romcloud.core.capabilities import OperatingMode
from romcloud.core.exceptions import ROMCloudError
from romcloud.infrastructure.library_view import operating_mode
from romcloud.integrations.batocera.game_access import set_operating_mode


@click.group("library")
def library_group() -> None:
    """Select Connected, Cache, or Offline operating mode."""


def _set(ctx: click.Context, mode: OperatingMode) -> None:
    container = get_container(ctx)
    try:
        report = set_operating_mode(container.config, mode)
    except ROMCloudError as exc:
        raise click.ClickException(str(exc)) from exc
    label = f"{mode.value.title()} Mode"
    if not report.mode_changed:
        click.echo(f"{label} is already active.")
        return
    click.echo(
        f"{label} is active: {report.visible} managed game(s) visible."
    )


@library_group.command("status")
@click.pass_context
def library_status(ctx: click.Context) -> None:
    """Show the authoritative operating mode."""
    container = get_container(ctx)
    selected = operating_mode(container.config)
    descriptions = {
        OperatingMode.CONNECTED: "use the configured ROM source directly",
        OperatingMode.CACHE: "show the managed library and cache games on demand",
        OperatingMode.OFFLINE: "show only games playable locally",
    }
    click.echo(
        f"Operating mode: {selected.value.title()} Mode ({descriptions[selected]})"
    )


@library_group.command("connected")
@click.pass_context
def library_connected(ctx: click.Context) -> None:
    """Use the configured ROM source directly."""
    _set(ctx, OperatingMode.CONNECTED)


@library_group.command("cache")
@click.pass_context
def library_cache(ctx: click.Context) -> None:
    """Expose the managed catalog and cache games on demand."""
    _set(ctx, OperatingMode.CACHE)


@library_group.command("offline")
@click.pass_context
def library_offline(ctx: click.Context) -> None:
    """Show only ROMCloud games with valid local cached assets."""
    _set(ctx, OperatingMode.OFFLINE)


@library_group.command("online", hidden=True)
@click.pass_context
def library_online_compat(ctx: click.Context) -> None:
    """Compatibility alias for Connected Mode."""
    _set(ctx, OperatingMode.CONNECTED)
