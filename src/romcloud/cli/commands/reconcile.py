"""romcloud _reconcile-install — internal: reconcile installer-managed
runtime artifacts.

Hidden — shared by ``scripts/install.sh`` (fresh install / manual repair)
and ``romcloud update`` (self-update) so neither one duplicates this logic.
All the actual reconciliation logic lives in
:mod:`romcloud.infrastructure.installer`; this module is only the thin CLI
wrapper around it, invoked as a subprocess of the (already installed or
just-upgraded) venv's own python — so it always runs the exact same source
revision that just got installed/upgraded.
"""

from __future__ import annotations

from pathlib import Path

import click

from romcloud.infrastructure import installer


@click.command("_reconcile-install", hidden=True)
@click.option("--romcloud-home", required=True, type=click.Path(path_type=Path))
@click.option("--project-root", required=True, type=click.Path(path_type=Path, exists=True))
@click.option("--ports-dir", required=True, type=click.Path(path_type=Path))
@click.option(
    "--system-python",
    default="",
    help="Explicit system Python override for the graphical Ports UI; empty means auto-detect.",
)
@click.pass_context
def reconcile_install_cmd(
    ctx: click.Context,
    romcloud_home: Path,
    project_root: Path,
    ports_dir: Path,
    system_python: str,
) -> None:
    """Internal: reconcile ROMCloud's installed runtime artifacts."""
    try:
        report = installer.reconcile_install(
            romcloud_home=romcloud_home,
            project_root=project_root,
            ports_dir=ports_dir,
            system_python=system_python or None,
        )
    except Exception as exc:  # noqa: BLE001 — must surface as a clear install/update failure
        click.echo(f"ERROR: failed to write ROMCloud runtime artifacts: {exc}", err=True)
        ctx.exit(1)
        return

    click.echo(f"  Wrote CLI wrapper: {report.core.cli_wrapper}")
    click.echo(f"  Wrote launch wrapper: {report.core.launch_wrapper}")

    ports = report.ports_ui
    if ports.installed:
        click.echo(
            f"  Wrote graphical Ports wrapper: {ports.wrapper_path} "
            f"(system python: {ports.system_python})"
        )
        if ports.port_entry_path is not None:
            click.echo(f"  Installed Batocera Port entry: {ports.port_entry_path}")
        elif ports.port_entry_skip_reason == "ports_dir_missing":
            click.echo(f"  Note: {ports_dir} not found — skipped Batocera Port entry.")
    elif ports.error is not None:
        click.echo(f"  Note: graphical Ports UI reconciliation failed: {ports.error}")
    else:
        click.echo(
            "  Skipping graphical Ports UI: no system Python with pygame found (CLI/TUI unaffected)."
        )

    if report.mount_service is True:
        click.echo("  Reconciled Batocera mount service script.")
    elif report.mount_service is False:
        click.echo("  Note: failed to reconcile Batocera mount service script.")

    if report.es_override is True:
        click.echo("  Reconciled EmulationStation override.")
    elif report.es_override is False:
        click.echo("  Note: failed to reconcile EmulationStation override.")

    if report.ports_gamelist is True:
        click.echo("  Reconciled Ports gamelist entry for ROMCloud.")
    elif report.ports_gamelist is False:
        click.echo("  Note: failed to reconcile Ports gamelist entry.")
