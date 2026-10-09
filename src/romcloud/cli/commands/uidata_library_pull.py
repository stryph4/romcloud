"""Read-only Library Sync endpoints for protocol-only SFTP data storage.

Kept separate from the larger ``uidata`` module so the Ports UI can expose
Library Sync pull semantics without weakening the normal publish-capable sync
path. Importing this module registers the commands on ``uidata_group``.
"""

from __future__ import annotations

import click

from romcloud.cli.commands.uidata import (
    _load_context_config,
    _progress_sink,
    _read_request,
    _run_action,
    uidata_group,
)
from romcloud.cli.context import get_container
from romcloud.core.capabilities import Capability
from romcloud.core.progress import emit_progress
from romcloud.infrastructure.capabilities import capability_policy


def _require_sftp_library_source(config) -> None:  # noqa: ANN001
    remote_data = getattr(config, "remote_data", None)
    if remote_data is None or getattr(remote_data, "provider", None) != "sftp":
        raise ValueError("Read-only Library Sync pull is only used with SFTP data storage.")
    if not config.library_sync.enabled:
        raise ValueError("Library Sync is disabled; enable it in setup first.")
    capability_policy(config).require(Capability.LIBRARY_SYNC, "Library Sync pull")


def _run_sftp_pull(ctx: click.Context, *, full: bool) -> None:
    def build() -> dict:
        request = _read_request()
        progress = _progress_sink(request)
        config = _load_context_config(ctx)
        _require_sftp_library_source(config)
        container = get_container(ctx)
        service = container.library_sync
        if not service.is_remote_reachable():
            raise ValueError("SFTP Library Sync data is not reachable.")

        label = "Full Pull" if full else "Quick Pull"
        emit_progress(
            progress,
            "library_sync",
            "pull",
            "running",
            f"{label}: reading metadata and media from SFTP…",
        )
        report = service.pull(full=full)

        from romcloud.integrations.batocera.presentation import refresh_emulationstation

        refresh_emulationstation(config, container.game_repo.list_systems())
        emit_progress(
            progress,
            "library_sync",
            "pull",
            "success",
            f"{label} complete",
            metadata=report.as_dict(),
        )
        return report.as_dict()

    _run_action(ctx, build)


@uidata_group.command("library-sync-operation-preview")
@click.pass_context
def uidata_library_sync_operation_preview(ctx: click.Context) -> None:
    """Return the GUI preflight appropriate for the configured data provider."""

    def build() -> dict:
        _read_request()  # Consume the standard GUI payload even when unused.
        config = _load_context_config(ctx)
        container = get_container(ctx)
        remote_data = config.remote_data
        read_only_sftp = (
            remote_data is not None and remote_data.provider == "sftp"
        )
        if not read_only_sftp:
            return {
                **container.library_sync.preview_source_import().as_dict(),
                "read_only": False,
            }

        _require_sftp_library_source(config)
        if not container.library_sync.is_remote_reachable():
            raise ValueError("SFTP Library Sync data is not reachable.")
        return {
            "read_only": True,
            "provider": "sftp",
            "games_eligible": 0,
            "systems": [],
            "gamelist_files": 0,
            "gamelist_bytes": 0,
            "media_references": 0,
            "artwork_references": 0,
            "video_references": 0,
            "other_media_references": 0,
            "message": (
                "SFTP is read-only. ROMCloud will pull existing Library Sync "
                "metadata and media without changing the server."
            ),
        }

    _run_action(ctx, build)


@uidata_group.command("library-sync-pull")
@click.pass_context
def uidata_library_sync_pull(ctx: click.Context) -> None:
    """Pull missing SFTP Library Sync metadata/media without remote writes."""
    _run_sftp_pull(ctx, full=False)


@uidata_group.command("library-sync-pull-full")
@click.pass_context
def uidata_library_sync_pull_full(ctx: click.Context) -> None:
    """Validate and repair local Library Sync data from read-only SFTP."""
    _run_sftp_pull(ctx, full=True)
