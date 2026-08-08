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
    help="Path or share root of your ROM library.",
)
@click.option(
    "--cache-root",
    default=None,
    help="Where to store cached ROMs locally.",
)
@click.option(
    "--provider",
    default=None,
    type=click.Choice(["local", "smb"], case_sensitive=False),
    help="Storage provider type.",
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
    provider: str | None,
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

    # ── provider ──────────────────────────────────────────────────────────────
    if provider is None and not non_interactive:
        provider = click.prompt(
            "Storage provider",
            type=click.Choice(["local", "smb"], case_sensitive=False),
            default="local",
        )
    provider = (provider or "local").lower()

    # ── ROM root ──────────────────────────────────────────────────────────────
    if rom_root is None and not non_interactive:
        default_root = "/mnt/rom-source/ROMs" if provider == "local" else "ROMs"
        rom_root = click.prompt("ROM root path", default=default_root)
    rom_root = rom_root or "/userdata/roms"

    # ── SMB settings ──────────────────────────────────────────────────────────
    smb_cfg = None
    if provider == "smb":
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
    _default_home = config_path.parent.parent
    config = AppConfig(
        source=SourceConfig(provider=provider, rom_root=rom_root),
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
    click.echo("Run `romcloud healthcheck` to verify the setup.")
