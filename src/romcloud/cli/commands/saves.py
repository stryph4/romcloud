"""``romcloud saves`` — shared save/state continuity and force operations.

NAS mode uses safe three-way reconciliation. Upload/download are explicit
power-user overrides: preview first, then (after confirmation) the destination
eligible selection becomes an exact copy of the source selection. Both this CLI and the graphical UI
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
    click.echo(f"  Conflicts: {len(diff.conflicts)}")
    click.echo(f"  Excluded by policy: {diff.excluded_files}")
    click.echo(f"  Transfer size: {_human_size(diff.transfer_bytes)}")
    for group, files, size_bytes in diff.optional_groups:
        click.echo(
            f"  Optional group disabled ({group}): {files} file(s), "
            f"{_human_size(size_bytes)}"
        )
    for entry in diff.entries:
        if entry.change.value != "unchanged":
            click.echo(f"    [{entry.change.value}] {entry.relative_path}")


@click.group("saves")
def saves_group() -> None:
    """Shared Batocera save/state continuity."""


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
    rpcs3_files, rpcs3_bytes = saves.rpcs3_installed_games_size()
    click.echo(
        "RPCS3 installed games (very large, opt-in): "
        f"{'enabled' if saves.rpcs3_installed_games_enabled else 'disabled'}"
    )
    if rpcs3_files:
        click.echo(
            f"  Installed-title data: {rpcs3_files} file(s), {_human_size(rpcs3_bytes)}"
        )
    click.echo(
        "Automatic local-game saves: "
        f"{'included' if saves.include_local_games else 'excluded (managed games only)'}"
    )

    state = saves.get_state()
    for label, record in (("Last upload", state.last_upload), ("Last download", state.last_download)):
        if record is None:
            click.echo(f"{label}: never")
        else:
            click.echo(
                f"{label}: {record.timestamp} "
                f"({record.artifact_count} artifact(s), {_human_size(record.total_bytes)})"
            )
    if state.last_reconcile is not None:
        click.echo(
            "Last NAS reconciliation: "
            f"{state.last_reconcile.timestamp} "
            f"({state.last_reconcile.uploaded} uploaded, "
            f"{state.last_reconcile.downloaded} downloaded, "
            f"{state.last_reconcile.conflicts} conflict(s))"
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
    click.echo(
        f"\nWARNING: this will replace the {destination} eligible save/state "
        f"library with the {source} selection above. Policy-excluded content is preserved."
    )
    if diff.conflicts:
        click.echo(
            f"WARNING: {len(diff.conflicts)} conflict(s) will be resolved in favor of the {source} side."
        )
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


@saves_group.command("upload-all")
@click.option("--yes", is_flag=True, help="Upload without prompting.")
@click.pass_context
def saves_upload_all(ctx: click.Context, yes: bool) -> None:
    """Replace remote eligible save/state data with this device's data."""
    _run_commit(ctx, direction="upload", yes=yes)


@saves_group.command("download-all")
@click.option("--yes", is_flag=True, help="Download without prompting.")
@click.pass_context
def saves_download_all(ctx: click.Context, yes: bool) -> None:
    """Replace local eligible save/state data with the remote data."""
    _run_commit(ctx, direction="download", yes=yes)


@saves_group.command("reconcile")
@click.pass_context
def saves_reconcile(ctx: click.Context) -> None:
    """Apply non-conflicting local/remote changes and preserve conflicts."""
    saves = get_container(ctx).saves
    try:
        plan = saves.preview_reconciliation()
        click.echo(
            f"Preflight: {len(plan.uploads)} upload, {len(plan.downloads)} download, "
            f"{len(plan.conflicts)} conflict(s), {plan.excluded_files} excluded."
        )
        if plan.conflicts:
            click.echo("Conflicting local and remote versions will both remain untouched.")
        report = saves.reconcile()
    except ROMCloudError as exc:
        click.echo(f"error: {exc}", err=True)
        ctx.exit(1)
        return
    click.echo(
        f"Done. {report.uploaded} uploaded, {report.downloaded} downloaded, "
        f"{report.conflicts} conflict(s) preserved."
    )


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


def _set_rpcs3_installed_games_enabled(ctx: click.Context, enabled: bool) -> None:
    config = get_container(ctx).config
    write_config(
        replace(
            config,
            saves=replace(config.saves, rpcs3_installed_games_enabled=enabled),
        ),
        ctx.obj["config_path"],
    )


@saves_group.command("rpcs3-installed-games-enable")
@click.option("--yes", is_flag=True, help="Enable without prompting.")
@click.pass_context
def saves_rpcs3_installed_games_enable(ctx: click.Context, yes: bool) -> None:
    """Include RPCS3 installed titles in future synchronization."""
    saves = get_container(ctx).saves
    files, size_bytes = saves.rpcs3_installed_games_size()
    click.echo(
        "WARNING: Include RPCS3 Installed Games can transfer tens or hundreds "
        "of gigabytes and may take a very long time."
    )
    click.echo(
        f"Current local estimate: {files} file(s), {_human_size(size_bytes)}."
    )
    if not yes and not click.confirm("Include RPCS3 installed games?"):
        click.echo("Cancelled.")
        return
    _set_rpcs3_installed_games_enabled(ctx, True)
    click.echo("RPCS3 installed-game synchronization enabled.")


@saves_group.command("rpcs3-installed-games-disable")
@click.pass_context
def saves_rpcs3_installed_games_disable(ctx: click.Context) -> None:
    """Exclude RPCS3 installed titles (the safe default)."""
    _set_rpcs3_installed_games_enabled(ctx, False)
    click.echo("RPCS3 installed-game synchronization disabled.")


def _set_include_local_games(ctx: click.Context, enabled: bool) -> None:
    config = get_container(ctx).config
    write_config(
        replace(
            config,
            saves=replace(config.saves, include_local_games=enabled),
        ),
        ctx.obj["config_path"],
    )


@saves_group.command("local-games-enable")
@click.option("--yes", is_flag=True, help="Enable without prompting.")
@click.pass_context
def saves_local_games_enable(ctx: click.Context, yes: bool) -> None:
    """Include eligible non-ROMCloud game saves in automatic NAS sync."""
    click.echo(
        "This allows automatic NAS reconciliation of eligible save/state data "
        "for games that ROMCloud does not manage."
    )
    if not yes and not click.confirm("Include Local Games in Save Sync?"):
        click.echo("Cancelled.")
        return
    _set_include_local_games(ctx, True)
    click.echo("Local-game save synchronization enabled.")


@saves_group.command("local-games-disable")
@click.pass_context
def saves_local_games_disable(ctx: click.Context) -> None:
    """Return automatic sync to ROMCloud-managed games only."""
    _set_include_local_games(ctx, False)
    click.echo("Local-game save synchronization disabled.")
