"""Read-only healthcheck rendered from the shared Troubleshoot collectors."""

from __future__ import annotations

import click

from romcloud.troubleshoot import collect_diagnostics


@click.command("healthcheck")
@click.pass_context
def healthcheck_cmd(ctx: click.Context) -> None:
    """Run comprehensive read-only diagnostics with concise output."""

    report, _ = collect_diagnostics(ctx.obj["config_path"])
    click.echo("\nROMCloud health check")
    click.echo("─" * 50)
    icons = {
        "healthy": "✓",
        "fixed": "✓",
        "warning": "!",
        "error": "✗",
        "skipped": "-",
    }
    for finding in report.findings:
        click.echo(f"  {icons[finding.status]}  {finding.message}")
        if finding.detail and finding.status != "healthy":
            click.echo(f"     {finding.detail}")
    click.echo("─" * 50)
    summary = report.summary
    click.echo(
        f"  {summary['healthy']} healthy, {summary['warning']} warning, "
        f"{summary['error']} error, {summary['skipped']} skipped"
    )
    click.echo()
    if summary["error"]:
        ctx.exit(1)
