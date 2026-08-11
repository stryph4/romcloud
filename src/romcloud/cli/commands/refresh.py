"""romcloud refresh — scan remote source and update proxy files."""

from __future__ import annotations

import click

from romcloud.core.exceptions import ProviderNotReachableError, ROMCloudError
from romcloud.cli.context import get_container


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

    from romcloud.core.capabilities import Capability
    from romcloud.infrastructure.capabilities import capability_policy

    try:
        capability_policy(container.config).require(Capability.CATALOG_REFRESH, "Catalog refresh")
    except ROMCloudError as exc:
        raise click.ClickException(str(exc)) from exc

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

        try:
            from romcloud.integrations.batocera.game_access import reconcile_game_access

            access_result = reconcile_game_access(
                container.config, render_library_metadata=False
            )
        except ROMCloudError as exc:
            click.echo(f"error: could not update EmulationStation integration — {exc}", err=True)
            ctx.exit(1)
            return

        from romcloud.core.capabilities import OperatingMode
        from romcloud.infrastructure.capabilities import capability_policy

        if capability_policy(container.config).effective_mode is not OperatingMode.CONNECTED:
            es_systems = getattr(
                access_result, "es_included_systems", container.game_repo.list_systems()
            )
            click.echo(
                "Updated EmulationStation registration for "
                f"{len(es_systems)} system(s)."
            )
        else:
            click.echo(
                "Updated Connected Mode exposure "
                f"({access_result.created} link(s) created, {access_result.removed} removed)."
            )
        es_missing = getattr(access_result, "es_missing_systems", ())
        if es_missing:
            click.echo(
                "warning: no Batocera system definition found for: "
                + ", ".join(es_missing),
                err=True,
            )
        click.echo("Update game lists or restart EmulationStation to show catalog changes.")

        if result.errors:
            ctx.exit(1)

    except ProviderNotReachableError as exc:
        click.echo(f"error: Source not reachable — {exc}", err=True)
        ctx.exit(1)
    except (ROMCloudError, RuntimeError) as exc:
        click.echo(f"error: {exc}", err=True)
        ctx.exit(1)
