"""Internal Batocera lifecycle commands for background SaveSync."""

from __future__ import annotations

from pathlib import Path

import click

from romcloud.cli.context import get_container
from romcloud.infrastructure.logging import get_logger
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
    )


@autosync_group.command("game-start", hidden=True)
@_event_arguments
@click.pass_context
def game_start(
    ctx: click.Context, system: str, emulator: str, core: str, rom: str
) -> None:
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
    try:
        _coordinator(ctx).game_stop(
            system=system, emulator=emulator, core=core, rom=rom
        )
    except Exception:  # noqa: BLE001 - detached best-effort background work
        log.warning("Background SaveSync game-exit pass failed", exc_info=True)
