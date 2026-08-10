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
from pathlib import Path

import click

from romcloud.cli.context import get_container
from romcloud.lifecycle.setup import (
    apply_setup,
    browse_local,
    browse_smb_directory,
    discover_shares,
    setup_state,
    validate_local_source,
    validate_share,
)
from romcloud.core.progress import ProgressEvent, redact_text
from romcloud.infrastructure.config import load_config
from romcloud.infrastructure.source_display import source_display_summary
from romcloud.services.connections import (
    connection_status,
    mount_connections,
    unmount_connections,
)


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


def _load_context_config(ctx: click.Context):
    config = load_config(ctx.obj["config_path"])
    ctx.obj["config"] = config
    return config


def _read_request() -> dict:
    raw = click.get_text_stream("stdin").read()
    if not raw:
        raise ValueError("Request body is required.")
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise ValueError("Request body must be a JSON object.")
    return payload


def _progress_sink(request: dict):
    """Return an opt-in stderr event sink for the graphical client."""
    if not request.get("progress"):
        return None
    secrets = tuple(
        str(request.get(key, ""))
        for key in ("password", "remote_password")
    )

    def emit(event: ProgressEvent) -> None:
        click.echo(event.wire_line(*secrets), err=True)

    return emit


def _run_request_action(ctx: click.Context, action) -> None:
    def build():
        request = _read_request()
        progress = _progress_sink(request)
        try:
            return action(request, progress) if progress is not None else action(request)
        except Exception as exc:
            safe = redact_text(
                str(exc),
                str(request.get("password", "")),
                str(request.get("remote_password", "")),
            )
            raise ValueError(safe) from None

    _run_action(ctx, build)


@uidata_group.command("setup-status")
@click.pass_context
def uidata_setup_status(ctx: click.Context) -> None:
    """Report whether graphical setup is fresh, repairable, or complete."""

    def build() -> dict:
        return setup_state(Path(ctx.obj["config_path"]))

    _run_action(ctx, build)


@uidata_group.command("setup-discover")
@click.pass_context
def uidata_setup_discover(ctx: click.Context) -> None:
    """Discover accessible SMB shares from a secret-bearing stdin request."""
    _run_request_action(ctx, discover_shares)


@uidata_group.command("setup-validate")
@click.pass_context
def uidata_setup_validate(ctx: click.Context) -> None:
    """Validate a selected SMB share and report recognized systems."""
    def validate(request, progress):
        action = validate_local_source if request.get("source_type") == "local" else validate_share
        return action(request, progress)

    _run_request_action(ctx, validate)


@uidata_group.command("setup-browse-smb")
@click.pass_context
def uidata_setup_browse_smb(ctx: click.Context) -> None:
    """Enumerate one directory inside a previously authenticated SMB share."""
    _run_request_action(ctx, browse_smb_directory)


@uidata_group.command("setup-browse-local")
@click.pass_context
def uidata_setup_browse_local(ctx: click.Context) -> None:
    """Enumerate a local filesystem directory for the setup folder picker."""
    _run_request_action(ctx, lambda request, progress=None: browse_local(request))


@uidata_group.command("setup-apply")
@click.pass_context
def uidata_setup_apply(ctx: click.Context) -> None:
    """Apply, mount, scan, and integrate a validated graphical setup."""
    _run_request_action(
        ctx,
        lambda request, progress=None: apply_setup(
            Path(ctx.obj["config_path"]), request, progress
        ),
    )


@uidata_group.command("status")
@click.pass_context
def uidata_status(ctx: click.Context) -> None:
    """Catalog + cache summary as JSON."""

    def build() -> dict:
        _load_context_config(ctx)
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
        _load_context_config(ctx)
        container = get_container(ctx)
        result = container.catalog.refresh()
        from romcloud.integrations.batocera import es_config

        es_result = es_config.refresh(container.game_repo.list_systems())
        return {
            "added": result.added,
            "updated": result.updated,
            "skipped": result.skipped,
            "removed": result.removed,
            "errors": [f"{system}: {message}" for system, message in result.errors],
            "warnings": result.warnings,
            "es_systems": es_result.included_systems,
            "es_missing_systems": es_result.missing_systems,
            "es_restart_required": True,
        }

    _run_action(ctx, build)


@uidata_group.command("healthcheck")
@click.pass_context
def uidata_healthcheck(ctx: click.Context) -> None:
    """Source reachability as JSON (a lightweight subset of `romcloud healthcheck`)."""

    def build() -> dict:
        _load_context_config(ctx)
        container = get_container(ctx)
        config = container.config
        reachable = container.provider.is_reachable(config.source.rom_root)
        payload = {
            "source_provider": config.source.provider,
            "source_reachable": reachable,
            "remote_data_configured": container.saves.is_remote_configured,
            "remote_data_reachable": container.saves.is_remote_reachable(),
        }
        payload.update(source_display_summary(config))
        return payload

    _run_action(ctx, build)


@uidata_group.command("cache-status")
@click.pass_context
def uidata_cache_status(ctx: click.Context) -> None:
    """Cache summary as JSON."""

    def build() -> dict:
        _load_context_config(ctx)
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


@uidata_group.command("connection-status")
@click.pass_context
def uidata_connection_status(ctx: click.Context) -> None:
    """Return an approachable connection state and configured targets."""

    def build() -> dict:
        config = _load_context_config(ctx)
        return connection_status(config)

    _run_action(ctx, build)


@uidata_group.command("connection-mount")
@click.pass_context
def uidata_connection_mount(ctx: click.Context) -> None:
    """Mount configured network locations with streamed progress events."""

    def build() -> dict:
        config = _load_context_config(ctx)
        return mount_connections(config, _progress_sink({"progress": True}))

    _run_action(ctx, build)


@uidata_group.command("connection-unmount")
@click.pass_context
def uidata_connection_unmount(ctx: click.Context) -> None:
    """Unmount configured network locations with streamed progress events."""

    def build() -> dict:
        config = _load_context_config(ctx)
        return unmount_connections(config, _progress_sink({"progress": True}))

    _run_action(ctx, build)


# ── SaveSync v1 ────────────────────────────────────────────────────────────
#
# The graphical UI never re-implements selection/diffing/commit logic — it
# only calls the same romcloud.services.saves.SaveSyncService the CLI
# (`romcloud saves ...`) uses. `savesync-preview`/`savesync-commit` take
# their request via stdin, since each `romcloud uidata` invocation is a
# fresh, stateless process — the GUI round-trips the exact diff JSON it
# received from a preview call back into the matching commit call.


def _record_dict(record) -> dict | None:
    if record is None:
        return None
    return {
        "revision": record.revision,
        "timestamp": record.timestamp,
        "device_id": record.device_id,
        "artifact_count": record.artifact_count,
        "total_bytes": record.total_bytes,
    }


@uidata_group.command("savesync-status")
@click.pass_context
def uidata_savesync_status(ctx: click.Context) -> None:
    """SaveSync connectivity/settings/last-sync summary as JSON."""

    def build() -> dict:
        _load_context_config(ctx)
        saves = get_container(ctx).saves
        state = saves.get_state()
        return {
            "remote_configured": saves.is_remote_configured,
            "remote_reachable": saves.is_remote_reachable(),
            "xbox_enabled": saves.xbox_enabled,
            "xbox_hdd_size_bytes": saves.xbox_hdd_size(),
            "last_upload": _record_dict(state.last_upload),
            "last_download": _record_dict(state.last_download),
        }

    _run_action(ctx, build)


@uidata_group.command("savesync-preview")
@click.pass_context
def uidata_savesync_preview(ctx: click.Context) -> None:
    """Build an upload/download diff; result (including the diff itself,
    for a later `savesync-commit` call) as JSON."""

    def build() -> dict:
        request = _read_request()
        direction = request.get("direction")
        if direction not in ("upload", "download"):
            raise ValueError("direction must be 'upload' or 'download'")
        _load_context_config(ctx)
        saves = get_container(ctx).saves
        diff = saves.preview_upload() if direction == "upload" else saves.preview_download()
        return {
            "diff": diff.to_dict(),
            "added": len(diff.added),
            "changed": len(diff.changed),
            "removed": len(diff.removed),
            "unchanged": len(diff.unchanged),
            "transfer_bytes": diff.transfer_bytes,
        }

    _run_action(ctx, build)


@uidata_group.command("savesync-commit")
@click.pass_context
def uidata_savesync_commit(ctx: click.Context) -> None:
    """Commit a previously previewed upload/download; result as JSON.

    The request must carry the exact ``diff`` object a prior
    ``savesync-preview`` call returned — this process never trusts a
    diff it did not just compute itself for anything but its shape.
    """

    def build() -> dict:
        from romcloud.core.models.savesync import SaveDiff

        request = _read_request()
        direction = request.get("direction")
        if direction not in ("upload", "download"):
            raise ValueError("direction must be 'upload' or 'download'")
        diff = SaveDiff.from_dict(request["diff"])
        _load_context_config(ctx)
        saves = get_container(ctx).saves
        record = saves.commit_upload(diff) if direction == "upload" else saves.commit_download(diff)
        return {"record": _record_dict(record)}

    _run_action(ctx, build)


@uidata_group.command("savesync-settings")
@click.pass_context
def uidata_savesync_settings(ctx: click.Context) -> None:
    """Update SaveSync settings (currently only Original Xbox opt-in)."""

    def build() -> dict:
        from dataclasses import replace

        from romcloud.infrastructure.config import write_config

        request = _read_request()
        config = _load_context_config(ctx)
        new_config = replace(config, saves=replace(config.saves, xbox_enabled=bool(request.get("xbox_enabled", config.saves.xbox_enabled))))
        write_config(new_config, ctx.obj["config_path"])
        return {"xbox_enabled": new_config.saves.xbox_enabled}

    _run_action(ctx, build)
