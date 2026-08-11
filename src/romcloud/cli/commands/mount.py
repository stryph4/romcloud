"""romcloud mount — manage configured SMB-backed ROMCloud locations.

Only relevant for the "mount a CIFS share locally, then use the `local`
storage provider against the mount point" deployment model. If ROMCloud is
configured with a plain local/USB path (no ``[smb]`` section), these
commands are no-ops that explain there is nothing to mount.

Boot-time safety
----------------
Real Batocera 42 hardware testing showed that a Batocera custom service's
``start`` action running the (blocking) mount logic directly can hang or
disrupt boot when the network/NAS isn't ready yet — see
:mod:`romcloud.infrastructure.mount_worker` for the full design. As a
result there are two distinct entry points:

- ``romcloud mount start`` — blocking, interactive, for manual use over SSH.
  Behavior is unchanged from before.
- ``romcloud mount boot-start`` — what the generated Batocera service script
  actually calls for ``start``. Never blocks: it spawns a detached
  background worker (``romcloud mount worker``, internal) and returns
  immediately, regardless of whether the mount eventually succeeds.

See :mod:`romcloud.infrastructure.mount` for the underlying mount/unmount/
reachability logic and :mod:`romcloud.integrations.batocera.mount_service`
for the Batocera boot-time service integration.
"""

from __future__ import annotations

from pathlib import Path

import click

from romcloud.cli.context import get_container
from romcloud.core.exceptions import ROMCloudError
from romcloud.infrastructure import mount, mount_worker
from romcloud.integrations.batocera import mount_service
from romcloud.services.connections import mount_connections, unmount_connections


@click.group("mount")
def mount_group() -> None:
    """Manage SMB-backed ROM source and remote-data mounts."""


def _require_mounts(ctx: click.Context):
    container = get_container(ctx)
    config = container.config
    if not mount_worker.configured_mounts(config):
        click.echo(
            "No SMB-backed ROM source or remote-data location is configured — "
            "nothing to mount.",
        )
        ctx.exit(0)
    return config


def _romcloud_home(config) -> Path:
    return mount_worker.romcloud_home_from_config(config)


@mount_group.command("status")
@click.pass_context
def mount_status_cmd(ctx: click.Context) -> None:
    """Show detailed mount/worker status: mounted, waiting, worker running,
    last failure, or not configured."""
    config = _require_mounts(ctx)
    romcloud_home = _romcloud_home(config)

    try:
        diag = mount_worker.get_diagnostics(romcloud_home, config)
    except ROMCloudError as exc:
        click.echo(f"error: {exc}", err=True)
        ctx.exit(1)
        return

    targets = mount_worker.configured_mounts(config)
    for target in targets:
        mode = "read-only" if target.read_only else "read-write"
        click.echo(
            f"  {target.label}: //{target.smb.server}/{target.smb.share}"
            f"{f'/{target.smb.remote_path}' if getattr(target.smb, 'remote_path', '') else ''} "
            f"→ {target.mount_point} ({mode})"
        )
    click.echo(f"  State:       {diag.label}")
    if diag.worker_pid is not None:
        click.echo(f"  Worker:      running (pid {diag.worker_pid})")
    if diag.last_state == "failed" and diag.last_detail:
        click.echo(f"  Last error:  {diag.last_detail}")
    if diag.last_timestamp:
        click.echo(f"  Last check:  {diag.last_timestamp}")
    if diag.cached_endpoint:
        click.echo(f"  Cached IP:   {diag.cached_endpoint} (boot-time fast path)")


@mount_group.command("start")
@click.pass_context
def mount_start_cmd(ctx: click.Context) -> None:
    """Mount configured SMB locations (blocking; waits for reachability).

    For interactive/manual use only — the Batocera boot service uses
    ``romcloud mount boot-start`` instead, which never blocks.
    """
    config = _require_mounts(ctx)

    try:
        result = mount_connections(config)
    except (ROMCloudError, OSError) as exc:
        click.echo(f"error: {exc}", err=True)
        ctx.exit(1)
        return

    click.echo("Mounted." if result["changed"] else "Already mounted.")


@mount_group.command("boot-start")
@click.pass_context
def mount_boot_start_cmd(ctx: click.Context) -> None:
    """Non-blocking boot-time trigger, used by the Batocera service's `start`.

    Never waits on DNS/network/Tailscale/CIFS. Checks whether the source is
    already mounted or a worker is already running (both fast, local
    checks); otherwise spawns a detached background worker and returns
    immediately. Always exits 0 — "ROMCloud may fail; Batocera must not."
    """
    try:
        container = get_container(ctx)
        config = container.config

        if not mount_worker.configured_mounts(config):
            click.echo("No SMB-backed locations configured — nothing to do.")
            return

        romcloud_home = _romcloud_home(config)

        if mount_worker.all_configured_mounts_are_mounted(config):
            click.echo("Already mounted.")
            return

        existing_pid = mount_worker.is_worker_running(romcloud_home)
        if existing_pid is not None:
            click.echo(f"Mount worker already running (pid {existing_pid}).")
            return

        pid = mount_worker.spawn_worker(romcloud_home)
        click.echo(f"Mount worker started in background (pid {pid}).")
    except Exception as exc:  # noqa: BLE001 — must never fail Batocera boot
        click.echo(f"warning: could not start mount worker: {exc}", err=True)


@mount_group.command("worker", hidden=True)
@click.pass_context
def mount_worker_cmd(ctx: click.Context) -> None:
    """Internal: runs the actual wait-then-mount worker loop.

    Not for interactive use — spawned by ``romcloud mount boot-start`` as a
    detached background process.
    """
    container = get_container(ctx)
    config = container.config
    romcloud_home = _romcloud_home(config)
    code = mount_worker.run_worker(romcloud_home, config)
    ctx.exit(code)


@mount_group.command("stop")
@click.pass_context
def mount_stop_cmd(ctx: click.Context) -> None:
    """Stop the mount worker, then unmount every configured SMB location."""
    config = _require_mounts(ctx)
    try:
        result = unmount_connections(config)
    except ROMCloudError as exc:
        click.echo(f"error: {exc}", err=True)
        ctx.exit(1)
        return
    click.echo("Unmounted." if result["changed"] else "Was not mounted.")


@mount_group.command("install")
@click.pass_context
def mount_install_cmd(ctx: click.Context) -> None:
    """Install the Batocera boot-time service for configured SMB locations."""
    config = _require_mounts(ctx)

    romcloud_bin = _romcloud_home(config) / "bin" / "romcloud"
    path = mount_service.install_service(str(romcloud_bin))
    click.echo(f"Installed boot service: {path}")
    click.echo(f"If not enabled automatically, run: batocera-services enable {mount_service.SERVICE_NAME}")


@mount_group.command("remove")
@click.pass_context
def mount_remove_cmd(ctx: click.Context) -> None:
    """Remove the Batocera boot-time mount service and ROMCloud's own
    runtime state (only ROMCloud-owned files; never touches other services)."""
    container = get_container(ctx)
    config = container.config
    targets = mount_worker.configured_mounts(config)
    errors: list[str] = []
    if targets:
        romcloud_home = _romcloud_home(config)
        mount_worker.stop_worker(romcloud_home)
        for target in reversed(targets):
            try:
                mount.unmount_cifs_source(target.mount_point)
            except ROMCloudError as exc:
                errors.append(f"{target.label}: {exc}")
        mount_worker.cleanup_runtime_state(romcloud_home)

    removed = mount_service.remove_service()
    if removed:
        click.echo(f"Removed boot service: {mount_service.SERVICE_SCRIPT_PATH}")
    else:
        click.echo("No ROMCloud mount service present — nothing to do.")
    if errors:
        click.echo(f"error: {'; '.join(errors)}", err=True)
        ctx.exit(1)
