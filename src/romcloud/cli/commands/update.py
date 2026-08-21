"""romcloud update — self-update mechanism (no git required).

Downloads the latest source from GitHub (a plain zip archive — no `git`
dependency), upgrades the existing persistent venv in place via
``<venv python> -m pip install --upgrade <extracted project>``, and then
reconciles ROMCloud's own runtime artifacts (CLI/launch wrappers, the
graphical Ports UI, and — only if previously enabled — the Batocera mount
service script and EmulationStation override) against that same source
revision. It also reconciles exact historical default paths in
``romcloud.toml``; credentials, the catalog database, and logs are left alone — see
:mod:`romcloud.lifecycle.update` for the full design.
"""

from __future__ import annotations

import sys
from pathlib import Path

import click

from romcloud.core.exceptions import ROMCloudError
from romcloud.core.capabilities import Capability
from romcloud.core.update_channels import DEFAULT_UPDATE_CHANNEL, UpdateChannel, resolve_channel
from romcloud.infrastructure.capabilities import capability_policy
from romcloud.infrastructure.config import load_config, write_update_channel
from romcloud.lifecycle.update import (
    DEFAULT_REPO,
    check_for_update,
    perform_update,
)


def _romcloud_home() -> Path:
    """The ROMCloud install root (``.../romcloud``), derived from the venv
    this process is running in — ``scripts/install.sh`` always lays out
    ``ROMCLOUD_HOME/venv``, and the installed ``romcloud`` wrapper always
    execs that venv's own python, so ``sys.prefix`` *is* ``ROMCLOUD_HOME/venv``
    at runtime.
    """
    return Path(sys.prefix).parent


def _venv_python() -> Path:
    return Path(sys.executable)


@click.command("update")
@click.option(
    "--check",
    "check_only",
    is_flag=True,
    help="Check whether an update is available, without installing it.",
)
@click.option(
    "--channel",
    type=click.Choice([item.value for item in UpdateChannel], case_sensitive=False),
    default=None,
    help="Switch to stable or develop after a successful update.",
)
@click.pass_context
def update_cmd(ctx: click.Context, check_only: bool, channel: str | None) -> None:
    """Update ROMCloud to the latest version from GitHub — no git required."""
    config_value = (ctx.obj or {}).get("config_path")
    romcloud_home = _romcloud_home()
    config_path = (
        Path(config_value)
        if config_value
        else romcloud_home / "config" / "romcloud.toml"
    )
    persisted_channel = DEFAULT_UPDATE_CHANNEL.value
    if config_path.exists():
        try:
            config = load_config(str(config_path), resolve_paths=False)
            capability_policy(config).require(
                Capability.UPDATE_NETWORK, "ROMCloud update"
            )
            persisted_channel = config.update_channel
        except ROMCloudError as exc:
            raise click.ClickException(str(exc)) from exc
    if check_only and channel is not None:
        raise click.UsageError(
            "--channel switches during an update and cannot be used with --check"
        )
    source = resolve_channel(channel or persisted_channel)

    if check_only:
        try:
            result = check_for_update(
                romcloud_home, repo=DEFAULT_REPO, channel=source.channel
            )
        except ROMCloudError as exc:
            click.echo(f"error: {exc}", err=True)
            ctx.exit(1)
            return

        current = result.current
        if current is None:
            click.echo("Installed: unknown (no version.json recorded yet)")
        else:
            click.echo(f"Installed: {current.version} ({current.commit_short or 'commit unknown'})")
        click.echo(
            f"Available: {result.latest_commit.short_sha} ({source.channel.value})"
        )

        click.echo()
        if result.update_available:
            click.echo("An update is available. Run `romcloud update` to install it.")
        else:
            click.echo("ROMCloud is up to date.")
        return

    try:
        result = perform_update(
            romcloud_home,
            _venv_python(),
            repo=DEFAULT_REPO,
            channel=source.channel,
        )
    except ROMCloudError as exc:
        click.echo(f"error: {exc}", err=True)
        ctx.exit(1)
        return

    if channel is not None and source.channel.value != persisted_channel:
        try:
            write_update_channel(source.channel, str(config_path))
        except ROMCloudError as exc:
            raise click.ClickException(
                "ROMCloud updated successfully, but the channel selection could "
                f"not be persisted: {exc}"
            ) from exc

    click.echo(
        f"Updated ROMCloud to {result.new.version} ({result.new.commit_short}) "
        f"on {source.channel.value}."
    )
    if result.reconcile_log:
        click.echo(result.reconcile_log)
