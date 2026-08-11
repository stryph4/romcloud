"""Explicit opt-in Library Sync commands."""

from __future__ import annotations

from dataclasses import replace

import click

from romcloud.cli.context import get_container
from romcloud.core.exceptions import LibrarySyncError
from romcloud.infrastructure.config import write_config


@click.group("library-sync")
def library_sync_group() -> None:
    """Synchronize canonical game metadata and scraped media."""


@library_sync_group.command("status")
@click.pass_context
def library_sync_status(ctx: click.Context) -> None:
    status = get_container(ctx).library_sync.status()
    click.echo(f"Library Sync: {'enabled' if status['enabled'] else 'disabled'}")
    click.echo(f"Remote data: {'configured' if status['remote_configured'] else 'not configured'}")
    if status.get("last_sync"):
        click.echo(f"Last sync: {status['last_sync']} ({status['last_direction']})")
    report = status.get("last_report")
    if isinstance(report, dict):
        click.echo(
            "Last result: "
            f"{report.get('metadata_added', 0)} metadata added, "
            f"{report.get('metadata_updated', 0)} updated, "
            f"{report.get('media_transferred', 0)} media transferred, "
            f"{report.get('unchanged', 0)} unchanged, "
            f"{len(report.get('conflicts', []))} conflicts, "
            f"{len(report.get('failures', []))} failures."
        )


@library_sync_group.command("enable")
@click.option("--yes", is_flag=True, help="Enable without confirmation.")
@click.pass_context
def library_sync_enable(ctx: click.Context, yes: bool) -> None:
    container = get_container(ctx)
    if container.config.remote_data is None:
        raise click.ClickException(
            "Configure writable ROMCloud data storage before enabling Library Sync."
        )
    click.echo("Existing source/NAS gamelist.xml files may be read to initialize metadata.")
    click.echo("ROMCloud will never modify those source files.")
    click.echo("ROMCloud will create and manage only its local Batocera metadata entries.")
    if not yes and not click.confirm("Enable Library Sync?", default=False):
        return
    config = replace(
        container.config,
        library_sync=replace(container.config.library_sync, enabled=True),
    )
    write_config(config, ctx.obj["config_path"])
    click.echo("Library Sync enabled. Run `romcloud library-sync sync` to initialize it.")


@library_sync_group.command("disable")
@click.pass_context
def library_sync_disable(ctx: click.Context) -> None:
    container = get_container(ctx)
    config = replace(
        container.config,
        library_sync=replace(container.config.library_sync, enabled=False),
    )
    write_config(config, ctx.obj["config_path"])
    click.echo("Library Sync disabled. Canonical and local metadata were preserved.")


def _run(ctx: click.Context, direction: str) -> None:
    service = get_container(ctx).library_sync
    try:
        report = getattr(service, direction)()
    except LibrarySyncError as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(
        f"{direction.title()} complete: {report.metadata_added} metadata added, "
        f"{report.metadata_updated} updated, {report.media_transferred} media transferred, "
        f"{report.unchanged} unchanged, {len(report.conflicts)} conflicts, "
        f"{len(report.failures)} failures."
    )
    for conflict in report.conflicts:
        click.echo(f"conflict: {conflict}", err=True)
    for failure in report.failures:
        click.echo(f"warning: {failure}", err=True)


@library_sync_group.command("pull")
@click.pass_context
def library_sync_pull(ctx: click.Context) -> None:
    """Pull canonical metadata and render this device's gamelists."""
    _run(ctx, "pull")


@library_sync_group.command("push")
@click.pass_context
def library_sync_push(ctx: click.Context) -> None:
    """Add local/source metadata to the canonical remote library."""
    _run(ctx, "push")


@library_sync_group.command("sync")
@click.pass_context
def library_sync_sync(ctx: click.Context) -> None:
    """Perform a safe additive bidirectional synchronization."""
    _run(ctx, "sync")


@library_sync_group.command("remove-local")
@click.option("--yes", is_flag=True, help="Remove without confirmation.")
@click.pass_context
def library_sync_remove_local(ctx: click.Context, yes: bool) -> None:
    """Remove ROMCloud-managed local entries; preserve canonical/source data."""
    if not yes and not click.confirm(
        "Remove only ROMCloud-managed entries from local gamelist.xml files?",
        default=False,
    ):
        return
    removed = get_container(ctx).library_sync.remove_local_metadata()
    click.echo(f"Removed {removed} ROMCloud-managed local metadata entries.")
