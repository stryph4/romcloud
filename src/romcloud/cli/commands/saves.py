"""romcloud saves — save sync sub-commands (stub for v0.1)."""

from __future__ import annotations

import click


@click.group("saves")
def saves_group() -> None:
    """Manage save data synchronisation."""


@saves_group.command("sync")
@click.argument("game_id", required=False)
@click.pass_context
def saves_sync(ctx: click.Context, game_id: str | None) -> None:
    """Sync save data to/from the configured save storage."""
    click.echo("Save sync is not yet implemented in v0.1.", err=True)
    ctx.exit(1)


@saves_group.command("status")
@click.argument("game_id", required=False)
@click.pass_context
def saves_status(ctx: click.Context, game_id: str | None) -> None:
    """Show save sync status."""
    click.echo("Save sync is not yet implemented in v0.1.", err=True)
    ctx.exit(1)
