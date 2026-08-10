"""romcloud saves — SaveSync v1.

Manual, directional, whole-dataset synchronization of game-progress save
data. No automatic sync, no launch/exit hooks, no bidirectional
reconciliation. Upload/download are symmetric: preview a full diff first,
then (after confirmation) the destination becomes an exact copy of the
source selection. Both this CLI and the graphical UI
(``romcloud uidata savesync-*``) call the same
:class:`~romcloud.services.saves.SaveSyncService` — neither duplicates
selection, diffing, or commit logic.
"""

from __future__ import annotations

from dataclasses import replace

import click

from romcloud.cli.context import get_container
from romcloud.core.exceptions import ROMCloudError
from romcloud.core.models.savesync import SaveDiff
from romcloud.infrastructure.config import write_config


def _human_size(num_bytes: int) -> str:
    value = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{int(value)} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} TB"  # pragma: no cover — unreachable given the loop above


def _print_diff(diff: SaveDiff) -> None:
    click.echo(f"Preview ({diff.direction}):")
    click.echo(f"  Added:     {len(diff.added)}")
    click.echo(f"  Changed:   {len(diff.changed)}")
    click.echo(f"  Removed:   {len(diff.removed)}")
    click.echo(f"  Unchanged: {len(diff.unchanged)}")
    click.echo(f"  Transfer size: {_human_size(diff.transfer_bytes)}")
    for entry in diff.entries:
        if entry.change.value != "unchanged":
            click.echo(f"    [{entry.change.value}] {entry.relative_path}")


@click.group("saves")
def saves_group() -> None:
    """SaveSync v1 — upload/download game-progress save data."""


@saves_group.command("status")
@click.pass_context
def saves_status(ctx: click.Context) -> None:
    """Show connectivity, settings, and last successful upload/download."""
    saves = get_container(ctx).saves
    if not saves.is_remote_configured:
        click.echo("ROMCloud data storage: not configured (SaveSync unavailable)")
    else:
        click.echo(
            f"ROMCloud data storage: "
            f"{'reachable and writable' if saves.is_remote_reachable() else 'unreachable or read-only'}"
        )
    click.echo(f"Original Xbox (heavyweight, opt-in): {'enabled' if saves.xbox_enabled else 'disabled'}")
    xbox_size = saves.xbox_hdd_size()
    if xbox_size is not None:
        click.echo(f"  xbox_hdd.qcow2 size: {_human_size(xbox_size)}")

    state = saves.get_state()
    for label, record in (("Last upload", state.last_upload), ("Last download", state.last_download)):
        if record is None:
            click.echo(f"{label}: never")
        else:
            click.echo(
                f"{label}: {record.timestamp} "
                f"({record.artifact_count} artifact(s), {_human_size(record.total_bytes)})"
            )


@saves_group.command("preview-upload")
@click.pass_context
def saves_preview_upload(ctx: click.Context) -> None:
    """Preview what `saves upload` would change, without changing anything."""
    try:
        diff = get_container(ctx).saves.preview_upload()
    except ROMCloudError as exc:
        click.echo(f"error: {exc}", err=True)
        ctx.exit(1)
        return
    _print_diff(diff)


@saves_group.command("preview-download")
@click.pass_context
def saves_preview_download(ctx: click.Context) -> None:
    """Preview what `saves download` would change, without changing anything."""
    try:
        diff = get_container(ctx).saves.preview_download()
    except ROMCloudError as exc:
        click.echo(f"error: {exc}", err=True)
        ctx.exit(1)
        return
    _print_diff(diff)


def _run_commit(ctx: click.Context, *, direction: str, yes: bool) -> None:
    saves = get_container(ctx).saves
    try:
        diff = saves.preview_upload() if direction == "upload" else saves.preview_download()
    except ROMCloudError as exc:
        click.echo(f"error: {exc}", err=True)
        ctx.exit(1)
        return

    _print_diff(diff)
    if not diff.added and not diff.changed and not diff.removed:
        click.echo("Nothing to do — already in sync.")
        return

    destination, source = ("remote", "local") if direction == "upload" else ("local", "remote")
    click.echo(f"\nThis will make the {destination} save data an exact copy of the {source} selection above.")
    if not yes and not click.confirm(f"{direction.capitalize()} saves now?"):
        click.echo("Cancelled.")
        return

    try:
        record = saves.commit_upload(diff) if direction == "upload" else saves.commit_download(diff)
    except ROMCloudError as exc:
        click.echo(f"error: {exc}", err=True)
        ctx.exit(1)
        return

    click.echo(
        f"Done. Revision {record.revision} "
        f"({record.artifact_count} artifact(s), {_human_size(record.total_bytes)})."
    )


@saves_group.command("upload")
@click.option("--yes", is_flag=True, help="Upload without prompting.")
@click.pass_context
def saves_upload(ctx: click.Context, yes: bool) -> None:
    """Preview, then upload local saves — the remote dataset becomes an
    exact copy of the local selection."""
    _run_commit(ctx, direction="upload", yes=yes)


@saves_group.command("download")
@click.option("--yes", is_flag=True, help="Download without prompting.")
@click.pass_context
def saves_download(ctx: click.Context, yes: bool) -> None:
    """Preview, then download remote saves — the local dataset becomes an
    exact copy of the remote selection."""
    _run_commit(ctx, direction="download", yes=yes)


def _set_xbox_enabled(ctx: click.Context, enabled: bool) -> None:
    config = get_container(ctx).config
    write_config(replace(config, saves=replace(config.saves, xbox_enabled=enabled)), ctx.obj["config_path"])


@saves_group.command("xbox-enable")
@click.pass_context
def saves_xbox_enable(ctx: click.Context) -> None:
    """Enable Original Xbox save sync (disabled by default).

    xemu stores Original Xbox saves inside its own virtual hard drive, so
    ROMCloud must transfer the entire virtual drive to preserve them
    safely — it is treated as one opaque, atomic artifact and never
    modified or extracted.
    """
    saves = get_container(ctx).saves
    click.echo(
        "xemu stores Original Xbox saves inside its virtual hard drive "
        "(xbox_hdd.qcow2), so ROMCloud must transfer the entire virtual "
        "drive to preserve them safely."
    )
    size = saves.xbox_hdd_size()
    click.echo(f"Current size: {_human_size(size)}" if size is not None else "No xbox_hdd.qcow2 found locally yet.")
    _set_xbox_enabled(ctx, True)
    click.echo("Original Xbox save sync enabled.")


@saves_group.command("xbox-disable")
@click.pass_context
def saves_xbox_disable(ctx: click.Context) -> None:
    """Disable Original Xbox save sync (the default)."""
    _set_xbox_enabled(ctx, False)
    click.echo("Original Xbox save sync disabled.")
