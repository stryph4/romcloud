"""romcloud healthcheck — verify environment readiness."""

from __future__ import annotations

import shutil
from pathlib import Path

import click

from romcloud.cli.context import get_container
from romcloud.infrastructure.source_display import source_display_summary
from romcloud.infrastructure.config import DIRECT_NAS_MODE


def _fmt_bytes(n: int) -> str:
    return f"{n / 1024**3:.1f} GB"


@click.command("healthcheck")
@click.pass_context
def healthcheck_cmd(ctx: click.Context) -> None:
    """Verify source reachability, cache space, and config integrity."""
    container = get_container(ctx)
    config = container.config
    source = source_display_summary(config)

    ok = True

    def check(label: str, passed: bool, detail: str = "") -> None:
        nonlocal ok
        icon = "✓" if passed else "✗"
        line = f"  {icon}  {label}"
        if detail:
            line += f" — {detail}"
        click.echo(line)
        if not passed:
            ok = False

    click.echo("\nROMCloud health check")
    click.echo("─" * 50)

    # Source reachability.
    reachable = container.provider.is_reachable(config.source.rom_root)
    check(
        f"Source reachable ({source['source_type']})",
        reachable,
        source["source_description"] if not reachable else "",
    )

    # Local ROM directory.
    local_roms = Path(config.local_roms_path)
    check(
        "Local ROM directory exists",
        local_roms.is_dir(),
        str(local_roms),
    )

    if config.game_access_mode != DIRECT_NAS_MODE:
        # Cache directory and reserve apply only to Smart Cache.
        cache_path = Path(config.cache.path)
        check("Cache path writable", _can_write(cache_path), str(cache_path))
        if cache_path.exists() or cache_path.parent.exists():
            check_path = cache_path if cache_path.exists() else cache_path.parent
            stat = shutil.disk_usage(str(check_path))
            free_gb = stat.free / 1024**3
            min_free_gb = config.cache.min_free_gb
            check(
                f"Free disk space ≥ {min_free_gb:.0f} GB",
                free_gb >= min_free_gb,
                f"{free_gb:.1f} GB available",
            )

    # Data directory.
    data_path = Path(config.data_path)
    check("Data directory writable", _can_write(data_path), str(data_path))

    # SaveSync's optional destination has a stricter reachability contract:
    # local roots must pass a real write probe, while SMB roots must be an
    # actual read-write mount and also pass that probe.
    if config.remote_data is not None:
        remote_ok = container.saves.is_remote_reachable()
        check(
            "ROMCloud data location writable",
            remote_ok,
            "" if remote_ok else str(config.remote_data.root),
        )

    # Independently configured SMB-backed source/remote-data mounts.
    from romcloud.infrastructure import mount_worker
    if mount_worker.configured_mounts(config):
        try:
            romcloud_home = mount_worker.romcloud_home_from_config(config)
            diag = mount_worker.get_diagnostics(romcloud_home, config)
            check("SMB locations mounted", diag.mounted, "" if diag.mounted else diag.label)
        except Exception as exc:  # noqa: BLE001 — healthcheck must never crash
            check("SMB locations mounted", False, f"error checking status: {exc}")

    click.echo("─" * 50)
    if ok:
        click.echo("  All checks passed.")
    else:
        click.echo("  One or more checks failed.")
        ctx.exit(1)
    click.echo()


def _can_write(path: Path) -> bool:
    try:
        path.mkdir(parents=True, exist_ok=True)
        test = path / ".romcloud_write_test"
        test.write_text("test")
        test.unlink()
        return True
    except OSError:
        return False
