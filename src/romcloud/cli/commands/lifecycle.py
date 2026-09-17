"""Public lifecycle commands: repair, uninstall, and purge."""

from __future__ import annotations

import os
import time
from pathlib import Path

import click

from romcloud.core.exceptions import ConfigurationNotFoundError
from romcloud.infrastructure.config import (
    AppConfig,
    CacheConfig,
    LoggingConfig,
    SourceConfig,
    load_config,
    load_config_read_only,
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


def _lifecycle_paths(ctx: click.Context) -> tuple[Path, AppConfig, bool]:
    """Load lifecycle configuration without migrations or credential writes."""
    config_path = Path(ctx.obj["config_path"])
    try:
        return config_path.parent.parent, load_config_read_only(str(config_path)), True
    except ConfigurationNotFoundError:
        # The placeholder authorizes no config-derived cleanup.  It exists only
        # to let independently verified fixed-path integrations be inspected.
        unknown_root = Path("/.__romcloud_missing_config__")
        return (
            config_path.parent.parent,
            AppConfig(
                source=SourceConfig(provider="local", rom_root=str(unknown_root / "source")),
                cache=CacheConfig(path=str(unknown_root / "cache")),
                local_roms_path=str(unknown_root / "roms"),
                data_path=str(unknown_root / "data"),
                logging=LoggingConfig(path=None),
            ),
            False,
        )


def _wait_for_handoff(pid: int | None, *, timeout: float = 30.0) -> None:
    if pid is None:
        return
    if pid <= 1 or pid == os.getpid():
        raise RuntimeError("Invalid graphical lifecycle handoff PID")
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        except PermissionError as exc:
            raise RuntimeError("Cannot verify graphical lifecycle handoff") from exc
        time.sleep(0.05)
    raise RuntimeError("Graphical ROMCloud process did not exit; no lifecycle changes were made")


def _render_report(report: manage.LifecycleReport) -> None:
    for stage in report.stages:
        detail = f": {stage.detail}" if stage.detail else ""
        click.echo(f"  {stage.name}: {stage.status}{detail}")
    for warning in report.warnings:
        click.echo(f"  warning: {warning}")


@click.command("repair")
@click.option("--system-python", default=None, hidden=True)
@click.pass_context
def repair_cmd(ctx: click.Context, system_python: str | None) -> None:
    """Restore ROMCloud from its configured channel without deleting user data."""
    romcloud_home, config = _paths(ctx)
    try:
        from romcloud.lifecycle.update import perform_repair

        result = perform_repair(
            romcloud_home,
            romcloud_home / "venv" / "bin" / "python",
            channel=config.update_channel,
            system_python=system_python,
        )
    except Exception as exc:  # noqa: BLE001
        raise click.ClickException(str(exc)) from exc
    if result.warnings:
        click.echo(
            f"Repair completed with warnings for ROMCloud {result.new.version} "
            f"({result.new.commit_short}) from {result.new.channel}."
        )
    else:
        click.echo(
            f"Repaired ROMCloud {result.new.version} ({result.new.commit_short}) "
            f"from {result.new.channel}."
        )
    if result.reconcile_log:
        click.echo(result.reconcile_log)
    if result.es_restart_required:
        click.echo("Restart EmulationStation to apply the repaired integration.")


@click.command("uninstall")
@click.option("--yes", is_flag=True, help="Remove without prompting.")
@click.option("--wait-for-pid", type=int, default=None, hidden=True)
@click.pass_context
def uninstall_cmd(ctx: click.Context, yes: bool, wait_for_pid: int | None) -> None:
    """Remove ROMCloud runtime/integration while preserving recoverable data."""
    click.echo(
        "This removes ROMCloud runtime, launch integration, service, ES overlay, "
        "Ports entry, and verified generated presentation. Settings, credentials, "
        "catalog, cached games, downloads, and sync state are preserved."
    )
    if not yes and not click.confirm("Continue with uninstall?"):
        click.echo("Uninstall cancelled.")
        return
    try:
        _wait_for_handoff(wait_for_pid)
        romcloud_home, config, trusted = _lifecycle_paths(ctx)
        report = manage.uninstall(
            config=config,
            romcloud_home=romcloud_home,
            config_trusted=trusted,
        )
    except manage.LifecycleFailure as exc:
        _render_report(exc.report)
        raise click.ClickException(str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise click.ClickException(str(exc)) from exc
    _render_report(report)
    click.echo(
        f"ROMCloud uninstalled. Removed proxies: {report.proxies_removed}; "
        f"Direct links: {report.direct_links_removed}"
    )


@click.command("purge")
@click.option("--yes", is_flag=True, help="Purge without prompting.")
@click.option("--wait-for-pid", type=int, default=None, hidden=True)
@click.pass_context
def purge_cmd(ctx: click.Context, yes: bool, wait_for_pid: int | None) -> None:
    """Remove ROMCloud and all ROMCloud-owned persistent state."""
    click.echo(
        "This permanently removes ROMCloud runtime, integration, proxies, config, "
        "credentials, catalog, cache, download history, and local sync state. "
        "Original ROMs, emulator saves, external keys, and remote provider data "
        "are preserved; OAuth access is not revoked server-side."
    )
    if not yes and not click.confirm("Permanently purge all ROMCloud state?"):
        click.echo("Purge cancelled.")
        return
    try:
        _wait_for_handoff(wait_for_pid)
        romcloud_home, config, trusted = _lifecycle_paths(ctx)
        report = manage.purge(
            config=config,
            romcloud_home=romcloud_home,
            config_trusted=trusted,
        )
    except manage.LifecycleFailure as exc:
        _render_report(exc.report)
        raise click.ClickException(str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise click.ClickException(str(exc)) from exc
    _render_report(report)
    click.echo(f"ROMCloud purged. Removed proxies: {report.proxies_removed}")
