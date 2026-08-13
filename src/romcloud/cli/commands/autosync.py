"""Internal Batocera lifecycle commands for background SaveSync."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import click

from romcloud.cli.context import get_container
from romcloud.infrastructure.config import load_config
from romcloud.infrastructure.logging import get_logger
from romcloud.infrastructure import savesync_prompts
from romcloud.services.auto_savesync import AutoSaveSyncCoordinator

log = get_logger("auto-savesync-cli")


@click.group("_autosync", hidden=True)
def autosync_group() -> None:
    pass


def _event_arguments(command):  # noqa: ANN001, ANN201
    command = click.argument("rom")(command)
    command = click.argument("core")(command)
    command = click.argument("emulator")(command)
    return click.argument("system")(command)


def _coordinator(ctx: click.Context) -> AutoSaveSyncCoordinator:
    container = get_container(ctx)
    return AutoSaveSyncCoordinator(
        container.saves,
        data_root=Path(container.config.data_path),
        enabled=container.config.saves.auto_sync_enabled,
        enabled_check=lambda: load_config(
            ctx.obj["config_path"]
        ).saves.auto_sync_enabled,
    )


def _launch_pending_conflict_popup(data_root: Path) -> None:
    """Run the focused system-Python UI after releasing every sync lock."""
    pending = savesync_prompts.pending_ids(data_root)
    if not pending:
        log.info("SaveSync conflict popup launch skipped: durable queue is empty")
        return
    romcloud_bin = os.environ.get("ROMCLOUD_BIN")
    launcher = (
        Path(romcloud_bin).with_name("romcloud-ports")
        if romcloud_bin
        else data_root.parent / "bin" / "romcloud-ports"
    )
    if not launcher.is_file():
        log.warning(
            "SaveSync conflict popup launch skipped: launcher=%s is unavailable; "
            "pending_ids=%s",
            launcher,
            ",".join(pending),
        )
        return
    with savesync_prompts.popup_process_lock(data_root) as acquired:
        if not acquired:
            log.info(
                "SaveSync conflict popup launch rejected by singleton; pending_ids=%s",
                ",".join(pending),
            )
            return
        environment = os.environ.copy()
        environment["ROMCLOUD_BIN"] = romcloud_bin or str(
            launcher.with_name("romcloud")
        )
        display_environment = {
            key: environment.get(key, "")
            for key in (
                "DISPLAY",
                "WAYLAND_DISPLAY",
                "XDG_SESSION_TYPE",
                "SDL_VIDEODRIVER",
            )
        }
        process_log = data_root.parent / "logs" / "savesync-conflict-popup.log"
        process_log.parent.mkdir(parents=True, exist_ok=True)
        log.info(
            "Launching focused SaveSync conflict popup: launcher=%s mode=%s "
            "pending=%d cwd=%s display_environment=%s",
            launcher,
            "--savesync-conflicts",
            len(pending),
            data_root.parent,
            display_environment,
        )
        try:
            with process_log.open("a", encoding="utf-8") as output:
                result = subprocess.run(
                    [str(launcher), "--savesync-conflicts"],
                    stdin=subprocess.DEVNULL,
                    stdout=output,
                    stderr=output,
                    check=False,
                    close_fds=True,
                    start_new_session=True,
                    cwd=str(data_root.parent),
                    env=environment,
                )
        except OSError:
            log.warning(
                "SaveSync conflict popup subprocess launch failed: launcher=%s log=%s",
                launcher,
                process_log,
                exc_info=True,
            )
            return
        remaining = savesync_prompts.pending_ids(data_root)
        log.info(
            "SaveSync conflict popup subprocess exited: returncode=%d remaining=%d "
            "process_log=%s",
            result.returncode,
            len(remaining),
            process_log,
        )
        if result.returncode != 0:
            log.warning(
                "SaveSync conflict prompt exited unsuccessfully; prompt remains pending"
            )
        elif remaining:
            log.warning(
                "SaveSync conflict popup exited with %d prompt(s) still pending",
                len(remaining),
            )


@autosync_group.command("game-start", hidden=True)
@_event_arguments
@click.pass_context
def game_start(
    ctx: click.Context, system: str, emulator: str, core: str, rom: str
) -> None:
    if not ctx.obj["config"].saves.auto_sync_enabled:
        return
    try:
        _coordinator(ctx).game_start(
            system=system, emulator=emulator, core=core, rom=rom
        )
    except Exception:  # noqa: BLE001 - lifecycle hooks never block Batocera
        log.warning("Could not record Batocera game start", exc_info=True)


@autosync_group.command("game-stop", hidden=True)
@_event_arguments
@click.pass_context
def game_stop(
    ctx: click.Context, system: str, emulator: str, core: str, rom: str
) -> None:
    if not ctx.obj["config"].saves.auto_sync_enabled:
        return
    try:
        conflict_ids = _coordinator(ctx).game_stop(
            system=system, emulator=emulator, core=core, rom=rom
        )
        log.info(
            "gameStop worker reached popup handoff: new_conflicts=%d",
            len(conflict_ids),
        )
        _launch_pending_conflict_popup(Path(ctx.obj["config"].data_path))
    except Exception:  # noqa: BLE001 - lifecycle integration must fail open
        log.warning("SaveSync game-exit pass failed", exc_info=True)


@autosync_group.command("menu-tick", hidden=True)
@click.option("--force", is_flag=True, default=False)
@click.pass_context
def menu_tick(ctx: click.Context, force: bool) -> None:
    if not ctx.obj["config"].saves.auto_sync_enabled:
        return
    try:
        _coordinator(ctx).menu_tick(force=force)
    except Exception:  # noqa: BLE001 - lifecycle hooks never block Batocera
        log.warning("Periodic SaveSync quick pull tick failed", exc_info=True)


@autosync_group.command("remote-reconnect", hidden=True)
@click.pass_context
def remote_reconnect(ctx: click.Context) -> None:
    """Handle one detached unavailable-to-available remote-data edge."""
    if not ctx.obj["config"].saves.auto_sync_enabled:
        return
    try:
        _coordinator(ctx).remote_reconnect()
    except Exception:  # noqa: BLE001 - detached best-effort background work
        log.warning("Remote-data reconnect Quick Sync failed", exc_info=True)


@autosync_group.command("menu-loop", hidden=True)
@click.pass_context
def menu_loop(ctx: click.Context) -> None:
    if not ctx.obj["config"].saves.auto_sync_enabled:
        return
    try:
        _coordinator(ctx).menu_loop()
    except Exception:  # noqa: BLE001 - detached best-effort background work
        log.warning("Periodic SaveSync quick pull loop failed", exc_info=True)
