"""romcloud update — self-update mechanism (stub for v0.1)."""

from __future__ import annotations

import click


@click.command("update")
@click.pass_context
def update_cmd(ctx: click.Context) -> None:
    """Update ROMCloud to the latest version.

    In a full installation this will pull from the configured update channel
    or re-run the installer with a newer package.
    """
    click.echo("Self-update is not yet implemented in v0.1.")
    click.echo(
        "To update manually:\n"
        "  1. Download the new release.\n"
        "  2. Re-run:  bash scripts/install.sh\n"
        "  (Your config, cache, and catalog are preserved.)"
    )
