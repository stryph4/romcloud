"""Internal Batocera lifecycle commands for durable Auto SaveSync."""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

import click

from romcloud.cli.context import get_container
from romcloud.core.capabilities import OperatingMode
from romcloud.core.exceptions import SaveSyncWorkerBusyError
from romcloud.infrastructure import savesync_prompts
from romcloud.infrastructure.config import load_config
from romcloud.infrastructure.library_view import operating_mode
from romcloud.infrastructure.logging import get_logger
from romcloud.integrations.batocera import auto_savesync as batocera_auto_savesync
from romcloud.services.auto_savesync import ActiveSessionStore, AutoSaveSyncCoordinator
from romcloud.ui.savesync_progress import NullSaveSyncProgress, start_savesync_progress

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
    enabled = _auto_sync_enabled(container.config)

    def enabled_check() -> bool:
        current = load_config(ctx.obj["config_path"])
        return _auto_sync_enabled(current)

    return AutoSaveSyncCoordinator(
        container.saves,
        data_root=Path(container.config.data_path),
        enabled=enabled,
        enabled_check=enabled_check,
        selected_systems=container.config.source.selected_systems,
    )


def _auto_sync_enabled(config) -> bool:  # noqa: ANN001
    """Automatic SaveSync runs whenever gameplay is online and locally owned."""
    return (
        bool(config.saves.auto_sync_enabled)
        and operating_mode(config) is not OperatingMode.OFFLINE
    )


def _session_store(config) -> ActiveSessionStore:  # noqa: ANN001
    """Gameplay markers remain active even when network Auto SaveSync is off."""
    return ActiveSessionStore(Path(config.data_path))


def _resolve_ports_launcher(data_root: Path) -> Path:
    """Resolve the installed graphical Ports UI wrapper (``romcloud-ports``).

    Shared by both the SaveSync conflict popup and the gameStop Auto
    SaveSync progress popup — the same wrapper subprocess dispatches to
    either mode via a CLI flag (see ``ports_gfx/__main__.py``).
    """
    romcloud_bin = os.environ.get("ROMCLOUD_BIN")
    return (
        Path(romcloud_bin).with_name("romcloud-ports")
        if romcloud_bin
        else data_root.parent / "bin" / "romcloud-ports"
    )


def _launch_pending_conflict_popup(
    data_root: Path, *, lifecycle_caller_pid: int | None = None
) -> None:
    """Run the focused system-Python UI after releasing every sync lock."""
    pending = savesync_prompts.pending_ids(data_root)
    if not pending:
        log.info("SaveSync conflict popup launch skipped: durable queue is empty")
        return
    romcloud_bin = os.environ.get("ROMCLOUD_BIN")
    launcher = _resolve_ports_launcher(data_root)
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
        log.info(
            "EmulationStation readiness wait started: caller_pid=%s timeout=%.1fs",
            lifecycle_caller_pid or "unknown",
            batocera_auto_savesync.ES_READINESS_TIMEOUT_SECONDS,
        )
        readiness = batocera_auto_savesync.wait_for_emulationstation_display(
            lifecycle_caller_pid
        )
        if not readiness.ready:
            log.warning(
                "EmulationStation readiness wait timed out: elapsed=%.3fs "
                "attempts=%d signal=%s detail=%s; conflict prompt remains queued "
                "for manual resolution",
                readiness.elapsed_seconds,
                readiness.attempts,
                readiness.signal,
                readiness.detail,
            )
            return
        log.info(
            "EmulationStation readiness condition satisfied: elapsed=%.3fs "
            "attempts=%d signal=%s detail=%s",
            readiness.elapsed_seconds,
            readiness.attempts,
            readiness.signal,
            readiness.detail,
        )
        pending = savesync_prompts.pending_ids(data_root)
        if not pending:
            log.info(
                "SaveSync conflict popup launch skipped after readiness: "
                "queued conflicts were already resolved or dismissed"
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
    if not _auto_sync_enabled(ctx.obj["config"]):
        try:
            _session_store(ctx.obj["config"]).start(
                system=system, emulator=emulator, core=core, rom=rom
            )
        except Exception:  # noqa: BLE001 - lifecycle hooks never block Batocera
            log.warning("Could not record Batocera game start", exc_info=True)
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
    """Finish required Quick Sync work before the lifecycle command succeeds."""
    if not _auto_sync_enabled(ctx.obj["config"]):
        config = ctx.obj["config"]
        mode = operating_mode(config)
        log.info(
            "gameStop SaveSync skipped before eligibility: system=%s "
            "emulator=%s core=%s rom=%s auto_sync_enabled=%s mode=%s "
            "reason=%s operation_id=%s",
            system,
            emulator,
            core,
            rom,
            bool(config.saves.auto_sync_enabled),
            mode.value,
            (
                "offline-mode"
                if mode is OperatingMode.OFFLINE
                else "auto-sync-disabled"
            ),
            os.environ.get("ROMCLOUD_DIAGNOSTIC_OPERATION_ID", "none"),
        )
        try:
            _session_store(config).stop(system=system, rom=rom)
        except Exception:  # noqa: BLE001 - lifecycle hooks never block Batocera
            log.warning("Could not clear Batocera game session", exc_info=True)
        return
    coordinator = _coordinator(ctx)
    if not coordinator.game_stop_eligible(
        system=system, emulator=emulator, core=core
    ):
        # Let the coordinator retire the lifecycle marker and emit its scoped
        # skip diagnostic, but do so before creating any progress UI.
        coordinator.game_stop(
            system=system, emulator=emulator, core=core, rom=rom
        )
        return
    worker_pid = os.getpid()
    caller_pid = batocera_auto_savesync.lifecycle_caller_pid()
    log.info(
        "gameStop synchronous worker started: pid=%d lifecycle_caller_pid=%s "
        "system=%s emulator=%s core=%s",
        worker_pid,
        caller_pid or "unknown",
        system,
        emulator,
        core,
    )
    data_root = Path(ctx.obj["config"].data_path)
    launcher = _resolve_ports_launcher(data_root)
    # Best-effort and fail-open: any failure to launch/drive this popup must
    # never affect the synchronous SaveSync work below (see
    # NullSaveSyncProgress / SaveSyncProgressReporter).
    progress = start_savesync_progress(launcher)
    log.info(
        "gameStop progress reporter initialized: launcher=%s launcher_exists=%s "
        "reporter=%s operation_id=%s",
        launcher,
        launcher.is_file(),
        type(progress).__name__,
        os.environ.get("ROMCLOUD_DIAGNOSTIC_OPERATION_ID", "none"),
    )
    if isinstance(progress, NullSaveSyncProgress):
        log.warning(
            "gameStop progress popup unavailable: launcher=%s launcher_exists=%s",
            launcher,
            launcher.is_file(),
        )
    try:
        quick_sync_started = time.monotonic()
        log.info("gameStop Quick Sync started: worker_pid=%d", worker_pid)
        try:
            conflict_ids = coordinator.game_stop(
                system=system, emulator=emulator, core=core, rom=rom,
                progress=progress,
            )
        except Exception:
            log.warning(
                "gameStop Quick Sync ended: worker_pid=%d status=failed "
                "elapsed=%.3fs",
                worker_pid,
                time.monotonic() - quick_sync_started,
            )
            raise
        log.info(
            "gameStop Quick Sync ended: worker_pid=%d status=complete "
            "elapsed=%.3fs new_conflicts=%d ids=%s",
            worker_pid,
            time.monotonic() - quick_sync_started,
            len(conflict_ids),
            ",".join(conflict_ids) or "none",
        )
        log.info(
            "gameStop durable Quick Sync result: status=complete "
            "new_conflicts=%d ids=%s",
            len(conflict_ids),
            ",".join(conflict_ids) or "none",
        )
    except Exception as exc:
        if isinstance(exc, SaveSyncWorkerBusyError):
            try:
                pid = batocera_auto_savesync.spawn_drain_pending(
                    operation_id=getattr(exc, "diagnostic_operation_id", None)
                )
                log.warning(
                    "gameStop worker-busy follow-up scheduled: drain_pending_pid=%d",
                    pid,
                )
            except Exception:  # noqa: BLE001 - the failure below still surfaces
                log.error(
                    "gameStop could not schedule a worker-busy follow-up sync; "
                    "durable dirty state remains for the next periodic tick",
                    exc_info=True,
                )
        log.warning("SaveSync game-exit pass failed", exc_info=True)
        raise click.ClickException(
            "Auto SaveSync did not complete; pending local work was retained."
        ) from exc
    finally:
        # Idempotent safety net: the coordinator already closes the popup on
        # every normal exit path; this only guards an unexpected failure
        # before/around that call so the popup subprocess is never orphaned.
        try:
            progress.close(False, "Save sync failed.\nYour local save has been preserved.")
        except Exception:  # noqa: BLE001 - never affects the lifecycle hook's result
            log.warning("Could not close SaveSync progress popup", exc_info=True)
        log.info("gameStop synchronous worker exited: pid=%d", worker_pid)


@autosync_group.command("conflict-popup", hidden=True)
@click.pass_context
def conflict_popup(ctx: click.Context) -> None:
    """Present queued conflicts only after the gameStop hook has returned."""
    try:
        _launch_pending_conflict_popup(
            Path(ctx.obj["config"].data_path),
            lifecycle_caller_pid=batocera_auto_savesync.lifecycle_caller_pid(),
        )
    except Exception:  # noqa: BLE001 - queued conflicts remain durable
        log.warning("SaveSync conflict popup handoff failed", exc_info=True)


@autosync_group.command("menu-tick", hidden=True)
@click.option("--force", is_flag=True, default=False)
@click.pass_context
def menu_tick(ctx: click.Context, force: bool) -> None:
    if not _auto_sync_enabled(ctx.obj["config"]):
        return
    try:
        _coordinator(ctx).menu_tick(force=force)
    except Exception:  # noqa: BLE001 - lifecycle hooks never block Batocera
        log.warning("Periodic SaveSync quick pull tick failed", exc_info=True)


@autosync_group.command("remote-reconnect", hidden=True)
@click.pass_context
def remote_reconnect(ctx: click.Context) -> None:
    """Handle one detached unavailable-to-available remote-data edge."""
    if not _auto_sync_enabled(ctx.obj["config"]):
        return
    try:
        _coordinator(ctx).remote_reconnect()
    except Exception:  # noqa: BLE001 - detached best-effort background work
        log.warning("Remote-data reconnect Quick Sync failed", exc_info=True)


@autosync_group.command("drain-pending", hidden=True)
@click.pass_context
def drain_pending(ctx: click.Context) -> None:
    """Detached, patient follow-up after a worker-busy gameStop deferral.

    Never blocks a lifecycle hook: this always runs as its own spawned
    process, so it can wait considerably longer than an interactive trigger
    for the worker lock a busy Manual/Auto Quick Sync is holding to free up.
    """
    if not _auto_sync_enabled(ctx.obj["config"]):
        return
    try:
        _coordinator(ctx).drain_pending()
    except Exception:  # noqa: BLE001 - detached best-effort background work
        log.warning("Worker-busy follow-up Quick Sync failed", exc_info=True)


@autosync_group.command("menu-loop", hidden=True)
@click.pass_context
def menu_loop(ctx: click.Context) -> None:
    if not _auto_sync_enabled(ctx.obj["config"]):
        return
    try:
        _coordinator(ctx).menu_loop()
    except Exception:  # noqa: BLE001 - detached best-effort background work
        log.warning("Periodic SaveSync quick pull loop failed", exc_info=True)
