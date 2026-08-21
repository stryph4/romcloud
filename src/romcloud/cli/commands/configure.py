"""romcloud configure — interactive configuration wizard."""

from __future__ import annotations

from pathlib import Path

import click

from romcloud.cli.smb_setup_wizard import (
    SMBConnectionDetails,
    run_smb_setup_wizard,
)
from romcloud.infrastructure.config import (
    AppConfig,
    CacheConfig,
    LoggingConfig,
    LibrarySyncConfig,
    RemoteDataConfig,
    SavesConfig,
    SFTPConfig,
    SMBConfig,
    SourceConfig,
    SMART_CACHE_MODE,
    DIRECT_NAS_MODE,
    paths_overlap,
    default_config_path,
    load_config,
    write_config,
)
from romcloud.core.exceptions import ProviderError
from romcloud.infrastructure.credentials import (
    write_remote_data_smb_password,
    write_sftp_password,
    write_smb_password,
)
from romcloud.infrastructure.providers.local import WritableLocalFilesystemProvider
from romcloud.infrastructure.providers.sftp import SFTPProvider, probe_host_key
from romcloud.infrastructure.smb_discovery_client import build_default_smb_discovery_service
from romcloud.infrastructure.atomic_file import atomic_write_text

import os


@click.command("configure")
@click.option(
    "--game-access-mode",
    default=None,
    type=click.Choice([SMART_CACHE_MODE, DIRECT_NAS_MODE], case_sensitive=False),
    help="Game access strategy: smart_cache or direct_nas.",
)
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
    "--remote-data-type",
    default=None,
    type=click.Choice(["none", "local", "smb"], case_sensitive=False),
    help="Writable ROMCloud data storage for SaveSync: none, local, or SMB.",
)
@click.option(
    "--remote-data-root",
    default=None,
    help="Explicit writable local/USB ROMCloud data directory.",
)
@click.option(
    "--library-sync/--no-library-sync",
    default=None,
    help="Enable opt-in synchronization of game metadata and scraped media.",
)
@click.option(
    "--source-type",
    default=None,
    type=click.Choice(["none", "local", "smb", "sftp"], case_sensitive=False),
    help=(
        "Where your ROMs live: 'none' (SaveSync only with local games), "
        "'local' (local disk or USB drive), "
        "'smb' (a network SMB/CIFS share, mounted locally and read from there), or "
        "'sftp' (an SFTP account, read directly over SSH)."
    ),
)
@click.option("--sftp-host", default=None, help="SFTP server hostname or IP.")
@click.option("--sftp-port", default=22, type=int, help="SFTP server port (default: 22).")
@click.option("--sftp-username", default=None, help="SFTP username.")
@click.option(
    "--sftp-host-key-fingerprint",
    default=None,
    help=(
        "Trusted SHA256 host-key fingerprint (see `romcloud sftp fingerprint`). "
        "Required for --non-interactive SFTP setup; prompted interactively otherwise."
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
    game_access_mode: str | None,
    remote_data_type: str | None,
    remote_data_root: str | None,
    library_sync: bool | None,
    source_type: str | None,
    sftp_host: str | None,
    sftp_port: int,
    sftp_username: str | None,
    sftp_host_key_fingerprint: str | None,
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
        click.echo("  sftp   SFTP account, read directly over SSH")
        click.echo("  none   SaveSync only; leave local Batocera games untouched")
        source_type = click.prompt(
            "Source type",
            type=click.Choice(["none", "local", "smb", "sftp"], case_sensitive=False),
            default="local",
        )
    source_type = (source_type or "local").lower()

    if source_type != "none" and game_access_mode is None and not non_interactive:
        click.echo("\nWhich initial ROMCloud operating mode should be used?")
        click.echo("  smart_cache  Cached Storage; copy to local storage on first launch")
        if source_type != "sftp":
            click.echo("  direct_nas   Direct; launch from the configured source")
            game_access_mode = click.prompt(
                "Game access mode",
                type=click.Choice([SMART_CACHE_MODE, DIRECT_NAS_MODE], case_sensitive=False),
                default=existing.game_access_mode if existing else SMART_CACHE_MODE,
            )
            if game_access_mode.lower() == DIRECT_NAS_MODE:
                click.echo("Direct launches games from the configured source.")
        else:
            click.echo("An SFTP source only supports Cached Storage (no Direct).")
            game_access_mode = SMART_CACHE_MODE
    game_access_mode = (
        game_access_mode
        or (existing.game_access_mode if existing else SMART_CACHE_MODE)
    ).lower()
    if source_type == "sftp" and game_access_mode == DIRECT_NAS_MODE:
        raise click.ClickException(
            "Direct requires filesystem semantics that an SFTP source does not "
            "provide; choose smart_cache instead."
        )

    # ── ROM root ──────────────────────────────────────────────────────────────
    if source_type != "none" and rom_root is None and not non_interactive:
        if source_type == "smb":
            rom_root = click.prompt(
                "Local mount point for the SMB share",
                default="/userdata/romcloud/source",
            )
        elif source_type == "sftp":
            rom_root = click.prompt(
                "Remote ROM path on the SFTP server",
                default="/mnt/user/ROMs",
            )
        else:
            rom_root = click.prompt("ROM root path", default="/mnt/rom-source/ROMs")
    if source_type == "none":
        rom_root = ""
    elif rom_root is None:
        rom_root = "/userdata/romcloud/source" if source_type == "smb" else "/userdata/roms"

    # ── SMB settings ──────────────────────────────────────────────────────────
    # Persisted regardless of how the source is actually read at runtime — the
    # mount manager (`romcloud mount ...`) needs this to mount the share at
    # rom_root; the storage provider used to read ROMs is always local.
    #
    # Nothing here is written to disk yet — see the "build and write" section
    # below. Discovery/validation happens entirely in memory; existing
    # configuration and credentials are left untouched unless the *entire*
    # wizard (including cache settings below) completes and the final
    # confirmation succeeds.
    smb_cfg = None
    smb_password: str | None = None
    source_setup_result = None
    if source_type == "smb":
        if not non_interactive:
            click.echo()
            discovery = build_default_smb_discovery_service()
            setup_result = run_smb_setup_wizard(discovery)
            if setup_result is None:
                click.echo("\nSetup cancelled — existing configuration left unchanged.")
                ctx.exit(1)
                return
            smb_cfg = SMBConfig(
                server=setup_result.server,
                share=setup_result.share,
                username=setup_result.username,
                port=setup_result.port,
            )
            smb_password = setup_result.password
            source_setup_result = setup_result
        else:
            smb_cfg = SMBConfig(server="localhost", share="ROMs", username="guest")

    # ── SFTP settings ─────────────────────────────────────────────────────────
    # A genuine protocol-level provider (see
    # romcloud.infrastructure.providers.sftp.SFTPProvider) — unlike SMB there
    # is no local mount step. The host key is never trusted blindly: it is
    # observed via `probe_host_key` (before any credential is sent) and must
    # be explicitly confirmed, or supplied already-verified via
    # --sftp-host-key-fingerprint for non-interactive setup.
    sftp_cfg = None
    sftp_password: str | None = None
    if source_type == "sftp":
        if not non_interactive:
            click.echo()
            host = sftp_host or click.prompt("SFTP host")
            port = sftp_port
            username = sftp_username or click.prompt("SFTP username")
            password = click.prompt("SFTP password", hide_input=True)
            fingerprint = sftp_host_key_fingerprint
            key_type = ""
            if not fingerprint:
                click.echo(f"\nObserving host key for {host}:{port} ...")
                try:
                    key_type, fingerprint = probe_host_key(host, port)
                except ProviderError as exc:
                    raise click.ClickException(str(exc)) from exc
                click.echo(f"Key type:    {key_type}")
                click.echo(f"Fingerprint: {fingerprint}")
                if not click.confirm(
                    "Trust this host key? Only confirm if you have verified it "
                    "through a trusted channel.",
                    default=False,
                ):
                    click.echo("\nSetup cancelled — existing configuration left unchanged.")
                    ctx.exit(1)
                    return
            sftp_cfg = SFTPConfig(
                host=host,
                username=username,
                port=port,
                host_key_type=key_type,
                host_key_fingerprint=fingerprint,
            )
            sftp_password = password
            click.echo("\nVerifying read access ...")
            probe_provider = SFTPProvider(
                host=sftp_cfg.host,
                username=sftp_cfg.username,
                port=sftp_cfg.port,
                password=sftp_password,
                trusted_host_key_fingerprint=sftp_cfg.host_key_fingerprint,
            )
            result = probe_provider.validate_access(rom_root)
            if not result.ok:
                raise click.ClickException(
                    f"SFTP source failed validation: {result.detail}"
                )
            click.echo("\u2713 Connected")
            click.echo("\u2713 Read access verified")
        else:
            if not (sftp_host and sftp_username and sftp_host_key_fingerprint):
                raise click.ClickException(
                    "Non-interactive SFTP setup requires --sftp-host, --sftp-username, "
                    "and --sftp-host-key-fingerprint (see `romcloud sftp fingerprint`)."
                )
            sftp_password = os.environ.get("ROMCLOUD_SFTP_PASSWORD")
            if not sftp_password:
                raise click.ClickException(
                    "Non-interactive SFTP setup requires the ROMCLOUD_SFTP_PASSWORD "
                    "environment variable."
                )
            sftp_cfg = SFTPConfig(
                host=sftp_host,
                username=sftp_username,
                port=sftp_port,
                host_key_fingerprint=sftp_host_key_fingerprint,
            )

    # ── general writable ROMCloud data storage ───────────────────────────────
    remote_data = existing.remote_data if existing is not None else None
    remote_password: str | None = None
    if remote_data_type is None and not non_interactive:
        use_remote = click.confirm(
            "Configure writable ROMCloud data storage for SaveSync?",
            default=remote_data is not None,
        )
        if use_remote:
            remote_data_type = click.prompt(
                "ROMCloud data storage type",
                type=click.Choice(["local", "smb"], case_sensitive=False),
                default=remote_data.provider if remote_data is not None else "smb",
            )
        else:
            remote_data_type = "none"
    elif remote_data_type is None:
        remote_data_type = remote_data.provider if remote_data is not None else "none"

    remote_data_type = remote_data_type.lower()
    if remote_data_type == "none":
        remote_data = None
    elif remote_data_type == "local":
        if remote_data_root is None and not non_interactive:
            existing_root = (
                remote_data.root
                if remote_data is not None and remote_data.provider == "local"
                else "/userdata/romcloud/remote"
            )
            remote_data_root = click.prompt(
                "Writable ROMCloud data directory",
                default=existing_root,
            )
        remote_data_root = remote_data_root or "/userdata/romcloud/remote"
        root_path = Path(remote_data_root)
        if not root_path.is_absolute():
            raise click.ClickException(
                "ROMCloud data directory must be an explicit absolute path."
            )
        effective_data_path = (
            Path(existing.data_path)
            if existing is not None
            else config_path.parent.parent / "data"
        )
        effective_saves_path = Path(
            existing.saves.local_path if existing is not None else SavesConfig().local_path
        )
        for label, other in (
            ("ROM source", Path(rom_root)),
            ("ROMCloud system data", effective_data_path),
            ("local save source", effective_saves_path),
        ):
            if paths_overlap(root_path, other):
                raise click.ClickException(
                    f"ROMCloud data directory must not overlap the {label}: {other}"
                )
        remote_data = RemoteDataConfig(provider="local", root=str(root_path))
    else:
        if non_interactive:
            if remote_data is None or remote_data.provider != "smb":
                raise click.ClickException(
                    "Non-interactive SMB remote data requires an existing configured target."
                )
        else:
            click.echo("\nChoose an independent writable SMB location for ROMCloud data.")
            discovery = build_default_smb_discovery_service()
            reused_connection = None
            if source_setup_result is not None and click.confirm(
                "Use the same server and credentials as the ROM library?",
                default=True,
            ):
                reused_connection = SMBConnectionDetails(
                    server=source_setup_result.server,
                    port=source_setup_result.port,
                    username=source_setup_result.username,
                    password=source_setup_result.password,
                )
            remote_result = run_smb_setup_wizard(
                discovery,
                purpose="ROMCloud data location",
                detect_systems=False,
                connection=reused_connection,
            )
            if remote_result is None:
                click.echo("\nSetup cancelled — existing configuration left unchanged.")
                ctx.exit(1)
                return
            remote_data = RemoteDataConfig(
                provider="smb",
                root="/userdata/romcloud/remote",
                smb=SMBConfig(
                    server=remote_result.server,
                    share=remote_result.share,
                    username=remote_result.username,
                    port=remote_result.port,
                ),
            )
        remote_password = remote_result.password

    if library_sync is None:
        library_sync = existing.library_sync.enabled if existing else False
    if library_sync and (remote_data is None or source_type == "none"):
        raise click.ClickException(
            "Library Sync requires ROMCloud game management and writable remote data."
        )
    if library_sync:
        click.echo("\nLibrary Sync will read existing source/NAS gamelist.xml files to initialize metadata.")
        click.echo("ROMCloud will not modify those source files; it manages local Batocera metadata only.")

    # ── cache ─────────────────────────────────────────────────────────────────
    if source_type != "none" and game_access_mode == SMART_CACHE_MODE and cache_root is None and not non_interactive:
        cache_root = click.prompt(
            "Cache directory",
            default="/userdata/romcloud/cache",
        )
    cache_root = cache_root or "/userdata/romcloud/cache"

    max_gb = existing.cache.max_size_gb if existing else 50.0
    min_free_gb = existing.cache.min_free_gb if existing else 5.0
    if source_type != "none" and game_access_mode == SMART_CACHE_MODE and not non_interactive:
        max_gb = click.prompt("Max cache size (GB)", default=50.0, type=float)
        min_free_gb = click.prompt("Min free disk space (GB)", default=5.0, type=float)

    if remote_data is not None and remote_data.provider == "local":
        root_path = Path(remote_data.root)
        if paths_overlap(root_path, Path(cache_root)):
            raise click.ClickException(
                f"ROMCloud data directory must not overlap the cache: {cache_root}"
            )
        root_path.mkdir(parents=True, exist_ok=True)
        validation = WritableLocalFilesystemProvider().validate_access(str(root_path))
        if not validation.ok:
            raise click.ClickException(
                f"ROMCloud data directory failed validation: {validation.detail}"
            )
        click.echo("\u2713 Connected")
        click.echo("\u2713 Read access verified")
        click.echo("\u2713 Write access verified")
        click.echo("\u2713 Cleanup verified")

    # ── build and write ───────────────────────────────────────────────────────
    # Advanced settings (local_roms_path, data_path, logging) are preserved
    # from the existing config.  They are NOT exposed in this wizard.
    #
    # source.provider is always "local": ROMCloud has exactly one implemented
    # storage provider today (LocalFilesystemProvider). An SMB source type
    # just means the share is mounted at rom_root first (see `romcloud mount`)
    # — the native SMBProvider stub remains unimplemented and is never
    # selected here.
    #
    # Everything below only happens after every prompt/confirmation above has
    # succeeded — config is written first, then credentials, each atomically
    # (write-temp-then-rename): a crash or failure partway through never
    # leaves a partial file, and if anything above failed/was cancelled we
    # never reach here at all, so the previous configuration and credentials
    # remain exactly as they were.
    _default_home = config_path.parent.parent
    config = AppConfig(
        source=SourceConfig(
            provider="none" if source_type == "none" else "local",
            rom_root=rom_root,
            selected_systems=(
                ()
                if source_type == "none"
                else existing.source.selected_systems if existing else None
            ),
        ),
        cache=CacheConfig(path=cache_root, max_size_gb=max_gb, min_free_gb=min_free_gb),
        local_roms_path=existing.local_roms_path if existing else "/userdata/roms",
        data_path=existing.data_path if existing else str(_default_home / "data"),
        logging=existing.logging if existing else LoggingConfig(
            level="INFO",
            path=str(_default_home / "logs"),
        ),
        smb=smb_cfg,
        remote_data=remote_data,
        saves=existing.saves if existing else SavesConfig(),
        library_sync=LibrarySyncConfig(enabled=bool(library_sync)),
        game_access_mode=game_access_mode,
    )

    previous_config_text = (
        config_path.read_text(encoding="utf-8") if config_path.exists() else None
    )
    written = write_config(config, str(config_path))
    click.echo(f"\nConfiguration written to {written}")

    # Reconfiguration switches only ROMCloud-owned access artifacts. A fresh
    # configuration has no catalog yet, so this is naturally a no-op.
    if existing is not None and config.source.enabled:
        from romcloud.integrations.batocera.game_access import reconcile_game_access

        try:
            reconcile_game_access(config)
        except RuntimeError as exc:
            if previous_config_text is not None:
                atomic_write_text(config_path, previous_config_text)
            else:
                config_path.unlink(missing_ok=True)
            raise click.ClickException(str(exc)) from exc

    if smb_password:
        creds_path = config_path.parent / "credentials.toml"
        write_smb_password(creds_path, smb_password)
        click.echo(f"Credentials written to {creds_path} (mode 0600)")
    if sftp_password:
        creds_path = config_path.parent / "credentials.toml"
        write_sftp_password(creds_path, sftp_password)
        click.echo(f"Credentials written to {creds_path} (mode 0600)")
    if remote_password:
        creds_path = config_path.parent / "credentials.toml"
        write_remote_data_smb_password(creds_path, remote_password)
        click.echo("Remote-data credentials stored separately (mode 0600)")

    if smb_cfg is not None or (
        remote_data is not None and remote_data.provider == "smb"
    ):
        click.echo(
            "Run `romcloud mount start` to mount the configured SMB location(s), then "
            "`romcloud healthcheck` to verify the setup."
        )
    else:
        click.echo("Run `romcloud healthcheck` to verify the setup.")
