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
import os
import sys
from pathlib import Path

import click

from romcloud.cli.context import get_container
from romcloud.lifecycle.setup import (
    apply_setup,
    browse_local,
    browse_sftp_directory,
    browse_smb_directory,
    discover_shares,
    probe_sftp_host_key,
    setup_state,
    validate_local_source,
    validate_sftp_source,
    validate_share,
)
from romcloud.core.progress import ProgressEvent, emit_progress, redact_text
from romcloud.infrastructure.config import load_config
from romcloud.infrastructure.capabilities import capability_policy
from romcloud.infrastructure.source_display import source_display_summary
from romcloud.infrastructure import savesync_prompts
from romcloud.integrations.batocera import startup_activation
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


def _require_capability_if_configured(
    ctx: click.Context, capability, operation: str
) -> None:  # noqa: ANN001
    """Apply persisted policy to optional setup/lifecycle endpoints."""
    path = Path(ctx.obj["config_path"])
    if not path.exists():
        return
    capability_policy(load_config(str(path))).require(capability, operation)


def _configured_update_channel(ctx: click.Context) -> str:
    """Return persisted channel, retaining stable behavior before config exists."""
    from romcloud.core.update_channels import DEFAULT_UPDATE_CHANNEL

    path = Path(ctx.obj["config_path"])
    if not path.is_file():
        return DEFAULT_UPDATE_CHANNEL.value
    return _load_context_config(ctx).update_channel


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
        config_path = Path(ctx.obj["config_path"])
        payload = setup_state(config_path)
        from romcloud import __version__
        from romcloud.core.update_channels import DEFAULT_UPDATE_CHANNEL, channel_label
        from romcloud.lifecycle.update import read_build_info

        channel = str(payload.get("update_channel") or DEFAULT_UPDATE_CHANNEL.value)
        build = read_build_info(config_path.parent.parent)
        version = build.version if build is not None else __version__
        revision = build.commit_short if build is not None else None
        identity = f"ROMCloud {version} — {channel_label(channel)}"
        if channel == "develop" and revision:
            identity += f" • {revision[:12]}"
        payload.update(
            update_channel=channel,
            build_version=version,
            build_revision=revision,
            build_identity=identity,
        )
        payload.update(
            startup_activation.activation_status(
                startup_activation.state_path(config_path.parent.parent)
            )
        )
        return payload

    _run_action(ctx, build)


@uidata_group.command("startup-integration-activated")
@click.pass_context
def uidata_startup_integration_activated(ctx: click.Context) -> None:
    """Compatibility activation endpoint; require a healthy manager."""

    def build() -> dict:
        config_path = Path(ctx.obj["config_path"])
        activation_path = startup_activation.state_path(config_path.parent.parent)
        config = _load_context_config(ctx)
        from romcloud.web.lifecycle import manager_status

        if not manager_status(config.data_path).get("running"):
            raise RuntimeError(
                "Library Manager is not healthy; startup activation was not recorded"
            )
        activated = startup_activation.mark_activated(activation_path)
        status = startup_activation.activation_status(activation_path)
        return {
            "startup_integration_activated": activated,
            "startup_restart_required": status["startup_restart_required"],
        }

    _run_action(ctx, build)


@uidata_group.command("startup-restart-now")
@click.pass_context
def uidata_startup_restart_now(ctx: click.Context) -> None:
    """Request a Batocera restart; activation clears on the next service start."""

    _run_action(ctx, startup_activation.request_reboot)


@uidata_group.command("setup-discover")
@click.pass_context
def uidata_setup_discover(ctx: click.Context) -> None:
    """Discover accessible SMB shares from a secret-bearing stdin request."""
    from romcloud.core.capabilities import Capability

    def run(request, progress=None):
        _require_capability_if_configured(ctx, Capability.REMOTE_VALIDATION, "SMB discovery")
        return discover_shares(request) if progress is None else discover_shares(request, progress)

    _run_request_action(ctx, run)


@uidata_group.command("setup-validate")
@click.pass_context
def uidata_setup_validate(ctx: click.Context) -> None:
    """Validate the selected source or remote-data storage target."""
    from romcloud.core.capabilities import Capability

    def validate(request, progress=None):
        purpose = str(request.get("purpose", "source")).strip().lower()
        provider = str(
            request.get(
                "remote_data_type" if purpose == "remote_data" else "source_type",
                "smb",
            )
        ).strip().lower()
        action = {
            "local": validate_local_source,
            "sftp": validate_sftp_source,
            "smb": validate_share,
        }.get(provider)
        if action is None:
            raise ValueError(
                f"Unsupported {purpose} storage provider: {provider or 'none'}"
            )
        if action is validate_share:
            _require_capability_if_configured(
                ctx, Capability.REMOTE_VALIDATION, "Storage validation"
            )
        return action(request, progress)

    _run_request_action(ctx, validate)


@uidata_group.command("setup-sftp-host-key")
@click.pass_context
def uidata_setup_sftp_host_key(ctx: click.Context) -> None:
    """Observe one SFTP host key for explicit wizard trust."""
    from romcloud.core.capabilities import Capability

    def probe(request, progress=None):
        _require_capability_if_configured(ctx, Capability.REMOTE_VALIDATION, "SFTP host-key lookup")
        return probe_sftp_host_key(request)

    _run_request_action(ctx, probe)


@uidata_group.command("setup-browse-smb")
@click.pass_context
def uidata_setup_browse_smb(ctx: click.Context) -> None:
    """Enumerate one directory inside a previously authenticated SMB share."""
    from romcloud.core.capabilities import Capability

    def browse(request, progress=None):
        _require_capability_if_configured(ctx, Capability.REMOTE_VALIDATION, "SMB browsing")
        return (
            browse_smb_directory(request)
            if progress is None
            else browse_smb_directory(request, progress)
        )

    _run_request_action(ctx, browse)


@uidata_group.command("setup-browse-local")
@click.pass_context
def uidata_setup_browse_local(ctx: click.Context) -> None:
    """Enumerate a local filesystem directory for the setup folder picker."""
    _run_request_action(ctx, lambda request, progress=None: browse_local(request))


@uidata_group.command("setup-browse-sftp")
@click.pass_context
def uidata_setup_browse_sftp(ctx: click.Context) -> None:
    """Enumerate one read-only directory through an authenticated SFTP account."""
    from romcloud.core.capabilities import Capability

    def browse(request, progress=None):
        _require_capability_if_configured(
            ctx, Capability.REMOTE_VALIDATION, "SFTP browsing"
        )
        return browse_sftp_directory(request, progress)

    _run_request_action(ctx, browse)


@uidata_group.command("setup-apply")
@click.pass_context
def uidata_setup_apply(ctx: click.Context) -> None:
    """Apply, mount, scan, and integrate a validated graphical setup."""
    from romcloud.core.capabilities import Capability

    def apply(request, progress=None):
        _require_capability_if_configured(ctx, Capability.REMOTE_VALIDATION, "Storage setup")
        return apply_setup(Path(ctx.obj["config_path"]), request, progress)

    _run_request_action(ctx, apply)


@uidata_group.command("system-selection-status")
@click.pass_context
def uidata_system_selection_status(ctx: click.Context) -> None:
    """Return detected and currently selected source systems."""
    from romcloud.services.system_selection import selection_status

    _run_action(
        ctx,
        lambda: selection_status(Path(ctx.obj["config_path"])),
    )


@uidata_group.command("system-selection-apply")
@click.pass_context
def uidata_system_selection_apply(ctx: click.Context) -> None:
    """Persist selected systems and reconcile catalog/game access."""
    from romcloud.services.system_selection import update_selection

    _run_request_action(
        ctx,
        lambda request, progress=None: update_selection(
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
        games = (
            container.catalog.list_games()
            if container.config.source.enabled
            else []
        )
        operating_state = capability_policy(container.config).serialize()
        operating_state["game_management_enabled"] = (
            container.config.source.enabled
        )
        payload = {
            "games_total": len(games),
            "game_access_mode": container.config.game_access_mode,
            "library_sync_enabled": container.config.library_sync.enabled,
            "operating_state": operating_state,
        }
        from romcloud.infrastructure.library_view import offline_library_enabled

        summary = (
            container.cache.status_summary()
            if container.config.source.enabled
            else {"complete": 0, "pinned": 0}
        )
        payload.update(
            cached=summary["complete"],
            pinned=summary["pinned"],
            offline_library_mode=offline_library_enabled(container.config),
        )
        payload.update(source_display_summary(container.config))
        return payload

    _run_action(ctx, build)


def _run_library_mode_action(ctx: click.Context, mode: str) -> None:
    def build() -> dict:
        _load_context_config(ctx)
        container = get_container(ctx)
        from romcloud.integrations.batocera.game_access import set_operating_mode

        progress = _progress_sink({"progress": True})
        label = f"{mode.title()} Mode"
        emit_progress(
            progress, "library", "reconcile", "running", f"Entering {label}…"
        )
        report = set_operating_mode(container.config, mode, progress=progress)
        emit_progress(
            progress, "library", "reconcile", "success", f"Entered {label}"
        )
        return {
            "offline_library_mode": report.offline,
            "operating_mode": mode,
            "visible_proxies": report.visible,
            "removed_proxies": report.removed,
            "restored_proxies": report.restored,
            "save_sync_available": report.save_sync_available,
            "save_reconcile": report.save_reconcile,
            "mode_changed": report.mode_changed,
            "es_restart_requested": report.es_restarted,
        }

    _run_action(ctx, build)


@uidata_group.command("library-offline")
@click.pass_context
def uidata_library_offline(ctx: click.Context) -> None:
    """Show only managed games that are playable locally."""
    _run_library_mode_action(ctx, "offline")


@uidata_group.command("library-cache")
@click.pass_context
def uidata_library_cache(ctx: click.Context) -> None:
    """Restore the managed catalog and on-demand cache behavior."""
    _run_library_mode_action(ctx, "cache")


@uidata_group.command("library-connected")
@click.pass_context
def uidata_library_connected(ctx: click.Context) -> None:
    """Use the configured primary source directly."""
    _run_library_mode_action(ctx, "connected")


@uidata_group.command("refresh")
@click.pass_context
def uidata_refresh(ctx: click.Context) -> None:
    """Refresh the catalog from the configured source; result as JSON."""

    _run_catalog_refresh(ctx, progress=None)


@uidata_group.command("refresh-progress")
@click.pass_context
def uidata_refresh_progress(ctx: click.Context) -> None:
    """Refresh with structured stderr events for the graphical UI."""

    _run_catalog_refresh(ctx, progress=_progress_sink({"progress": True}))


def _run_catalog_refresh(ctx: click.Context, progress) -> None:  # noqa: ANN001

    def build() -> dict:
        _load_context_config(ctx)
        container = get_container(ctx)
        result = container.catalog.refresh(progress=progress)
        from romcloud.integrations.batocera.game_access import reconcile_game_access
        from romcloud.core.capabilities import OperatingMode
        from romcloud.infrastructure.library_view import operating_mode

        access_result = reconcile_game_access(
            container.config, render_library_metadata=False
        )
        if operating_mode(container.config) is OperatingMode.CONNECTED:
            es_systems: list[str] = []
            es_missing: list[str] = []
        else:
            es_systems = list(access_result.es_included_systems)
            es_missing = list(access_result.es_missing_systems)
        errors = [f"{system}: {message}" for system, message in result.errors]
        return {
            "ok": not errors,
            "error": (
                f"Catalog refresh completed with {len(errors)} failed system(s)."
                if errors
                else ""
            ),
            "added": result.added,
            "updated": result.updated,
            "skipped": result.skipped,
            "removed": result.removed,
            "errors": errors,
            "warnings": result.warnings,
            "game_access_mode": container.config.game_access_mode,
            "direct_links_created": access_result.created,
            "direct_links_removed": access_result.removed,
            "es_systems": es_systems,
            "es_missing_systems": es_missing,
            "es_restart_required": True,
            "library_sync": None,
        }

    _run_action(ctx, build)


@uidata_group.command("library-sync-status")
@click.pass_context
def uidata_library_sync_status(ctx: click.Context) -> None:
    """Library Sync opt-in/connectivity/last-operation state."""
    def build() -> dict:
        _load_context_config(ctx)
        return get_container(ctx).library_sync.status()

    _run_action(ctx, build)


def _run_library_sync(ctx: click.Context, *, full: bool) -> None:
    def build() -> dict:
        _load_context_config(ctx)
        container = get_container(ctx)
        progress = _progress_sink({"progress": True})
        report = container.library_sync.sync(progress=progress, full=full)
        from romcloud.integrations.batocera.presentation import refresh_emulationstation

        refresh_emulationstation(container.config, container.game_repo.list_systems())
        return report.as_dict()

    _run_action(ctx, build)


@uidata_group.command("manager-status")
@click.pass_context
def uidata_manager_status(ctx: click.Context) -> None:
    """Return the supervised browser Library Manager state."""

    def build() -> dict:
        from romcloud.web.lifecycle import manager_status

        config = _load_context_config(ctx)
        return manager_status(config.data_path)

    _run_action(ctx, build)


@uidata_group.command("manager-start")
@click.pass_context
def uidata_manager_start(ctx: click.Context) -> None:
    """Start or surface the existing ``romcloud manager`` service."""

    def build() -> dict:
        from romcloud.web.lifecycle import start_manager

        config = _load_context_config(ctx)
        romcloud_bin = os.environ.get("ROMCLOUD_BIN") or str(
            Path(sys.executable).with_name("romcloud")
        )
        result = start_manager(romcloud_bin, config.data_path)
        activation_path = startup_activation.state_path(
            Path(ctx.obj["config_path"]).parent.parent
        )
        # A manual recovery on a later boot may activate the healthy service;
        # the boot-ID guard still prevents same-session setup from clearing it.
        startup_activation.mark_activated(activation_path)
        return result

    _run_action(ctx, build)


@uidata_group.command("manager-boot-start")
@click.pass_context
def uidata_manager_boot_start(ctx: click.Context) -> None:
    """Bounded boot entrypoint with durable attempt/failure evidence."""

    config_path = Path(ctx.obj["config_path"])
    activation_path = startup_activation.state_path(config_path.parent.parent)
    startup_activation.record_startup_attempt(activation_path)

    def build() -> dict:
        from romcloud.web.lifecycle import start_manager

        try:
            config = _load_context_config(ctx)
            romcloud_bin = os.environ.get("ROMCLOUD_BIN") or str(
                Path(sys.executable).with_name("romcloud")
            )
            result = start_manager(romcloud_bin, config.data_path)
            activated = startup_activation.mark_activated(activation_path)
            return {**result, "startup_integration_activated": activated}
        except Exception as exc:
            startup_activation.record_startup_failure(activation_path, str(exc))
            raise

    _run_action(ctx, build)


@uidata_group.command("manager-pair")
@click.pass_context
def uidata_manager_pair(ctx: click.Context) -> None:
    """Issue a short-lived LAN pairing code."""

    def build() -> dict:
        from romcloud.web.lifecycle import issue_pairing_code, start_manager

        config = _load_context_config(ctx)
        romcloud_bin = os.environ.get("ROMCLOUD_BIN") or str(
            Path(sys.executable).with_name("romcloud")
        )
        start_manager(romcloud_bin, config.data_path)
        return issue_pairing_code(config.data_path)

    _run_action(ctx, build)


@uidata_group.command("manager-stop")
@click.pass_context
def uidata_manager_stop(ctx: click.Context) -> None:
    """Stop the exact recorded Library Manager process."""

    def build() -> dict:
        from romcloud.web.lifecycle import stop_manager

        config = _load_context_config(ctx)
        return {"stopped": stop_manager(config.data_path)}

    _run_action(ctx, build)


@uidata_group.command("manager-open-local")
@click.option(
    "--allow-no-sandbox",
    is_flag=True,
    help="Explicitly disable sandboxing for a user-installed browser only.",
)
@click.pass_context
def uidata_manager_open_local(ctx: click.Context, allow_no_sandbox: bool) -> None:
    """Open the manager in the local fullscreen browser until it exits."""

    def build() -> dict:
        from romcloud.web.lifecycle import launch_local_browser, start_manager

        config = _load_context_config(ctx)
        romcloud_bin = os.environ.get("ROMCLOUD_BIN") or str(
            Path(sys.executable).with_name("romcloud")
        )
        start_manager(romcloud_bin, config.data_path)
        return launch_local_browser(
            config.data_path, allow_no_sandbox=allow_no_sandbox
        )

    _run_action(ctx, build)


@uidata_group.command("browser-runtime-status")
@click.pass_context
def uidata_browser_runtime_status(ctx: click.Context) -> None:
    """Report the independently managed local-browser dependency."""

    def build() -> dict:
        from romcloud.web.lifecycle import local_browser_runtime_status

        config = _load_context_config(ctx)
        return local_browser_runtime_status(config.data_path)

    _run_action(ctx, build)


@uidata_group.command("library-sync")
@click.pass_context
def uidata_library_sync(ctx: click.Context) -> None:
    """Run routine presence-only Library Quick Sync for the GUI."""
    _run_library_sync(ctx, full=False)


@uidata_group.command("library-sync-full")
@click.pass_context
def uidata_library_sync_full(ctx: click.Context) -> None:
    """Run explicit validating/repairing Library Full Sync for the GUI."""
    _run_library_sync(ctx, full=True)


@uidata_group.command("library-sync-preview")
@click.pass_context
def uidata_library_sync_preview(ctx: click.Context) -> None:
    """Return a lightweight source-metadata import preflight."""
    def build() -> dict:
        _load_context_config(ctx)
        return get_container(ctx).library_sync.preview_source_import().as_dict()

    _run_action(ctx, build)


@uidata_group.command("update-check")
@click.pass_context
def uidata_update_check(ctx: click.Context) -> None:
    """Check for an update through the shared updater; result as JSON."""

    def build() -> dict:
        import sys

        from romcloud.core.capabilities import Capability

        _require_capability_if_configured(ctx, Capability.UPDATE_NETWORK, "Update check")
        channel = _configured_update_channel(ctx)
        from romcloud.lifecycle.update import check_for_update

        progress = _progress_sink({"progress": True})
        home = Path(sys.prefix).parent
        try:
            result = check_for_update(
                home, channel=channel, progress=progress
            )
        except Exception as exc:
            emit_progress(
                progress,
                "update",
                "check_completed",
                "error",
                "Could not check for ROMCloud updates",
                detail=str(exc),
            )
            raise
        current_version = result.current.version if result.current else "unknown"
        return {
            "update_available": result.update_available,
            "current_version": current_version,
            "available_version": result.latest_version or result.latest_commit.short_sha,
            "available_commit": result.latest_commit.short_sha,
            "channel": channel,
        }

    _run_action(ctx, build)


@uidata_group.command("update-install")
@click.pass_context
def uidata_update_install(ctx: click.Context) -> None:
    """Install an update through the shared lifecycle updater."""

    def build() -> dict:
        import sys

        from romcloud.core.capabilities import Capability

        _require_capability_if_configured(ctx, Capability.UPDATE_NETWORK, "ROMCloud update")
        channel = _configured_update_channel(ctx)
        from romcloud.lifecycle.update import perform_update

        progress = _progress_sink({"progress": True})
        home = Path(sys.prefix).parent
        try:
            result = perform_update(
                home,
                Path(sys.executable),
                channel=channel,
                progress=progress,
            )
        except Exception as exc:
            emit_progress(
                progress,
                "update",
                "completed",
                "error",
                "ROMCloud update failed",
                detail=str(exc),
            )
            raise
        return {
            "version": result.new.version,
            "commit": result.new.commit_short,
            "channel": result.new.channel,
            "restart_required": True,
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
@click.option("--override", is_flag=True, help="Allow this request in Direct.")
@click.pass_context
def uidata_cache_status(ctx: click.Context, override: bool) -> None:
    """Cache summary as JSON."""

    def build() -> dict:
        _load_context_config(ctx)
        container = get_container(ctx)
        from romcloud.core.capabilities import OperatingMode
        from romcloud.infrastructure.library_view import operating_mode

        if operating_mode(container.config) is OperatingMode.CONNECTED and not override:
            raise ValueError(
                "Cache status is unavailable in Direct; pass --override "
                "for this request only."
            )
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


# ── Shared save/state continuity ───────────────────────────────────────────
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


def _conflict_prompt_dict(conflict) -> dict:
    artifacts = conflict.local.artifacts or conflict.remote.artifacts
    group_label = conflict.group_id
    if artifacts:
        artifact_path = Path(artifacts[0].relative_path)
        group_label = artifact_path.with_suffix("").as_posix()
    return {
        "conflict_id": conflict.conflict_id,
        "group_id": conflict.group_id,
        "group_label": group_label,
        "layout_id": conflict.layout_id,
        "detected_at": conflict.detected_at,
        "local": {
            "artifact_count": conflict.local.artifact_count,
            "total_bytes": conflict.local.total_bytes,
        },
        "remote": {
            "artifact_count": conflict.remote.artifact_count,
            "total_bytes": conflict.remote.total_bytes,
        },
    }


def _conflicts_for_prompt(saves, data_root: Path, source: str) -> list[dict]:  # noqa: ANN001
    active = {item.conflict_id: item for item in saves.get_state().active_conflicts}
    if source == "manual":
        # Manual recovery deliberately includes conflicts whose one-time
        # automatic prompt was previously dismissed.
        return [
            _conflict_prompt_dict(active[conflict_id])
            for conflict_id in sorted(active)
        ]
    if source != "automatic":
        raise ValueError("Conflict prompt source must be 'automatic' or 'manual'")

    result = []
    for conflict_id in savesync_prompts.pending_ids(data_root):
        conflict = active.get(conflict_id)
        if conflict is not None:
            result.append(_conflict_prompt_dict(conflict))
            continue
        # Manual resolution may race an automatic queued prompt. Remove only
        # the stale handoff; durable conflict history remains authoritative.
        savesync_prompts.complete(data_root, conflict_id)
    return result


@uidata_group.command("savesync-conflicts")
@click.pass_context
def uidata_savesync_conflicts(ctx: click.Context) -> None:
    """Return automatic queued or manual active conflicts for one resolver."""

    def run(request, progress=None):
        _ = progress
        config = _load_context_config(ctx)
        saves = get_container(ctx).saves
        data_root = Path(config.data_path)
        source = str(request.get("source", "automatic"))
        conflicts = _conflicts_for_prompt(saves, data_root, source)
        return {"source": source, "conflicts": conflicts}

    _run_request_action(ctx, run)


@uidata_group.command("savesync-conflict-action")
@click.pass_context
def uidata_savesync_conflict_action(ctx: click.Context) -> None:
    """Apply or defer one queued game-stop conflict inside SaveSync."""

    def run(request, progress=None):
        from romcloud.core.models.savesync import SaveConflictResolution

        conflict_id = str(request.get("conflict_id", ""))
        action = request.get("action")
        source = str(request.get("source", "automatic"))
        if action not in ("upload-local", "download-remote", "resolve-later"):
            raise ValueError("Unknown SaveSync conflict action")
        if source not in ("automatic", "manual"):
            raise ValueError("Unknown SaveSync conflict source")
        config = _load_context_config(ctx)
        data_root = Path(config.data_path)
        if source == "automatic" and not savesync_prompts.contains(
            data_root, conflict_id
        ):
            raise ValueError("This SaveSync conflict is not queued for this popup")
        saves = get_container(ctx).saves
        if not any(
            item.conflict_id == conflict_id for item in saves.get_state().active_conflicts
        ):
            raise ValueError("This SaveSync conflict is no longer active")
        if action == "resolve-later":
            record = None
        else:
            resolution = (
                SaveConflictResolution.KEEP_LOCAL
                if action == "upload-local"
                else SaveConflictResolution.KEEP_REMOTE
            )
            record = saves.resolve_conflict(
                conflict_id,
                resolution,
                progress=progress,
            )
        # Resolve Later dismisses only the exact automatic event queue entry.
        # It never acknowledges or resolves the authoritative conflict. A
        # successful manual overwrite also removes a now-stale auto handoff.
        if source == "automatic" or action != "resolve-later":
            remaining = savesync_prompts.complete(data_root, conflict_id)
        else:
            remaining = savesync_prompts.pending_ids(data_root)
        return {
            "handled_conflict_id": conflict_id,
            "source": source,
            "remaining": len(remaining),
            "record": _record_dict(record),
        }

    _run_request_action(ctx, run)


@uidata_group.command("savesync-status")
@click.pass_context
def uidata_savesync_status(ctx: click.Context) -> None:
    """SaveSync local/configured state without touching remote storage."""

    def build() -> dict:
        config = _load_context_config(ctx)
        saves = get_container(ctx).saves
        state = saves.get_state()
        return {
            "remote_configured": saves.is_remote_configured,
            "auto_sync_enabled": config.saves.auto_sync_enabled,
            "xbox_enabled": saves.xbox_enabled,
            "xbox_hdd_size_bytes": saves.xbox_hdd_size(),
            # Compatibility-only setting. The graphical SaveSync screen no
            # longer offers unsafe RPCS3 application-data inclusion.
            "rpcs3_installed_games_enabled": saves.rpcs3_installed_games_enabled,
            "sync_status": state.effective_status.value,
            "quick_sync_ready": state.quick_sync_ready,
            "quick_sync_cursor_generation": state.quick_sync_cursor_generation,
            "active_conflicts": len(state.active_conflicts),
            "last_upload": _record_dict(state.last_upload),
            "last_download": _record_dict(state.last_download),
            "last_reconcile": (
                state.last_reconcile.to_dict() if state.last_reconcile else None
            ),
        }

    _run_action(ctx, build)


@uidata_group.command("savesync-availability")
@click.pass_context
def uidata_savesync_availability(ctx: click.Context) -> None:
    """Validate configured writable ``[remote_data]`` storage as JSON."""

    def build() -> dict:
        _load_context_config(ctx)
        saves = get_container(ctx).saves
        access = saves.validate_remote_storage()
        state = saves.record_remote_observation(access)
        return {
            "remote_configured": saves.is_remote_configured,
            "remote_available": access.readable,
            "remote_readable": access.readable,
            "remote_writable": access.writable,
            # Keep the old boolean spelling in this provider-neutral endpoint
            # for internal callers migrating from the combined status call.
            "remote_reachable": access.reachable,
            "access": access.as_dict(),
            "detail": access.detail,
            "sync_status": state.effective_status.value,
            "active_conflicts": len(state.active_conflicts),
        }

    _run_action(ctx, build)


@uidata_group.command("savesync-preview")
@click.pass_context
def uidata_savesync_preview(ctx: click.Context) -> None:
    """Build an upload/download diff; result (including the diff itself,
    for a later `savesync-commit` call) as JSON."""

    def build() -> dict:
        request = _read_request()
        progress = _progress_sink(request)
        direction = request.get("direction")
        if direction not in ("upload", "download"):
            raise ValueError("direction must be 'upload' or 'download'")
        _load_context_config(ctx)
        saves = get_container(ctx).saves
        emit_progress(
            progress,
            "savesync",
            "preview",
            "running",
            f"Preparing SaveSync {direction} preview",
        )
        diff = saves.preview_upload() if direction == "upload" else saves.preview_download()
        emit_progress(
            progress,
            "savesync",
            "preview",
            "success",
            f"SaveSync {direction} preview ready",
            metadata={
                "added": len(diff.added),
                "changed": len(diff.changed),
                "removed": len(diff.removed),
                "conflicts": len(diff.conflicts),
            },
        )
        return {
            "diff": diff.to_dict(),
            "added": len(diff.added),
            "changed": len(diff.changed),
            "removed": len(diff.removed),
            "unchanged": len(diff.unchanged),
            "conflicts": len(diff.conflicts),
            "transfer_bytes": diff.transfer_bytes,
            "excluded_files": diff.excluded_files,
            "excluded_bytes": diff.excluded_bytes,
            "optional_groups": [
                {"group": group, "files": files, "bytes": size_bytes}
                for group, files, size_bytes in diff.optional_groups
            ],
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
        progress = _progress_sink(request)
        direction = request.get("direction")
        if direction not in ("upload", "download"):
            raise ValueError("direction must be 'upload' or 'download'")
        diff = SaveDiff.from_dict(request["diff"])
        if diff.direction != direction:
            raise ValueError("diff direction does not match requested direction")
        _load_context_config(ctx)
        saves = get_container(ctx).saves
        emit_progress(
            progress,
            "savesync",
            "commit",
            "running",
            f"Applying SaveSync {direction}",
        )
        record = (
            saves.commit_upload(diff, progress=progress)
            if direction == "upload"
            else saves.commit_download(diff, progress=progress)
        )
        emit_progress(
            progress,
            "savesync",
            "commit",
            "success",
            f"SaveSync {direction} complete",
            metadata={"artifact_count": record.artifact_count},
        )
        return {"record": _record_dict(record)}

    _run_action(ctx, build)


@uidata_group.command("savesync-settings")
@click.pass_context
def uidata_savesync_settings(ctx: click.Context) -> None:
    """Update heavyweight SaveSync opt-ins."""

    def build() -> dict:
        from dataclasses import replace

        from romcloud.infrastructure.config import write_config

        request = _read_request()
        progress = _progress_sink(request)
        config = _load_context_config(ctx)
        new_config = replace(
            config,
            saves=replace(
                config.saves,
                auto_sync_enabled=bool(
                    request.get(
                        "auto_sync_enabled", config.saves.auto_sync_enabled
                    )
                ),
                xbox_enabled=bool(
                    request.get("xbox_enabled", config.saves.xbox_enabled)
                ),
                rpcs3_installed_games_enabled=bool(
                    request.get(
                        "rpcs3_installed_games_enabled",
                        config.saves.rpcs3_installed_games_enabled,
                    )
                ),
            ),
        )
        write_config(new_config, ctx.obj["config_path"])
        emit_progress(
            progress,
            "savesync",
            "settings",
            "success",
            "SaveSync settings updated",
        )
        return {
            "auto_sync_enabled": new_config.saves.auto_sync_enabled,
            "xbox_enabled": new_config.saves.xbox_enabled,
            "rpcs3_installed_games_enabled": (
                new_config.saves.rpcs3_installed_games_enabled
            ),
        }

    _run_action(ctx, build)


@uidata_group.command("savesync-reconcile")
@click.pass_context
def uidata_savesync_reconcile(ctx: click.Context) -> None:
    """Run conflict-aware writable remote-data reconciliation."""

    def build() -> dict:
        request = _read_request()
        progress = _progress_sink(request)
        _load_context_config(ctx)
        saves = get_container(ctx).saves
        plan = saves.preview_reconciliation()
        report = saves.reconcile(progress=progress)
        return {"preflight": plan.to_dict(), "report": report.to_dict()}

    _run_action(ctx, build)


@uidata_group.command("savesync-full-sync")
@click.pass_context
def uidata_savesync_full_sync(ctx: click.Context) -> None:
    """Run authoritative Full Sync and establish Quick Sync baseline."""

    def build() -> dict:
        request = _read_request()
        progress = _progress_sink(request)
        _load_context_config(ctx)
        saves = get_container(ctx).saves
        report = saves.full_sync(progress=progress)
        state = saves.get_state()
        return {
            "report": report.to_dict(),
            "quick_sync_ready": state.quick_sync_ready,
            "quick_sync_cursor_generation": state.quick_sync_cursor_generation,
        }

    _run_action(ctx, build)


@uidata_group.command("savesync-quick-sync")
@click.pass_context
def uidata_savesync_quick_sync(ctx: click.Context) -> None:
    """Run journal-driven Quick Sync when a trusted baseline exists."""

    def build() -> dict:
        request = _read_request()
        progress = _progress_sink(request)
        _load_context_config(ctx)
        saves = get_container(ctx).saves
        result = saves.quick_sync(progress=progress)
        payload = {
            "status": result.status,
            "remote_generation": result.remote_generation,
            "cursor_before": result.cursor_before,
            "cursor_after": result.cursor_after,
            "processed_entries": result.processed_entries,
            "processed_groups": list(result.processed_groups),
            "reason": result.reason,
        }
        if result.report is not None:
            payload["report"] = result.report.to_dict()
        return payload

    _run_action(ctx, build)
