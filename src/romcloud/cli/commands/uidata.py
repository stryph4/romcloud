"""romcloud uidata — internal JSON data endpoints for the graphical Ports UI.

This is the **only** interface the graphical Ports app (``ports_gfx``,
which runs under Batocera's system Python — see ``scripts/install.sh``'s
``romcloud-ports`` wrapper) is allowed to use to reach ROMCloud's backend.
It is a deliberate process boundary:

- ``ports_gfx`` never imports anything from the ``romcloud`` package.
- It only shells out to ``<romcloud_bin> uidata <action>`` and parses a
  single JSON object from stdout (see ``ports_gfx.client.call_backend``).

Every command here prints **exactly one** JSON object to stdout and nothing
else — no progress text, no log lines — so the graphical client's parser
never has to guess which line is the payload. Logging (if any) must go
through the normal logging setup (file/stderr), never stdout. Every command
catches all exceptions and reports them as ``{"ok": false, "error": ...}``
rather than letting a traceback reach stdout; the process exit code is 0 on
success and 1 on failure, mirroring the JSON ``ok`` field for callers that
prefer to check exit status instead of parsing the payload.

Hidden from ``romcloud --help`` — this is an internal contract for the
graphical UI, not a user-facing command surface.
"""

from __future__ import annotations

import json

import click

from romcloud.cli.context import get_container
from romcloud.infrastructure.source_display import source_display_summary


def _emit(ctx: click.Context, payload: dict) -> None:
    """Print exactly one JSON line to stdout and set the process exit code."""
    click.echo(json.dumps(payload))
    if not payload.get("ok", False):
        ctx.exit(1)


def _run_action(ctx: click.Context, build_payload) -> None:
    try:
        payload = build_payload()
    except Exception as exc:  # noqa: BLE001 — must never leak a traceback to stdout
        _emit(ctx, {"ok": False, "error": str(exc)})
        return
    _emit(ctx, {"ok": True, **payload})


@click.group("uidata", hidden=True)
def uidata_group() -> None:
    """Internal: JSON data endpoints for the graphical Ports UI."""


@uidata_group.command("status")
@click.pass_context
def uidata_status(ctx: click.Context) -> None:
    """Catalog + cache summary as JSON."""

    def build() -> dict:
        container = get_container(ctx)
        games = container.catalog.list_games()
        summary = container.cache.status_summary()
        payload = {
            "games_total": len(games),
            "cached": summary["complete"],
            "pinned": summary["pinned"],
        }
        payload.update(source_display_summary(container.config))
        return payload

    _run_action(ctx, build)


@uidata_group.command("refresh")
@click.pass_context
def uidata_refresh(ctx: click.Context) -> None:
    """Refresh the catalog from the configured source; result as JSON."""

    def build() -> dict:
        container = get_container(ctx)
        result = container.catalog.refresh()
        return {
            "added": result.added,
            "skipped": result.skipped,
            "removed": result.removed,
            "errors": [f"{system}: {message}" for system, message in result.errors],
        }

    _run_action(ctx, build)


@uidata_group.command("healthcheck")
@click.pass_context
def uidata_healthcheck(ctx: click.Context) -> None:
    """Source reachability as JSON (a lightweight subset of `romcloud healthcheck`)."""

    def build() -> dict:
        container = get_container(ctx)
        config = container.config
        reachable = container.provider.is_reachable(config.source.rom_root)
        payload = {
            "source_provider": config.source.provider,
            "source_reachable": reachable,
        }
        payload.update(source_display_summary(config))
        return payload

    _run_action(ctx, build)


@uidata_group.command("cache-status")
@click.pass_context
def uidata_cache_status(ctx: click.Context) -> None:
    """Cache summary as JSON."""

    def build() -> dict:
        container = get_container(ctx)
        summary = container.cache.status_summary()
        return {
            "complete": summary["complete"],
            "pinned": summary["pinned"],
            "total_bytes": summary["total_bytes"],
            "free_bytes": summary["free_bytes"],
            "max_bytes": summary["max_bytes"],
            "min_free_bytes": summary["min_free_bytes"],
        }

    _run_action(ctx, build)
