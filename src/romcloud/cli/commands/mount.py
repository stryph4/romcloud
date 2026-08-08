"""romcloud mount — manage the mounted SMB ROM source.

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
from romcloud.infrastructure.credentials import cifs_credentials_path, load_smb_password, write_cifs_credentials_file
from romcloud.integrations.batocera import mount_service


@click.group("mount")
def mount_group() -> None:
    """Manage the mounted SMB ROM source."""


def _require_smb(ctx: click.Context):
    container = get_container(ctx)
    config = container.config
    if config.smb is None:
        click.echo(
            "No [smb] section configured — source is a plain local/USB path, "
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
    config = _require_smb(ctx)
    romcloud_home = _romcloud_home(config)

    try:
        diag = mount_worker.get_diagnostics(romcloud_home, config)
    except ROMCloudError as exc:
        click.echo(f"error: {exc}", err=True)
        ctx.exit(1)
        return

    click.echo(f"  Server:      //{config.smb.server}/{config.smb.share}")
    click.echo(f"  Mount point: {config.source.rom_root}")
    click.echo(f"  State:       {diag.label}")
    if diag.worker_pid is not None:
        click.echo(f"  Worker:      running (pid {diag.worker_pid})")
    if diag.last_state == "failed" and diag.last_detail:
        click.echo(f"  Last error:  {diag.last_detail}")
    if diag.last_timestamp:
        click.echo(f"  Last check:  {diag.last_timestamp}")


@mount_group.command("start")
@click.pass_context
def mount_start_cmd(ctx: click.Context) -> None:
    """Mount the configured SMB source (blocking; waits for reachability).

    For interactive/manual use only — the Batocera boot service uses
    ``romcloud mount boot-start`` instead, which never blocks.
    """
    config = _require_smb(ctx)

    password = load_smb_password(config.credentials_path)
    if not password:
        click.echo(
            "error: no SMB password stored. Run `romcloud configure` first.",
            err=True,
        )
        ctx.exit(1)
        return

    creds_path = cifs_credentials_path(config.credentials_path)
    write_cifs_credentials_file(creds_path, config.smb.username, password)

    try:
        outcome = mount.mount_cifs_source(
            server=config.smb.server,
            share=config.smb.share,
            mount_point=config.source.rom_root,
            credentials_path=creds_path,
            port=config.smb.port,
        )
    except ROMCloudError as exc:
        click.echo(f"error: {exc}", err=True)
        ctx.exit(1)
        return

    click.echo("Already mounted." if outcome.already_mounted else "Mounted.")


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

        if config.smb is None:
            click.echo("No [smb] section configured — nothing to do.")
            return

        romcloud_home = _romcloud_home(config)

        if mount.is_target_mounted(config.source.rom_root):
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
    """Stop any running mount worker, then unmount the configured SMB source."""
    config = _require_smb(ctx)
    romcloud_home = _romcloud_home(config)

    mount_worker.stop_worker(romcloud_home)

    try:
        did_unmount = mount.unmount_cifs_source(config.source.rom_root)
    except ROMCloudError as exc:
        click.echo(f"error: {exc}", err=True)
        ctx.exit(1)
        return

    click.echo("Unmounted." if did_unmount else "Was not mounted.")


@mount_group.command("install")
@click.pass_context
def mount_install_cmd(ctx: click.Context) -> None:
    """Install the Batocera boot-time service that mounts the source at startup."""
    config = _require_smb(ctx)

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
    if config.smb is not None:
        romcloud_home = _romcloud_home(config)
        mount_worker.stop_worker(romcloud_home)
        mount_worker.cleanup_runtime_state(romcloud_home)

    removed = mount_service.remove_service()
    if removed:
        click.echo(f"Removed boot service: {mount_service.SERVICE_SCRIPT_PATH}")
    else:
        click.echo("No ROMCloud mount service present — nothing to do.")
