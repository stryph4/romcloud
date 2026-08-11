"""romcloud refresh — scan remote source and update proxy files."""

from __future__ import annotations

import click

from romcloud.core.exceptions import ProviderNotReachableError, ROMCloudError
from romcloud.cli.context import get_container
from romcloud.integrations.batocera import es_config
from romcloud.infrastructure.config import DIRECT_NAS_MODE


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

        if getattr(getattr(container.config, "library_sync", None), "enabled", False):
            library_report = container.library_sync.sync()
            click.echo(
                "Library Sync: "
                f"{library_report.metadata_added} added, "
                f"{library_report.metadata_updated} updated, "
                f"{library_report.media_transferred} media transferred, "
                f"{len(library_report.conflicts)} conflicts, "
                f"{len(library_report.failures)} failures."
            )
            for failure in library_report.failures:
                click.echo(f"warning: {failure}", err=True)

        managed = container.game_repo.list_systems()
        try:
            from romcloud.integrations.batocera.game_access import reconcile_game_access

            access_result = reconcile_game_access(container.config)
            es_result = (
                None
                if container.config.game_access_mode == DIRECT_NAS_MODE
                else es_config.refresh(managed)
            )
            if es_result is None:
                es_config.remove()
        except es_config.ESConfigError as exc:
            click.echo(f"error: could not update EmulationStation integration — {exc}", err=True)
            ctx.exit(1)
            return

        if es_result is not None:
            click.echo(
                "Updated EmulationStation registration for "
                f"{len(es_result.included_systems)} system(s)."
            )
        else:
            click.echo(
                "Updated Direct/NAS exposure "
                f"({access_result.created} link(s) created, {access_result.removed} removed)."
            )
        if es_result is not None and es_result.missing_systems:
            click.echo(
                "warning: no Batocera system definition found for: "
                + ", ".join(es_result.missing_systems),
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
