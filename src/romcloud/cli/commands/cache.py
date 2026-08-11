"""romcloud cache — cache management sub-commands."""

from __future__ import annotations

import click

from romcloud.core.exceptions import CacheError, GameNotFoundError, GamePinnedError, ROMCloudError
from romcloud.cli.context import get_container


def _fmt_bytes(n: int) -> str:
    if n >= 1024**3:
        return f"{n / 1024**3:.1f} GB"
    if n >= 1024**2:
        return f"{n / 1024**2:.1f} MB"
    return f"{n / 1024:.1f} KB"


@click.group("cache")
@click.option(
    "--override",
    is_flag=True,
    help="Allow this cache command while Direct/NAS mode is configured.",
)
@click.pass_context
def cache_group(ctx: click.Context, override: bool) -> None:
    """Manage the local ROM cache."""
    ctx.ensure_object(dict)
    ctx.obj["cache_override"] = override


def _require_cache_mode(ctx: click.Context) -> None:
    from romcloud.infrastructure.config import DIRECT_NAS_MODE

    container = get_container(ctx)
    if (
        container.config.game_access_mode == DIRECT_NAS_MODE
        and not ctx.obj.get("cache_override", False)
    ):
        raise click.ClickException(
            "Cache commands are unavailable in Direct/NAS mode because games are "
            "played from the remote source. Re-run this one command with "
            "`romcloud cache --override ...` to proceed without changing the configured mode."
        )


@cache_group.command("status")
@click.argument("game_id", required=False)
@click.pass_context
def cache_status(ctx: click.Context, game_id: str | None) -> None:
    """List cached games (or show detail for GAME_ID)."""
    _require_cache_mode(ctx)
    container = get_container(ctx)

    if game_id:
        entry = container.cache_repo.get(game_id)
        if entry is None:
            click.echo(f"Game {game_id!r} is not cached.")
            return
        game = container.game_repo.get(game_id)
        title = game.title if game else game_id
        click.echo(f"  title:        {title}")
        click.echo(f"  status:       {entry.status.value}")
        click.echo(f"  pinned:       {'yes' if entry.is_pinned else 'no'}")
        click.echo(f"  size:         {_fmt_bytes(entry.size_bytes)}")
        click.echo(f"  cached_at:    {entry.cached_at.isoformat()}")
        click.echo(f"  last_played:  {entry.last_accessed.isoformat()}")
        click.echo(f"  path:         {entry.cache_path}")
        return

    entries = container.cache_repo.list_complete()
    if not entries:
        click.echo("Cache is empty.")
        return

    click.echo(f"\n{'Title':<45} {'System':<12} {'Size':>8}  {'Pinned'}")
    click.echo("─" * 80)
    for entry in entries:
        game = container.game_repo.get(entry.game_id)
        title = (game.title if game else entry.game_id)[:44]
        system = (game.system if game else "?")[:11]
        pinned = "✓" if entry.is_pinned else ""
        click.echo(f"  {title:<44} {system:<12} {_fmt_bytes(entry.size_bytes):>8}  {pinned}")
    click.echo()


@cache_group.command("add")
@click.argument("game_id")
@click.pass_context
def cache_add(ctx: click.Context, game_id: str) -> None:
    """Pre-cache a game (download from source now)."""
    _require_cache_mode(ctx)
    container = get_container(ctx)

    game = container.game_repo.get(game_id)
    if game is None:
        click.echo(f"error: Game {game_id!r} not found in catalog.", err=True)
        ctx.exit(1)
        return

    if container.cache.is_cached(game_id):
        click.echo(f"{game.title!r} is already cached.")
        return

    click.echo(f"Caching {game.title!r} ...")
    try:
        from romcloud.ui.progress import run_progress_transfer
        launch_path = run_progress_transfer(container.cache, game)
        click.echo(f"Cached at: {launch_path}")
    except ROMCloudError as exc:
        click.echo(f"error: {exc}", err=True)
        ctx.exit(1)


@cache_group.command("remove")
@click.argument("game_id")
@click.option("--force", is_flag=True, help="Remove even if pinned.")
@click.pass_context
def cache_remove(ctx: click.Context, game_id: str, force: bool) -> None:
    """Remove the cached copy of GAME_ID."""
    _require_cache_mode(ctx)
    container = get_container(ctx)

    game = container.game_repo.get(game_id)
    title = game.title if game else game_id

    try:
        container.cache.remove(game_id, force=force)
        click.echo(f"Removed cache for {title!r}.")
    except GamePinnedError as exc:
        click.echo(f"error: {exc}", err=True)
        ctx.exit(1)
    except ROMCloudError as exc:
        click.echo(f"error: {exc}", err=True)
        ctx.exit(1)


@cache_group.command("pin")
@click.argument("game_id")
@click.pass_context
def cache_pin(ctx: click.Context, game_id: str) -> None:
    """Pin GAME_ID so it is never auto-evicted."""
    _require_cache_mode(ctx)
    container = get_container(ctx)
    try:
        container.cache.pin(game_id)
        game = container.game_repo.get(game_id)
        title = game.title if game else game_id
        click.echo(f"Pinned {title!r}.")
    except CacheError as exc:
        click.echo(f"error: {exc}", err=True)
        ctx.exit(1)


@cache_group.command("unpin")
@click.argument("game_id")
@click.pass_context
def cache_unpin(ctx: click.Context, game_id: str) -> None:
    """Unpin GAME_ID (cached copy is kept; becomes eviction-eligible)."""
    _require_cache_mode(ctx)
    container = get_container(ctx)
    container.cache.unpin(game_id)
    game = container.game_repo.get(game_id)
    title = game.title if game else game_id
    click.echo(f"Unpinned {title!r}.")
