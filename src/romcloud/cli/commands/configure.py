"""romcloud configure — interactive configuration wizard."""

from __future__ import annotations

from pathlib import Path

import click

from romcloud.infrastructure.config import (
    AppConfig,
    CacheConfig,
    LoggingConfig,
    SMBConfig,
    SourceConfig,
    default_config_path,
    load_config,
    write_config,
)
from romcloud.infrastructure.credentials import write_smb_password


@click.command("configure")
@click.option(
    "--rom-root",
    default=None,
    help="Path to your ROM library (local path, or the local mount point for an SMB share).",
)
@click.option(
    "--cache-root",
    default=None,
    help="Where to store cached ROMs locally.",
)
@click.option(
    "--source-type",
    default=None,
    type=click.Choice(["local", "smb"], case_sensitive=False),
    help=(
        "Where your ROMs live: 'local' (local disk or USB drive) or "
        "'smb' (a network SMB/CIFS share, mounted locally and read from there)."
    ),
)
@click.option(
    "--non-interactive",
    is_flag=True,
    help="Skip prompts; use provided options or defaults.",
)
@click.pass_context
def configure_cmd(
    ctx: click.Context,
    rom_root: str | None,
    cache_root: str | None,
    source_type: str | None,
    non_interactive: bool,
) -> None:
    """Interactive wizard to create or update romcloud.toml."""
    config_path = Path(ctx.obj.get("config_path") or str(default_config_path()))

    # Load any existing config so advanced/internal settings are preserved
    # when the wizard is re-run.  The wizard only prompts for user-facing
    # settings; everything else carries over unchanged.
    existing: AppConfig | None = None
    if config_path.exists():
        click.echo(f"Existing configuration found at {config_path}")
        if not non_interactive and not click.confirm("Update it?", default=True):
            return
        try:
            existing = load_config(str(config_path))
        except Exception:  # noqa: BLE001
            pass  # corrupt config — wizard will create a clean one

    # ── source type ───────────────────────────────────────────────────────────
    # This chooses *how ROMCloud reaches your ROMs*, not a low-level storage
    # implementation: an SMB share is mounted locally (see `romcloud mount`)
    # and always read through the same local filesystem code path as a plain
    # local/USB source — there is no separate "SMB provider" to select here.
    if source_type is None and not non_interactive:
        click.echo("\nWhere are your ROMs stored?")
        click.echo("  local  Local disk or USB drive")
        click.echo("  smb    Network share (SMB/CIFS), e.g. a NAS")
        source_type = click.prompt(
            "Source type",
            type=click.Choice(["local", "smb"], case_sensitive=False),
            default="local",
        )
    source_type = (source_type or "local").lower()

    # ── ROM root ──────────────────────────────────────────────────────────────
    if rom_root is None and not non_interactive:
        if source_type == "smb":
            rom_root = click.prompt(
                "Local mount point for the SMB share",
                default="/userdata/romcloud-source",
            )
        else:
            rom_root = click.prompt("ROM root path", default="/mnt/rom-source/ROMs")
    if rom_root is None:
        rom_root = "/userdata/romcloud-source" if source_type == "smb" else "/userdata/roms"

    # ── SMB settings ──────────────────────────────────────────────────────────
    # Persisted regardless of how the source is actually read at runtime — the
    # mount manager (`romcloud mount ...`) needs this to mount the share at
    # rom_root; the storage provider used to read ROMs is always local.
    smb_cfg = None
    if source_type == "smb":
        if not non_interactive:
            server = click.prompt("SMB server hostname or IP")
            share = click.prompt("SMB share name")
            username = click.prompt("SMB username", default="guest")
        else:
            server = "localhost"
            share = "ROMs"
            username = "guest"
        smb_cfg = SMBConfig(server=server, share=share, username=username)

        # Store password separately with restricted permissions.
        if not non_interactive:
            password = click.prompt(
                "SMB password (stored in credentials.toml, not echoed here)",
                hide_input=True,
                default="",
            )
            if password:
                creds_path = config_path.parent / "credentials.toml"
                write_smb_password(creds_path, password)
                click.echo(f"  Credentials written to {creds_path} (mode 0600)")

    # ── cache ─────────────────────────────────────────────────────────────────
    if cache_root is None and not non_interactive:
        cache_root = click.prompt(
            "Cache directory",
            default="/userdata/romcloud-cache",
        )
    cache_root = cache_root or "/userdata/romcloud-cache"

    max_gb: float = 50.0
    min_free_gb: float = 5.0
    if not non_interactive:
        max_gb = click.prompt("Max cache size (GB)", default=50.0, type=float)
        min_free_gb = click.prompt("Min free disk space (GB)", default=5.0, type=float)

    # ── build and write ───────────────────────────────────────────────────────
    # Advanced settings (local_roms_path, data_path, logging) are preserved
    # from the existing config.  They are NOT exposed in this wizard.
    #
    # source.provider is always "local": ROMCloud has exactly one implemented
    # storage provider today (LocalFilesystemProvider). An SMB source type
    # just means the share is mounted at rom_root first (see `romcloud mount`)
    # — the native SMBProvider stub remains unimplemented and is never
    # selected here.
    _default_home = config_path.parent.parent
    config = AppConfig(
        source=SourceConfig(provider="local", rom_root=rom_root),
        cache=CacheConfig(path=cache_root, max_size_gb=max_gb, min_free_gb=min_free_gb),
        local_roms_path=existing.local_roms_path if existing else "/userdata/roms",
        data_path=existing.data_path if existing else str(_default_home / "data"),
        logging=existing.logging if existing else LoggingConfig(
            level="INFO",
            path=str(_default_home / "logs"),
        ),
        smb=smb_cfg,
    )

    written = write_config(config, str(config_path))
    click.echo(f"\nConfiguration written to {written}")
    if smb_cfg is not None:
        click.echo(
            "Run `romcloud mount start` to mount the SMB share, then "
            "`romcloud healthcheck` to verify the setup."
        )
    else:
        click.echo("Run `romcloud healthcheck` to verify the setup.")
