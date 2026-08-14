"""Launch the browser-based Library/Cache Manager."""

from __future__ import annotations

import secrets
import os
from pathlib import Path

import click

from romcloud.cli.context import get_container


@click.command("manager")
@click.option("--host", default="0.0.0.0", show_default=True, help="Interface to listen on.")
@click.option("--port", default=8765, type=click.IntRange(1, 65535), show_default=True)
@click.option(
    "--token",
    envvar="ROMCLOUD_MANAGER_TOKEN",
    help="Access token (a temporary token is generated when omitted).",
)
@click.option("--http", "plain_http", is_flag=True, help="Use plain HTTP (Gamepad API is normally unavailable to remote browsers).")
@click.option("--tls-cert", type=click.Path(exists=True, dir_okay=False, path_type=Path), help="Trusted PEM certificate to use instead of ROMCloud's certificate.")
@click.option("--tls-key", type=click.Path(exists=True, dir_okay=False, path_type=Path), help="PEM private key for --tls-cert.")
@click.option("--quiet", is_flag=True, hidden=True)
@click.pass_context
def manager_cmd(
    ctx: click.Context,
    host: str,
    port: int,
    token: str | None,
    plain_http: bool,
    tls_cert: Path | None,
    tls_key: Path | None,
    quiet: bool,
) -> None:
    """Serve the Library/Cache Manager for browsers on this network."""
    from romcloud.web.server import serve_manager
    from romcloud.web.lifecycle import (
        clear_manager_state,
        manager_instance_lock,
        manager_runtime_state,
        write_manager_state,
    )

    if bool(tls_cert) != bool(tls_key):
        raise click.UsageError("--tls-cert and --tls-key must be provided together.")
    if plain_http and tls_cert:
        raise click.UsageError("--http cannot be combined with --tls-cert/--tls-key.")
    container = get_container(ctx)
    try:
        with manager_instance_lock(container.config.data_path):
            if not plain_http and tls_cert is None:
                from romcloud.web.tls import ensure_manager_certificate

                tls_cert, tls_key = ensure_manager_certificate(container.config.data_path)
            access_token = token or secrets.token_urlsafe(24)
            display_host = "127.0.0.1" if host in {"0.0.0.0", "::"} else host
            scheme = "http" if plain_http else "https"
            if not quiet:
                click.echo("ROMCloud Library Manager")
                click.echo(f"Local URL: {scheme}://{display_host}:{port}/")
                if host in {"0.0.0.0", "::"}:
                    click.echo(
                        "For another device, replace 127.0.0.1 with this Batocera "
                        "device's LAN or Tailscale address, then create a pairing "
                        "link in ROMCloud."
                    )
                click.echo(f"Advanced manual access token: {access_token}")
                click.echo("Press Ctrl+C to stop.")
                if not plain_http and tls_cert and "manager-cert.pem" in tls_cert.name:
                    click.echo(
                        "The first browser visit may ask you to accept ROMCloud's "
                        "self-signed certificate."
                    )
            runtime_state = manager_runtime_state(
                host=host,
                port=port,
                token=access_token,
                scheme=scheme,
                pid=os.getpid(),
                instance_id=os.environ.get("ROMCLOUD_MANAGER_INSTANCE"),
            )
            write_manager_state(container.config.data_path, runtime_state)
            try:
                serve_manager(
                    container.library_manager,
                    host,
                    port,
                    access_token,
                    tls_cert=str(tls_cert) if tls_cert else None,
                    tls_key=str(tls_key) if tls_key else None,
                    auth_state_path=str(
                        Path(container.config.data_path) / "web" / "trusted-devices.json"
                    ),
                )
            except KeyboardInterrupt:
                if not quiet:
                    click.echo("\nLibrary Manager stopped.")
            finally:
                clear_manager_state(container.config.data_path, pid=os.getpid())
    except RuntimeError as exc:
        raise click.ClickException(str(exc)) from exc
