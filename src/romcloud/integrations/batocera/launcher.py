"""Batocera emulatorlauncher integration.

Verified command format on Batocera 42
---------------------------------------
``/usr/share/emulationstation/es_systems.cfg`` uses::

    emulatorlauncher %CONTROLLERSCONFIG% -system %SYSTEM% -rom %ROM% \\
        -gameinfoxml %GAMEINFOXML% -systemname %SYSTEMNAME%

``batocera-run`` does not exist on Batocera 42.

ROMCloud as a transparent pass-through wrapper
-----------------------------------------------
The ``romcloud-run`` script receives the exact same argv that EmulationStation
would have passed to ``emulatorlauncher``.  Its only job is:

* **Non-.romcloud ROM** → ``exec emulatorlauncher`` with argv unchanged.
* **.romcloud proxy** → resolve/cache the real ROM, replace *only* the value
  immediately following ``-rom``, then ``exec emulatorlauncher`` with every
  other argument preserved exactly — including its position.

``%CONTROLLERSCONFIG%``, ``-system``, ``-gameinfoxml``, ``-systemname``, and
any future arguments Batocera may add are never inspected or modified.

The argv helpers in this module (:func:`find_rom_path`, :func:`replace_rom_path`,
:func:`is_romcloud_proxy`) are pure functions with no I/O, making them easy
to unit-test without a Batocera install.

Batocera configgen settings — confirmed on Batocera 42
-------------------------------------------------------
A direct invocation of::

    emulatorlauncher -system snes -rom "/userdata/roms/snes/Some Game.sfc"

confirmed that:

* The emulator/core (``libretro`` / ``snes9x``) is selected automatically
  from ``global.*`` and ``snes.*`` config — no ``-emulator`` or ``-core``
  argument is required.
* ``snes["Some Game.sfc"].*`` per-game settings are keyed on the ROM
  **filename**, not the full path.  ROMCloud preserves the original filename
  verbatim when caching (e.g. ``/cache/snes/<uuid>/Some Game.sfc``), so
  per-game emulator, core, and shader overrides apply correctly.

Known compatibility limitation — folder-specific settings
----------------------------------------------------------
``snes.folder["/userdata/roms/snes"].*`` overrides are keyed on the ROM's
**containing directory**.  Cached ROMs live under
``/userdata/romcloud/cache/<system>/<uuid>/``, which differs from the
original ``/userdata/roms/<system>/`` directory used as the folder key.

**Folder-level settings will not apply to cached ROMs.**

Users who rely on ``folder[...].*`` overrides to set a non-default emulator
or core for a directory of games should migrate those settings to the
system level (``snes.*``) or per-game level (``snes["Game.sfc"].*``), which
work correctly with cached paths.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import sys
from pathlib import Path
from typing import Optional

from romcloud.core.exceptions import LaunchError
from romcloud.infrastructure.logging import get_logger

log = get_logger("batocera.launcher")

# Override via environment variable for testing on non-Batocera machines.
_EMULATOR_LAUNCHER: str = os.environ.get("ROMCLOUD_EMULATORLAUNCHER", "emulatorlauncher")


def _source_sha256(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return "unavailable"


# ── pure argv helpers ─────────────────────────────────────────────────────────


def find_rom_path(argv: list[str]) -> Optional[str]:
    """Return the value immediately following ``-rom`` in *argv*, or ``None``.

    Scans left-to-right; returns the first match only.  Does not validate
    whether the path exists.
    """
    for i, arg in enumerate(argv):
        if arg == "-rom" and i + 1 < len(argv):
            return argv[i + 1]
    return None


def replace_rom_path(argv: list[str], new_path: str) -> list[str]:
    """Return a new list with the value after ``-rom`` replaced by *new_path*.

    Only the first ``-rom`` occurrence is replaced.  Every other element —
    including its index — is preserved exactly.  Returns an unmodified copy
    if ``-rom`` is not present.
    """
    result = list(argv)
    for i, arg in enumerate(result):
        if arg == "-rom" and i + 1 < len(result):
            result[i + 1] = new_path
            return result
    return result


def is_romcloud_proxy(path: str) -> bool:
    """Return True if *path* ends with the ``.romcloud`` extension (case-insensitive)."""
    return path.lower().endswith(".romcloud")


# ── launcher ──────────────────────────────────────────────────────────────────


class EmulatorLauncher:
    """Delegates to Batocera's ``emulatorlauncher`` with argv pass-through.

    The only element that may differ between the received argv and the argv
    forwarded to ``emulatorlauncher`` is the value of ``-rom``.  All other
    arguments — count, order, content — are preserved exactly.
    """

    def exec_passthrough(self, original_argv: list[str]) -> None:
        """Exec ``emulatorlauncher`` with *original_argv* unchanged.

        ``argv[0]`` (the wrapper script path) is replaced with
        ``"emulatorlauncher"`` as the new program name; every other element
        is forwarded as-is.

        **Does not return on success.**

        Raises :class:`~romcloud.core.exceptions.LaunchError` if
        ``emulatorlauncher`` is not found on ``PATH``.
        """
        launcher = _require_launcher()
        target_argv = ["emulatorlauncher"] + list(original_argv[1:])
        log.info("passthrough: %s", launcher)
        os.execvp(launcher, target_argv)

    def exec_with_rom(self, original_argv: list[str], cached_rom_path: str) -> None:
        """Exec ``emulatorlauncher`` with the ``-rom`` value replaced by *cached_rom_path*.

        Every other argument — including ``%CONTROLLERSCONFIG%``, ``-system``,
        ``-gameinfoxml``, ``-systemname``, and their positions — is preserved
        exactly as in *original_argv*.

        **Does not return on success.**

        Raises :class:`~romcloud.core.exceptions.LaunchError` if
        ``emulatorlauncher`` is not found on ``PATH``.
        """
        launcher = _require_launcher()
        patched = replace_rom_path(list(original_argv), cached_rom_path)
        target_argv = ["emulatorlauncher"] + patched[1:]
        log.info(
            "launch diagnostic: final Batocera/configgen path=%r regular_file=%s",
            find_rom_path(target_argv),
            Path(cached_rom_path).is_file(),
        )
        log.info(
            "romcloud handoff: %s -rom %s [%d args total]",
            launcher,
            cached_rom_path,
            len(target_argv),
        )
        os.execvp(launcher, target_argv)


def _require_launcher() -> str:
    launcher = shutil.which(_EMULATOR_LAUNCHER)
    if launcher is None:
        raise LaunchError(
            f"emulatorlauncher not found: {_EMULATOR_LAUNCHER!r}. "
            "Is ROMCloud running on Batocera?"
        )
    return launcher


# ── wrapper entry point ───────────────────────────────────────────────────────


def run_launcher_wrapper(argv: list[str]) -> None:
    """Entry point called by the ``romcloud-run`` wrapper script.

    *argv* is ``sys.argv`` as received from EmulationStation — identical to
    what ``emulatorlauncher`` would have received, except ``argv[0]`` is the
    wrapper script path.

    Exits the process via ``os.execvp`` or ``sys.exit``; **never returns**.
    """
    rom_path = find_rom_path(argv)
    launcher = EmulatorLauncher()

    if rom_path is None or not is_romcloud_proxy(rom_path):
        # Non-.romcloud ROM — transparent passthrough, no ROMCloud work needed.
        try:
            launcher.exec_passthrough(argv)
        except LaunchError as exc:
            log.error("Passthrough failed: %s", exc)
            print(f"romcloud-run: {exc}", file=sys.stderr)
            sys.exit(1)
        sys.exit(1)  # unreachable if exec succeeded

    # .romcloud proxy — resolve, cache, then hand off to emulatorlauncher.
    try:
        cached_path = _resolve_and_cache(rom_path)
    except KeyboardInterrupt:
        log.info("Launch cancelled by user for %r", rom_path)
        print("romcloud-run: launch cancelled", file=sys.stderr)
        sys.exit(1)
    except Exception as exc:  # noqa: BLE001
        log.error("Failed to prepare %r: %s", rom_path, exc)
        print(f"romcloud-run: error preparing game: {exc}", file=sys.stderr)
        sys.exit(1)

    try:
        launcher.exec_with_rom(argv, cached_path)
    except LaunchError as exc:
        log.error("Launch handoff failed: %s", exc)
        print(f"romcloud-run: {exc}", file=sys.stderr)
        sys.exit(1)
    sys.exit(1)  # unreachable


def _resolve_and_cache(proxy_path: str) -> str:
    """Resolve a ``.romcloud`` proxy to a local cached ROM path.

    Imports are deferred so the passthrough path has minimal startup overhead.
    """
    from romcloud.bootstrap.container import Container
    from romcloud.infrastructure.config import load_config
    from romcloud.infrastructure.logging import configure_logging
    from romcloud.lifecycle.update import read_build_info
    from romcloud.services import cache as cache_module

    config = load_config()
    if config.game_access_mode == "direct_nas":
        from romcloud.core.exceptions import CacheError

        raise CacheError(
            "ROMCloud proxy caching is unavailable in Direct/NAS mode; launch the "
            "game through its Direct/NAS path."
        )
    configure_logging(
        level=config.logging.level,
        log_dir=config.logging.path,
        console=True,
    )
    container = Container(config)

    game = container.catalog.resolve_proxy(proxy_path)
    primary = game.primary_asset
    entry = container.cache.get_entry(game.id)
    resolved_before = container.cache.get_launch_path(game.id)
    build = read_build_info(Path(config.data_path).parent)
    log.info(
        "launch diagnostic: proxy=%r primary_asset=%r recorded_cache_path=%r "
        "get_launch_path=%r launcher_module=%s launcher_sha256=%s "
        "cache_module=%s cache_sha256=%s build_commit=%r build_source=%r",
        proxy_path,
        primary.relative_path if primary is not None else None,
        entry.cache_path if entry is not None else None,
        resolved_before,
        __file__,
        _source_sha256(Path(__file__)),
        cache_module.__file__,
        _source_sha256(Path(cache_module.__file__)),
        build.commit if build is not None else None,
        build.source if build is not None else None,
    )

    if container.cache.is_cached(game.id):
        container.cache.mark_launched(game.id)
        path = container.cache.get_launch_path(game.id)
        assert path is not None  # guaranteed by is_cached
        log.info("launch diagnostic: cached-hit selected path=%r", path)
        return path

    from romcloud.core.capabilities import Capability
    from romcloud.infrastructure.capabilities import capability_policy

    capability_policy(config).require(
        Capability.GAME_DOWNLOAD, "Launching an uncached game"
    )
    if not container.provider.is_reachable(game.source_root):
        from romcloud.core.exceptions import GameNotCachedError
        raise GameNotCachedError(
            f"Game is not cached and source is unreachable: {game.source_root}"
        )

    path = _transfer_with_progress(container, config, game)

    entry = container.cache.get_entry(game.id)
    resolved_after = container.cache.get_launch_path(game.id)
    log.info(
        "launch diagnostic: cache-miss transfer_return=%r recorded_cache_path=%r "
        "get_launch_path=%r",
        path,
        entry.cache_path if entry is not None else None,
        resolved_after,
    )

    container.cache.mark_launched(game.id)
    return path


def _transfer_with_progress(container, config, game) -> str:
    """Choose the best available progress presentation for a cache-miss
    transfer, in order: graphical (system-Python pygame subprocess, the
    only option that can actually render during a real EmulationStation
    launch — see ``romcloud.ui.graphical_progress``) > curses (only
    reachable when stdout is a real tty, e.g. an interactive dev/SSH
    session) > plain text. A missing/broken graphical install always
    degrades silently to the next option rather than failing the launch.
    """
    from romcloud.ui.graphical_progress import (
        GraphicalProgressUnavailable,
        graphical_progress_binary,
        run_graphical_progress_transfer,
    )

    launcher_bin = graphical_progress_binary(config)
    if launcher_bin is not None:
        try:
            return run_graphical_progress_transfer(container.cache, game, launcher_bin=launcher_bin)
        except GraphicalProgressUnavailable as exc:
            log.warning("Graphical launch progress unavailable, falling back: %s", exc)

    if sys.stdout.isatty():
        from romcloud.ui.progress import run_progress_transfer
        return run_progress_transfer(container.cache, game)
    return container.cache.cache_game(game.id)
