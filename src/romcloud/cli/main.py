"""ROMCloud CLI — entry point and shared context."""

from __future__ import annotations

import sys
import click

from romcloud.infrastructure.config import load_config, default_config_path
from romcloud.infrastructure.logging import configure_logging
from romcloud.core.exceptions import ConfigurationNotFoundError, ROMCloudError

# Re-exported for backward compatibility — command modules should import
# from romcloud.cli.context directly (see that module for why).
from romcloud.cli.context import get_container

__all__ = ["cli", "get_container"]


# ── root command group ────────────────────────────────────────────────────────


@click.group(context_settings={"help_option_names": ["-h", "--help"]})
@click.option(
    "--config",
    "config_path",
    default=None,
    envvar="ROMCLOUD_CONFIG",
    metavar="PATH",
    help="Path to romcloud.toml (default: /userdata/system/romcloud/config/romcloud.toml)",
)
@click.option("--debug", is_flag=True, hidden=True)
@click.version_option(package_name="romcloud", prog_name="romcloud")
@click.pass_context
def cli(ctx: click.Context, config_path: str | None, debug: bool) -> None:
    """ROMCloud — browse and launch ROMs from remote/external sources on Batocera."""
    ctx.ensure_object(dict)
    ctx.obj["config_path"] = config_path or str(default_config_path())
    ctx.obj["debug"] = debug

    # Load config eagerly (except for commands which may run before one exists).
    # Individual uidata actions load it when required; setup-status must be
    # available to the graphical first-run UI on a completely fresh install.
    if ctx.invoked_subcommand not in ("configure", "update", "_reconcile-install", "uidata"):
        try:
            config = load_config(config_path)
            ctx.obj["config"] = config
            configure_logging(
                level="DEBUG" if debug else config.logging.level,
                log_dir=config.logging.path,
                console=True,
            )
        except ConfigurationNotFoundError as exc:
            click.echo(f"error: {exc}", err=True)
            ctx.exit(1)
        except ROMCloudError as exc:
            click.echo(f"error: {exc}", err=True)
            ctx.exit(1)


# ── sub-command registration ──────────────────────────────────────────────────

from romcloud.cli.commands.configure import configure_cmd
from romcloud.cli.commands.refresh import refresh_cmd
from romcloud.cli.commands.status import status_cmd
from romcloud.cli.commands.healthcheck import healthcheck_cmd
from romcloud.cli.commands.launch import launch_cmd
from romcloud.cli.commands.cache import cache_group
from romcloud.cli.commands.saves import saves_group
from romcloud.cli.commands.update import update_cmd
from romcloud.cli.commands.es import es_group
from romcloud.cli.commands.mount import mount_group
from romcloud.cli.commands.uidata import uidata_group
from romcloud.cli.commands.reconcile import reconcile_install_cmd

cli.add_command(configure_cmd, name="configure")
cli.add_command(refresh_cmd, name="refresh")
cli.add_command(status_cmd, name="status")
cli.add_command(healthcheck_cmd, name="healthcheck")
cli.add_command(launch_cmd, name="launch")
cli.add_command(cache_group, name="cache")
cli.add_command(saves_group, name="saves")
cli.add_command(update_cmd, name="update")
cli.add_command(es_group, name="es")
cli.add_command(mount_group, name="mount")
cli.add_command(uidata_group, name="uidata")
cli.add_command(reconcile_install_cmd, name="_reconcile-install")


if __name__ == "__main__":
    cli()
