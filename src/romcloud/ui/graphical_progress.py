"""Graphical (pygame, system-Python) cache-miss progress bridge.

Real Batocera hardware constraint: EmulationStation launches ``romcloud-run``
with no controlling terminal at all — no tty is ever attached — so the
existing curses progress screen (see :mod:`romcloud.ui.progress`) can never
actually render there: ``curses.wrapper()`` requires a real terminal device,
which simply doesn't exist on Batocera's framebuffer-only display stack when
launched from EmulationStation. That curses screen still works fine when
``romcloud launch``/``romcloud-run`` is run manually from an interactive
SSH/dev terminal (a real tty) — which is exactly why it appeared to "work in
the dev environment" while never actually appearing during a real hardware
launch.

This is precisely the same boundary the graphical Ports menu already solved:
pygame/SDL renders directly to the framebuffer/DRM display without needing a
tty, but pygame only exists in Batocera's *system* Python, never ROMCloud's
isolated venv. This module reuses that exact boundary for the cache-miss
transfer screen:

- This process (the venv, no pygame) spawns the installed
  ``romcloud-launch-progress`` wrapper (system Python + ``ports_gfx``) as a
  subprocess and drives it with newline-delimited JSON progress events over
  its stdin — the same "subprocess + a narrow text protocol" shape already
  used by the Ports menu (:mod:`ports_gfx.client`/:mod:`ports_gfx.operation`),
  just in the opposite direction. ``ports_gfx`` still never imports anything
  from ``romcloud``.
- The actual transfer still runs through the exact same
  ``CacheService.cache_game(..., on_progress=...)`` call the curses screen
  uses; only *how progress is displayed* differs. Cache/transfer semantics,
  staged ``.partial`` files, resumability, and LRU are entirely untouched —
  this module never reads or writes cache state itself.

Failure semantics ("ROMCloud may fail; Batocera must not"): any failure to
launch or drive the graphical subprocess is raised as
:class:`GraphicalProgressUnavailable`, so the caller can fall back to the
curses/plain-text progress path — a broken or never-installed graphical
component must never block a real game launch.
"""

from __future__ import annotations

import json
import subprocess
import threading
from pathlib import Path
from typing import Callable, Optional

from romcloud.core.cancellation import TransferCancellationToken
from romcloud.core.exceptions import ROMCloudError, TransferCancelledError
from romcloud.core.models.game import Game
from romcloud.services.cache import CacheService
from romcloud.infrastructure.mount_worker import romcloud_home_from_config

LAUNCH_PROGRESS_WRAPPER_NAME = "romcloud-launch-progress"

_SUBPROCESS_EXIT_GRACE_SECONDS = 5.0


class GraphicalProgressUnavailable(ROMCloudError):
    """The graphical progress subprocess could not be launched or driven —
    callers should fall back to the curses/plain-text progress path rather
    than treating this as a transfer failure."""


def graphical_progress_binary(config) -> Optional[Path]:
    """Path to the installed graphical launch-progress wrapper, or ``None``
    if it was never installed (e.g. no system Python with pygame was found
    at install time) — a normal, best-effort state, not a failure."""
    romcloud_home = romcloud_home_from_config(config)
    candidate = romcloud_home / "bin" / LAUNCH_PROGRESS_WRAPPER_NAME
    return candidate if candidate.is_file() else None


def run_graphical_progress_transfer(
    cache_service: CacheService,
    game: Game,
    *,
    launcher_bin: Path,
    popen: Callable[..., "subprocess.Popen[str]"] = subprocess.Popen,
) -> str:
    """Transfer *game* to cache while driving the graphical progress
    subprocess. Returns the launch path on success.

    Raises :class:`GraphicalProgressUnavailable` if the subprocess itself
    cannot even be launched — the caller should fall back to the
    curses/plain-text progress path, never treat it as a transfer failure.
    Raises :class:`~romcloud.core.exceptions.ROMCloudError` on a genuine
    transfer failure. Raises :class:`TransferCancelledError` if the user
    cancels via the graphical UI.
    """
    try:
        proc = popen(
            [str(launcher_bin)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
    except OSError as exc:
        raise GraphicalProgressUnavailable(str(exc)) from exc

    cancellation = TransferCancellationToken()

    def watch_cancel() -> None:
        try:
            for line in iter(proc.stdout.readline, ""):
                if line.strip() == "cancel":
                    cancellation.cancel()
                    return
        except Exception:  # noqa: BLE001 — a broken pipe just stops the watch
            return

    watcher = threading.Thread(target=watch_cancel, daemon=True)
    watcher.start()

    def send(event: dict) -> None:
        try:
            proc.stdin.write(json.dumps(event) + "\n")
            proc.stdin.flush()
        except (BrokenPipeError, ValueError, OSError):
            pass  # UI process gone — the transfer itself must still proceed

    def on_progress(done: int, total: int) -> None:
        cancellation.raise_if_cancelled()
        send({
            "phase": "downloading",
            "done": done,
            "total": total,
            "game_title": game.title,
            "system": game.system,
        })

    send({
        "phase": "connecting",
        "done": 0,
        "total": game.total_size_bytes or 0,
        "game_title": game.title,
        "system": game.system,
    })

    try:
        try:
            path = cache_service.cache_game(
                game.id,
                on_progress=on_progress,
                cancellation=cancellation,
            )
            cancellation.raise_if_cancelled()
        except TransferCancelledError:
            raise
        except ROMCloudError as exc:
            send({"event": "error", "message": str(exc)})
            raise
        send({"event": "launching"})
        return path
    finally:
        _close_subprocess(proc, watcher)


def _close_subprocess(proc: "subprocess.Popen[str]", watcher: threading.Thread) -> None:
    """Wait for the graphical process to exit cleanly (bounded) before
    returning, so pygame's fullscreen window is guaranteed gone before the
    caller execs emulatorlauncher. Never blocks indefinitely."""
    try:
        if proc.stdin:
            proc.stdin.close()
    except Exception:  # noqa: BLE001
        pass
    try:
        proc.wait(timeout=_SUBPROCESS_EXIT_GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        proc.terminate()
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            proc.kill()
    watcher.join(timeout=1)
