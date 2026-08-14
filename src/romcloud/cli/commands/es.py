"""romcloud es — EmulationStation integration sub-commands.

Manages ROMCloud's own, separate EmulationStation override file
(``es_systems_romcloud.cfg``) — see :mod:`romcloud.integrations.batocera.es_config`.
Never touches Batocera's stock ``es_systems.cfg`` or any other override file.
"""

from __future__ import annotations

import click

from romcloud.cli.context import get_container
from romcloud.integrations.batocera import es_config
from romcloud.integrations.batocera.system_registry import SystemRegistryError


@click.group("es")
def es_group() -> None:
    """Manage ROMCloud's EmulationStation system overrides."""


@es_group.command("install")
@click.pass_context
def es_install_cmd(ctx: click.Context) -> None:
    """Generate and write the ROMCloud ES override for all managed systems."""
    container = get_container(ctx)
    managed = container.game_repo.list_systems()

    try:
        result = es_config.install(
            managed, system_registry=container.system_registry
        )
    except (es_config.ESConfigError, SystemRegistryError) as exc:
        click.echo(f"error: {exc}", err=True)
        ctx.exit(1)
        return

    _print_generation_result(result)


@es_group.command("refresh")
@click.pass_context
def es_refresh_cmd(ctx: click.Context) -> None:
    """Regenerate the ROMCloud ES override to match the current catalog."""
    container = get_container(ctx)
    managed = container.game_repo.list_systems()

    try:
        result = es_config.refresh(
            managed, system_registry=container.system_registry
        )
    except (es_config.ESConfigError, SystemRegistryError) as exc:
        click.echo(f"error: {exc}", err=True)
        ctx.exit(1)
        return

    _print_generation_result(result)


@es_group.command("status")
@click.pass_context
def es_status_cmd(ctx: click.Context) -> None:
    """Show the current state of ROMCloud's ES integration."""
    container = get_container(ctx)
    managed = container.game_repo.list_systems()

    try:
        st = es_config.status(managed, system_registry=container.system_registry)
    except SystemRegistryError as exc:
        click.echo(f"error: {exc}", err=True)
        ctx.exit(1)
        return

    click.echo("\nROMCloud EmulationStation integration")
    click.echo("─" * 50)
    click.echo(
        f"  Wrapper installed:    {'yes' if st.wrapper_installed else 'no'} "
        f"({es_config.WRAPPER_SCRIPT_PATH})"
    )
    click.echo(
        f"  Override present:     {'yes' if st.override_exists else 'no'} "
        f"({es_config.ROMCLOUD_OVERRIDE_PATH})"
    )
    click.echo(f"  Managed systems:      {', '.join(st.managed_systems) or '(none)'}")
    if st.override_exists:
        click.echo(
            f"  Systems in override:  {', '.join(st.systems_in_override) or '(none)'}"
        )
        click.echo(
            "  Up to date:           "
            + ("yes" if st.up_to_date else "no — run `romcloud es refresh`")
        )
    click.echo()


@es_group.command("remove")
@click.pass_context
def es_remove_cmd(ctx: click.Context) -> None:
    """Remove ROMCloud's ES override file (only the file ROMCloud owns)."""
    removed = es_config.remove()
    if removed:
        click.echo(f"Removed {es_config.ROMCLOUD_OVERRIDE_PATH}")
    else:
        click.echo("No ROMCloud ES override present — nothing to do.")


def _print_generation_result(result: "es_config.GeneratedOverride") -> None:
    click.echo(
        f"Wrote ES override for {len(result.included_systems)} system(s): "
        f"{', '.join(result.included_systems) or '(none)'}"
    )
    if result.missing_systems:
        click.echo(
            f"  Skipped (no stock definition found): {', '.join(result.missing_systems)}"
        )
    click.echo(f"  {es_config.ROMCLOUD_OVERRIDE_PATH}")
    click.echo("Restart EmulationStation to apply.")
