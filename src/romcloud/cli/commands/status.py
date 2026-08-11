"""romcloud status — catalog and cache summary."""

from __future__ import annotations

import click

from romcloud.cli.context import get_container
from romcloud.infrastructure.source_display import source_display_summary
from romcloud.infrastructure.config import DIRECT_NAS_MODE


def _fmt_bytes(n: int) -> str:
    if n >= 1024**3:
        return f"{n / 1024**3:.1f} GB"
    if n >= 1024**2:
        return f"{n / 1024**2:.1f} MB"
    if n >= 1024:
        return f"{n / 1024:.1f} KB"
    return f"{n} B"


@click.command("status")
@click.option("--system", default=None, metavar="SYSTEM", help="Filter by system.")
@click.pass_context
def status_cmd(ctx: click.Context, system: str | None) -> None:
    """Show catalog and cache summary."""
    container = get_container(ctx)
    source = source_display_summary(container.config)

    click.echo(f"\n  Source:  {source['source_type']}" + (f" ({source['source_description']})" if source.get("source_description") else ""))
    click.echo(
        "  Access:  "
        + ("Direct/NAS" if container.config.game_access_mode == DIRECT_NAS_MODE else "Smart Cache")
    )
    # Catalog stats.
    games = container.catalog.list_games(system)
    click.echo(f"\n{'─' * 50}")
    click.echo(f"  Catalog: {len(games)} games" + (f" [{system}]" if system else ""))
    click.echo(f"{'─' * 50}")

    if system and games:
        for game in games:
            entry = container.cache_repo.get(game.id)
            cached = ""
            # `container.cache.is_cached` (not just `entry.is_complete`) is
            # the single source of truth — it also verifies every required
            # asset (e.g. cue companions) is actually present on disk.
            if container.config.game_access_mode != DIRECT_NAS_MODE and entry and container.cache.is_cached(game.id):
                cached = " [cached" + (" pinned" if entry.is_pinned else "") + "]"
            click.echo(f"  {game.title}{cached}")
    elif not system:
        from collections import Counter
        by_system: Counter = Counter(g.system for g in games)
        for sys_, count in sorted(by_system.items()):
            click.echo(f"  {sys_:<20} {count:>5} games")

    if container.config.game_access_mode == DIRECT_NAS_MODE:
        click.echo("\n  Games are exposed directly; local cache/offline features are inactive.\n")
        return

    # Cache stats.
    summary = container.cache.status_summary()
    click.echo(f"\n{'─' * 50}")
    click.echo("  Cache")
    click.echo(f"{'─' * 50}")
    click.echo(f"  Path:    {container.config.cache.path}")
    click.echo(
        f"  Cached:  {summary['complete']} games  "
        f"({_fmt_bytes(summary['total_bytes'])})"
    )
    click.echo(f"  Pinned:  {summary['pinned']} games")
    click.echo(
        f"  Free:    {_fmt_bytes(summary['free_bytes'])} "
        f"(min: {_fmt_bytes(summary['min_free_bytes'])})"
    )
    click.echo(
        f"  Quota:   {_fmt_bytes(summary['total_bytes'])} / "
        f"{_fmt_bytes(summary['max_bytes'])}"
    )
    click.echo()
