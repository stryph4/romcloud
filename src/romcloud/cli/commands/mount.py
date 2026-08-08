"""romcloud mount — manage the mounted SMB ROM source.

Only relevant for the "mount a CIFS share locally, then use the `local`
storage provider against the mount point" deployment model. If ROMCloud is
configured with a plain local/USB path (no ``[smb]`` section), these
commands are no-ops that explain there is nothing to mount.

See :mod:`romcloud.infrastructure.mount` for the underlying mount/unmount/
reachability logic and :mod:`romcloud.integrations.batocera.mount_service`
for the Batocera boot-time service integration.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import click

from romcloud.cli.context import get_container
from romcloud.core.exceptions import ROMCloudError
from romcloud.infrastructure import mount
from romcloud.infrastructure.credentials import load_smb_password, write_cifs_credentials_file
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


def _cifs_credentials_path(config) -> Path:
    return config.credentials_path.parent / "smb-cifs-credentials"


@mount_group.command("status")
@click.pass_context
def mount_status_cmd(ctx: click.Context) -> None:
    """Show whether the configured SMB source is currently mounted."""
    config = _require_smb(ctx)
    mount_point = config.source.rom_root

    try:
        mounted = mount.is_target_mounted(mount_point)
    except ROMCloudError as exc:
        click.echo(f"error: {exc}", err=True)
        ctx.exit(1)
        return

    click.echo(f"  Server:      //{config.smb.server}/{config.smb.share}")
    click.echo(f"  Mount point: {mount_point}")
    click.echo(f"  Mounted:     {'yes' if mounted else 'no'}")


@mount_group.command("start")
@click.pass_context
def mount_start_cmd(ctx: click.Context) -> None:
    """Mount the configured SMB source (waits for reachability; idempotent)."""
    config = _require_smb(ctx)

    password = load_smb_password(config.credentials_path)
    if not password:
        click.echo(
            "error: no SMB password stored. Run `romcloud configure` first.",
            err=True,
        )
        ctx.exit(1)
        return

    creds_path = _cifs_credentials_path(config)
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


@mount_group.command("stop")
@click.pass_context
def mount_stop_cmd(ctx: click.Context) -> None:
    """Unmount the configured SMB source."""
    config = _require_smb(ctx)

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
    _require_smb(ctx)

    romcloud_bin = shutil.which("romcloud") or "/userdata/system/romcloud/bin/romcloud"
    path = mount_service.install_service(romcloud_bin)
    click.echo(f"Installed boot service: {path}")
    click.echo(f"If not enabled automatically, run: batocera-services enable {mount_service.SERVICE_NAME}")


@mount_group.command("remove")
def mount_remove_cmd() -> None:
    """Remove the Batocera boot-time mount service (only ROMCloud's own script)."""
    removed = mount_service.remove_service()
    if removed:
        click.echo(f"Removed boot service: {mount_service.SERVICE_SCRIPT_PATH}")
    else:
        click.echo("No ROMCloud mount service present — nothing to do.")
