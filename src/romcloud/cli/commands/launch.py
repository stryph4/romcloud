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
@click.option(
    "--override",
    is_flag=True,
    help="Allow this one cache-backed launch while Direct is active.",
)
@click.pass_context
def launch_cmd(ctx: click.Context, proxy_path: str, no_ui: bool, override: bool) -> None:
    """Resolve and cache PROXY_PATH; print the resulting local ROM path.

    Does not invoke emulatorlauncher.  Use ``romcloud-run`` as the
    EmulationStation ``<command>`` wrapper for actual Batocera launch.
    """
    container = get_container(ctx)
    from romcloud.core.capabilities import OperatingMode
    from romcloud.infrastructure.library_view import operating_mode

    if operating_mode(container.config) is OperatingMode.CONNECTED and not override:
        raise click.ClickException(
            "Proxy caching is unavailable in Direct. Batocera should launch "
            "the exposed source game directly; pass --override only for this command."
        )

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

    from romcloud.core.capabilities import Capability
    from romcloud.infrastructure.capabilities import capability_policy

    try:
        capability_policy(container.config).require(
            Capability.GAME_DOWNLOAD, "Launching an uncached game"
        )
    except ROMCloudError as exc:
        raise click.ClickException(str(exc)) from exc

    # Need to transfer first. Check reachability against the currently
    # configured source root, not the game's persisted (possibly historical)
    # `source_root` — see romcloud.services.transfer.TransferService.
    source_root = container.config.source.rom_root
    if not container.provider.is_reachable(source_root):
        click.echo(
            "error: Game is not cached and the configured source is unavailable. "
            f"Reconnect it and try again ({source_root}).",
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
