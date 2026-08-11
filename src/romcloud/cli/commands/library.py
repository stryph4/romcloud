"""Manual Smart Cache library-presentation commands."""

from __future__ import annotations

import click

from romcloud.cli.context import get_container
from romcloud.infrastructure.config import DIRECT_NAS_MODE
from romcloud.infrastructure.library_view import offline_library_enabled
from romcloud.integrations.batocera import es_config
from romcloud.integrations.batocera.game_access import set_offline_library_mode


@click.group("library")
def library_group() -> None:
    """Control which Smart Cache games EmulationStation displays."""


def _require_smart_cache(ctx: click.Context):  # noqa: ANN202
    container = get_container(ctx)
    if container.config.game_access_mode == DIRECT_NAS_MODE:
        raise click.ClickException(
            "Offline Library Mode is unavailable in Direct/NAS mode."
        )
    return container


def _set(ctx: click.Context, enabled: bool) -> None:
    container = _require_smart_cache(ctx)
    try:
        report = set_offline_library_mode(container.config, enabled)
        es_config.refresh(container.game_repo.list_systems())
    except (RuntimeError, es_config.ESConfigError) as exc:
        raise click.ClickException(str(exc)) from exc
    label = "cached games only" if enabled else "full Smart Cache catalog"
    click.echo(
        f"Library now shows {label}: {report.visible} proxy file(s). "
        "Update game lists or restart EmulationStation to see the change."
    )


@library_group.command("status")
@click.pass_context
def library_status(ctx: click.Context) -> None:
    """Show the current Smart Cache library presentation."""
    container = get_container(ctx)
    if container.config.game_access_mode == DIRECT_NAS_MODE:
        click.echo("Offline Library Mode: unavailable (Direct/NAS mode)")
        return
    click.echo(
        "Offline Library Mode: "
        + ("cached games only" if offline_library_enabled(container.config) else "full library")
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
