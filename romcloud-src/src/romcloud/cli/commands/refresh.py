"""romcloud refresh — scan remote source and update proxy files."""

from __future__ import annotations

import click

from romcloud.core.exceptions import ProviderNotReachableError, ROMCloudError
from romcloud.cli.main import get_container


@click.command("refresh")
@click.option(
    "--system",
    default=None,
    metavar="SYSTEM",
    help="Limit refresh to a single system folder (e.g. ps2).",
)
@click.option("--dry-run", is_flag=True, help="Show what would happen; make no changes.")
@click.pass_context
def refresh_cmd(ctx: click.Context, system: str | None, dry_run: bool) -> None:
    """Scan the ROM source and create proxy files for new games."""
    container = get_container(ctx)

    if dry_run:
        click.echo("(dry-run: no changes will be made)")

    click.echo(f"Scanning {container.config.source.rom_root} ...")

    try:
        if dry_run:
            # Show what systems would be scanned.
            systems = container.provider.list_systems(container.config.source.rom_root)
            click.echo(f"Found systems: {', '.join(systems)}")
            return

        result = container.catalog.refresh()
        click.echo(str(result))

        if result.errors:
            ctx.exit(1)

    except ProviderNotReachableError as exc:
        click.echo(f"error: Source not reachable — {exc}", err=True)
        ctx.exit(1)
    except ROMCloudError as exc:
        click.echo(f"error: {exc}", err=True)
        ctx.exit(1)
