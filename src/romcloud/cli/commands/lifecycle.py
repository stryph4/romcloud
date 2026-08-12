"""Public lifecycle commands: repair, uninstall, and purge."""

from __future__ import annotations

from pathlib import Path

import click

from romcloud.core.exceptions import ConfigurationNotFoundError
from romcloud.infrastructure.config import (
    AppConfig,
    CacheConfig,
    LoggingConfig,
    SourceConfig,
    load_config,
)
from romcloud.lifecycle import manage


def _paths(ctx: click.Context, *, allow_missing: bool = False) -> tuple[Path, AppConfig]:
    config_path = Path(ctx.obj["config_path"])
    romcloud_home = config_path.parent.parent
    try:
        config = load_config(str(config_path))
    except ConfigurationNotFoundError:
        if not allow_missing:
            raise
        unknown_root = Path("/.__romcloud_missing_config__")
        config = AppConfig(
            source=SourceConfig(provider="local", rom_root=str(unknown_root / "source")),
            cache=CacheConfig(path=str(romcloud_home / "cache")),
            local_roms_path=str(unknown_root / "roms"),
            data_path=str(romcloud_home / "data"),
            logging=LoggingConfig(path=str(romcloud_home / "logs")),
        )
    return romcloud_home, config


def _project_root() -> Path:
    module_path = Path(__file__).resolve()
    for candidate in module_path.parents:
        if (candidate / "pyproject.toml").is_file():
            return candidate
    return module_path.parent


@click.command("repair")
@click.option("--system-python", default=None, hidden=True)
@click.pass_context
def repair_cmd(ctx: click.Context, system_python: str | None) -> None:
    """Restore ROMCloud runtime artifacts without deleting user data."""
    romcloud_home, config = _paths(ctx)
    try:
        report, lifecycle_report = manage.repair(
            config=config,
            romcloud_home=romcloud_home,
            project_root=_project_root(),
            system_python=system_python,
        )
    except Exception as exc:  # noqa: BLE001
        raise click.ClickException(str(exc)) from exc
    click.echo(f"Repaired CLI wrapper: {report.core.cli_wrapper}")
    click.echo(f"Repaired launch wrapper: {report.core.launch_wrapper}")
    click.echo(f"Restored proxies: {lifecycle_report.proxies_restored}")


@click.command("uninstall")
@click.option("--yes", is_flag=True, help="Remove without prompting.")
@click.pass_context
def uninstall_cmd(ctx: click.Context, yes: bool) -> None:
    """Remove ROMCloud runtime/integration while preserving recoverable data."""
    click.echo(
        "This removes ROMCloud runtime, launch integration, service, ES overlay, "
        "Ports entry, and generated proxies. Config, credentials, catalog, cache, "
        "and logs are preserved."
    )
    if not yes and not click.confirm("Continue with uninstall?"):
        click.echo("Uninstall cancelled.")
        return
    romcloud_home, config = _paths(ctx, allow_missing=True)
    try:
        report = manage.uninstall(config=config, romcloud_home=romcloud_home)
    except Exception as exc:  # noqa: BLE001
        raise click.ClickException(str(exc)) from exc
    click.echo(
        f"ROMCloud uninstalled. Removed proxies: {report.proxies_removed}; "
        f"Direct links: {report.direct_links_removed}"
    )


@click.command("purge")
@click.option("--yes", is_flag=True, help="Purge without prompting.")
@click.pass_context
def purge_cmd(ctx: click.Context, yes: bool) -> None:
    """Remove ROMCloud and all ROMCloud-owned persistent state."""
    click.echo(
        "This permanently removes ROMCloud runtime, integration, proxies, config, "
        "credentials, catalog, cache, logs, and runtime state. Real ROMs and "
        "unrelated Batocera files are not removed."
    )
    if not yes and not click.confirm("Permanently purge all ROMCloud state?"):
        click.echo("Purge cancelled.")
        return
    romcloud_home, config = _paths(ctx, allow_missing=True)
    try:
        report = manage.purge(config=config, romcloud_home=romcloud_home)
    except Exception as exc:  # noqa: BLE001
        raise click.ClickException(str(exc)) from exc
    click.echo(f"ROMCloud purged. Removed proxies: {report.proxies_removed}")
