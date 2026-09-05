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
    """Select Direct, Cached Storage, or Offline operating mode."""


_MODE_LABELS = {
    OperatingMode.CONNECTED: "Direct",
    OperatingMode.CACHE: "Cached Storage",
    OperatingMode.OFFLINE: "Offline",
}


def _set(
    ctx: click.Context, mode: OperatingMode, *, use_remote_saves: bool = False
) -> None:
    container = get_container(ctx)
    try:
        report = set_operating_mode(
            container.config,
            mode,
            conflict_action="remote-wins" if use_remote_saves else "stop",
        )
    except ROMCloudError as exc:
        raise click.ClickException(str(exc)) from exc
    label = _MODE_LABELS[mode]
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
        OperatingMode.CONNECTED: "play games directly from the configured ROM source",
        OperatingMode.CACHE: "copy games into ROMCloud-managed local storage as needed, then play them locally",
        OperatingMode.OFFLINE: "show and use only games already available locally",
    }
    click.echo(
        f"Operating mode: {_MODE_LABELS[selected]} ({descriptions[selected]})"
    )


@library_group.command("connected")
@click.option(
    "--use-remote-saves",
    is_flag=True,
    help="Explicitly discard conflicting local progress and continue with remote saves.",
)
@click.pass_context
def library_connected(ctx: click.Context, use_remote_saves: bool) -> None:
    """Use the configured ROM source directly."""
    _set(ctx, OperatingMode.CONNECTED, use_remote_saves=use_remote_saves)


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
@click.option("--use-remote-saves", is_flag=True)
@click.pass_context
def library_online_compat(ctx: click.Context, use_remote_saves: bool) -> None:
    """Compatibility alias for Direct."""
    _set(ctx, OperatingMode.CONNECTED, use_remote_saves=use_remote_saves)
