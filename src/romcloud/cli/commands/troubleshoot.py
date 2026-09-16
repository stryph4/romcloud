"""Explicit two-step Troubleshoot / Quick Repair CLI."""

from __future__ import annotations

import json

import click

from romcloud.troubleshoot import (
    collect_diagnostics,
    cooperative_cancellation,
    run_quick_repair,
)


@click.command("troubleshoot")
@click.option("--fix", is_flag=True, help="Run explicitly whitelisted Quick Repair fixes, then diagnose again.")
@click.option("--json-output", is_flag=True, help="Emit the structured report as JSON.")
@click.pass_context
def troubleshoot_cmd(ctx: click.Context, fix: bool, json_output: bool) -> None:
    """Diagnose ROMCloud, optionally followed by an explicit Quick Repair."""

    config_path = ctx.obj["config_path"]
    with cooperative_cancellation() as token:
        report = (
            run_quick_repair(config_path, cancelled=token)
            if fix
            else collect_diagnostics(config_path, cancelled=token)[0]
        )
    payload = report.as_dict()
    if json_output:
        click.echo(json.dumps(payload, separators=(",", ":")))
    else:
        title = "ROMCloud Quick Repair" if fix else "Troubleshoot ROMCloud"
        click.echo(f"\n{title}")
        click.echo("─" * 50)
        for finding in report.findings:
            click.echo(f"  [{finding.status}] {finding.message}")
            if finding.detail and finding.status != "healthy":
                click.echo(f"    {finding.detail}")
        click.echo("─" * 50)
        click.echo(f"  Result: {report.overall_status}")
        if not fix and report.quick_repair_available:
            click.echo("  Safe fixes are available. Run `romcloud troubleshoot --fix` to apply them.")
        if report.es_restart_required:
            click.echo("  Restart EmulationStation to apply completed integration fixes.")
        click.echo()
    if report.summary["error"]:
        ctx.exit(1)
