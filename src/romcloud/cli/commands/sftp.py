"""romcloud sftp — standalone SFTP host-key trust helpers."""

from __future__ import annotations

import click

from romcloud.core.exceptions import ProviderNotReachableError
from romcloud.infrastructure.providers.sftp import probe_host_key


@click.group("sftp")
def sftp_group() -> None:
    """SFTP setup helpers (host-key trust)."""


@sftp_group.command("fingerprint")
@click.argument("host")
@click.option("--port", default=22, type=int, help="SSH port (default: 22).")
@click.option("--timeout", default=10.0, type=float, help="Connection timeout in seconds.")
def fingerprint_cmd(host: str, port: int, timeout: float) -> None:
    """Observe HOST's SSH host key without authenticating.

    Prints the key type and ``SHA256:...`` fingerprint so it can be reviewed
    and passed to `romcloud configure --sftp-host-key-fingerprint` (or
    trusted interactively during the wizard) — never accepted blindly.
    """
    try:
        key_type, fingerprint = probe_host_key(host, port, timeout=timeout)
    except ProviderNotReachableError as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(f"Host:        {host}:{port}")
    click.echo(f"Key type:    {key_type}")
    click.echo(f"Fingerprint: {fingerprint}")
    click.echo(
        "\nVerify this fingerprint through a trusted channel (e.g. the NAS's own "
        "admin console) before trusting it."
    )
