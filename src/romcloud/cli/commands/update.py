"""romcloud update — self-update mechanism (no git required).

Downloads the latest source from GitHub (a plain zip archive — no `git`
dependency) and upgrades the existing persistent venv in place via
``<venv python> -m pip install --upgrade <extracted project>``. Never
touches ``romcloud.toml``, credentials, the catalog database, the cache,
logs, generated proxies, or Batocera service/integration files — see
:mod:`romcloud.infrastructure.update` for the full design.
"""

from __future__ import annotations

import sys
from pathlib import Path

import click

from romcloud.core.exceptions import ROMCloudError
from romcloud.infrastructure.update import (
    DEFAULT_BRANCH,
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
@click.option("--repo", default=None, hidden=True, help="Override the source GitHub repo.")
@click.option("--branch", default=DEFAULT_BRANCH, hidden=True, help="Branch to update from.")
@click.pass_context
def update_cmd(ctx: click.Context, check_only: bool, repo: str | None, branch: str) -> None:
    """Update ROMCloud to the latest version from GitHub — no git required."""
    romcloud_home = _romcloud_home()
    repo = repo or DEFAULT_REPO

    if check_only:
        try:
            result = check_for_update(romcloud_home, repo=repo, branch=branch)
        except ROMCloudError as exc:
            click.echo(f"error: {exc}", err=True)
            ctx.exit(1)
            return

        current = result.current
        if current is None:
            click.echo("Installed: unknown (no version.json recorded yet)")
        else:
            click.echo(f"Installed: {current.version} ({current.commit_short or 'commit unknown'})")
        click.echo(f"Available: {result.latest_commit.short_sha} ({branch})")

        click.echo()
        if result.update_available:
            click.echo("An update is available. Run `romcloud update` to install it.")
        else:
            click.echo("ROMCloud is up to date.")
        return

    try:
        result = perform_update(romcloud_home, _venv_python(), repo=repo, branch=branch)
    except ROMCloudError as exc:
        click.echo(f"error: {exc}", err=True)
        ctx.exit(1)
        return

    click.echo(f"Updated ROMCloud to {result.new.version} ({result.new.commit_short}).")

