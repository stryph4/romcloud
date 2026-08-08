"""romcloud launch — resolve a proxy, cache if needed, print the local ROM path.

This command is primarily useful for development, testing, and pre-caching
games before they are needed.

For actual Batocera launch from EmulationStation, ROMCloud's own ES override
(see ``romcloud es install``) configures the system's ``<command>`` to use
``romcloud-run`` as the transparent wrapper automatically. See
:mod:`romcloud.integrations.batocera.es_config` for how that override is
generated.

ROMCloud does not reconstruct a reduced Batocera launch from only the system
name and ROM path — the full ``emulatorlauncher`` argv passthrough is required
to preserve controller config, per-game settings, and any future arguments.
"""

from __future__ import annotations

import click

from romcloud.core.exceptions import ROMCloudError
from romcloud.cli.context import get_container


@click.command("launch")
@click.argument("proxy_path")
@click.option(
    "--no-ui",
    is_flag=True,
    help="Suppress the progress UI (useful for scripting).",
)
@click.pass_context
def launch_cmd(ctx: click.Context, proxy_path: str, no_ui: bool) -> None:
    """Resolve and cache PROXY_PATH; print the resulting local ROM path.

    Does not invoke emulatorlauncher.  Use ``romcloud-run`` as the
    EmulationStation ``<command>`` wrapper for actual Batocera launch.
    """
    container = get_container(ctx)

    try:
        game = container.catalog.resolve_proxy(proxy_path)
    except ROMCloudError as exc:
        click.echo(f"error: {exc}", err=True)
        ctx.exit(1)
        return

    # Fast path: already cached — just print the path.
    if container.cache.is_cached(game.id):
        launch_path = container.cache.get_launch_path(game.id)
        click.echo(launch_path)
        return

    # Need to transfer first.
    source_root = game.source_root
    if not container.provider.is_reachable(source_root):
        click.echo(
            f"error: Game is not cached and source is unreachable ({source_root}).",
            err=True,
        )
        ctx.exit(1)
        return

    if no_ui:
        try:
            launch_path = container.cache.cache_game(game.id)
        except ROMCloudError as exc:
            click.echo(f"error: {exc}", err=True)
            ctx.exit(1)
            return
    else:
        from romcloud.ui.progress import run_progress_transfer
        try:
            launch_path = run_progress_transfer(container.cache, game)
        except ROMCloudError as exc:
            click.echo(f"error: {exc}", err=True)
            ctx.exit(1)
            return
        except KeyboardInterrupt:
            click.echo("\nCancelled.", err=True)
            ctx.exit(130)
            return

    if launch_path is None:
        click.echo("error: Cache completed but launch path could not be determined.", err=True)
        ctx.exit(1)
        return

    click.echo(launch_path)
