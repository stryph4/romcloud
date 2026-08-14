"""Pygame-based Batocera Ports UI — the actual rendering/event loop.

Runs under Batocera's SYSTEM Python (pygame/SDL already present there —
confirmed pygame 2.5.2 / SDL 2.32.8 on real hardware). ``pygame`` is
imported lazily, only inside :func:`run_app`, so merely importing this
module never fails on a machine without pygame (e.g. this repo's own dev
venv) — the failure is deferred to the point where the UI is actually
launched, where it can be reported clearly instead of via a module-import
traceback at ``python -m ports_gfx`` startup.

Rendering is fully resolution-responsive: the window opens full-screen at
whatever the current display reports (720p/1080p/4K TVs, Steam Deck-class
handhelds, unusual aspect ratios), and every position/size, font, and
focus-navigation decision is derived from that actual size via
``layout.compute_layout``/``layout.find_next_focus_index`` — nothing here
is a fixed pixel coordinate. See ``layout.py`` for the geometry itself.

Input is fully device-agnostic: controller, physical keyboard, and touch
are all translated into the same small vocabulary of semantic actions by
``input_manager.InputManager`` (see ``actions.py``) before this module ever
sees them — nothing here reasons about a raw pygame key/button constant.

Everything above the lazy pygame import (menu contents, result formatting)
is plain Python and unit-tested directly; the render/event loop itself is
not (mirrors how ``romcloud.ui.progress``/``romcloud.ui.maintenance`` leave
their curses render loops untested — they need a real display/terminal).
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path
from typing import Optional

from ports_gfx.actions import ACTION_DIRECTIONS, Action
from ports_gfx.activity import ActivityLog
from ports_gfx.catalog_progress import CatalogRefreshProgress
from ports_gfx.client import BackendResult, call_backend, operation_result
from ports_gfx.display_diagnostics import DisplayDiagnostics
from ports_gfx.input_debug import InputDebugLogger
from ports_gfx.input_manager import InputEvent, InputManager
from ports_gfx.layout import (
    Layout,
    Rect,
    compute_layout,
    compute_vertical_control_rects,
    compute_wizard_regions,
    find_next_focus_index,
)
from ports_gfx.library_sync_screen import (
    CONFIRMING as LIBRARY_CONFIRMING,
    IMPORTING as LIBRARY_IMPORTING,
    PREFLIGHT as LIBRARY_PREFLIGHT,
    PREFLIGHTING as LIBRARY_PREFLIGHTING,
    RESULT as LIBRARY_RESULT,
    LibrarySyncScreenState,
)
from ports_gfx.menu import (
    BACK_ACTION,
    CATEGORY_ACTION_PREFIX,
    CONTROLLER_TEST_ACTION,
    EXIT_ACTION,
    MenuItem,
    MenuState,
    NavigationState,
)
from ports_gfx.operation import OperationRunner, OperationState
from ports_gfx.operation_screen import (
    OPERATION_SCREEN,
    OperationScreenState,
    OperationSpec,
    display_lines,
    handle_operation_event,
    visible_window,
    wrap_lines,
)
from ports_gfx.osk import compute_osk_layout, compute_osk_text_rect
from ports_gfx.relaunch import (
    RELAUNCH_FAILURE_LOG,
    GuiRelaunchCoordinator,
    relaunch_failure_message,
)
from ports_gfx.savesync_screen import (
    APPLYING_SETTINGS,
    COMMITTING,
    CONFIRMING,
    CONFLICTS,
    DASHBOARD,
    PREVIEW,
    PREVIEWING,
    REMOTE_AVAILABLE,
    REMOTE_CHECKING,
    RESULT,
    SETTINGS,
    SaveSyncScreenState,
)
from ports_gfx.savesync_conflict_popup import (
    action_rects as conflict_action_rects,
    render_conflict_resolver,
)
from ports_gfx.splash import SplashRenderer
from ports_gfx.update_state import UpdateCheckState
from ports_gfx.wizard import WizardState, WizardStep

SAVESYNC_ACTION = "savesync"
"""Sentinel the UI layer interprets as "switch to the SaveSync screen"
rather than dispatching to the backend — SaveSync is a first-class
top-level menu entry, not a Settings submenu."""
SETUP_ACTION = "setup"
LIBRARY_QUICK_SYNC_ACTION = "library-sync-quick"
LIBRARY_FULL_SYNC_ACTION = "library-sync-full"

MENU_CATEGORIES: dict[str, tuple[MenuItem, ...]] = {
    "Library": (
        MenuItem("Catalog Status", "status"),
        MenuItem("Refresh Catalog", "refresh"),
        MenuItem("Cache Status", "cache-status"),
    ),
    "Storage": (
        MenuItem("Run Setup", SETUP_ACTION),
        MenuItem("Connection Status", "connection-status"),
        MenuItem("Mount / Reconnect", "connection-mount"),
        MenuItem("Unmount", "connection-unmount"),
    ),
    "Maintenance": (
        MenuItem("Check for Updates", "update-check"),
        MenuItem("Update ROMCloud", "update-install"),
        MenuItem("Health Check", "healthcheck"),
        MenuItem("Setup Controller Mapping", CONTROLLER_TEST_ACTION),
    )
}


ACTIVE_MODE_ACTION = "operating-mode-active"


def _fallback_operating_state(mode: str) -> dict[str, object]:
    """Compatibility fallback for older backends; current backends serialize policy."""
    selected = "connected" if mode == "direct_nas" else "cache"
    return {
        "game_access_mode": mode,
        "operating_mode": selected,
        "presentation_intent": selected,
        "connected_mode": selected == "connected",
        "cache_mode": selected == "cache",
        "offline_mode": False,
        "offline_mode_supported": True,
        "capabilities": {
            "catalog_refresh": True,
            "library_sync": True,
            "save_sync": True,
            "update_network": True,
            "remote_validation": True,
        },
    }


def operating_state_from_status(data: dict) -> dict[str, object]:
    state = data.get("operating_state")
    if isinstance(state, dict):
        return state
    return _fallback_operating_state(str(data.get("game_access_mode", "smart_cache")))


def root_menu_items_for_state(state: dict[str, object]) -> tuple[MenuItem, ...]:
    capabilities = state.get("capabilities", {})
    capabilities = capabilities if isinstance(capabilities, dict) else {}
    items = [MenuItem("Library", f"{CATEGORY_ACTION_PREFIX}Library")]
    active_mode = str(state.get("operating_mode", "cache"))
    items.append(
        MenuItem(
            "Direct",
            ACTIVE_MODE_ACTION if active_mode == "connected" else "library-connected",
            "Active"
            if active_mode == "connected"
            else "Play games directly from the configured ROM source.",
            active=active_mode == "connected",
        )
    )
    items.append(
        MenuItem(
            "Cached Storage",
            ACTIVE_MODE_ACTION if active_mode == "cache" else "library-cache",
            "Active"
            if active_mode == "cache"
            else "Copy games into ROMCloud-managed local storage as needed, then play them locally.",
            active=active_mode == "cache",
        )
    )
    items.append(
        MenuItem(
            "Offline",
            ACTIVE_MODE_ACTION if active_mode == "offline" else "library-offline",
            "Active"
            if active_mode == "offline"
            else "Show and launch only games already available locally.",
            active=active_mode == "offline",
        )
    )
    items.append(MenuItem("Storage", f"{CATEGORY_ACTION_PREFIX}Storage"))
    if capabilities.get("save_sync", True):
        items.append(MenuItem("SaveSync", SAVESYNC_ACTION))
    if capabilities.get("update_network", True):
        items.append(MenuItem("Maintenance", f"{CATEGORY_ACTION_PREFIX}Maintenance"))
    items.append(MenuItem("Exit", EXIT_ACTION))
    return tuple(items)


def menu_categories_for_state(
    state: dict[str, object], library_sync_enabled: bool = False
) -> dict[str, tuple[MenuItem, ...]]:
    capabilities = state.get("capabilities", {})
    capabilities = capabilities if isinstance(capabilities, dict) else {}
    library: list[MenuItem] = [MenuItem("Catalog Status", "status")]
    if capabilities.get("catalog_refresh", True):
        library.append(MenuItem("Refresh Catalog", "refresh"))
    library.append(MenuItem("Cache Status", "cache-status"))
    if library_sync_enabled and capabilities.get("library_sync", True):
        library.extend(
            (
                MenuItem(
                    "Sync Missing Media",
                    LIBRARY_QUICK_SYNC_ACTION,
                    "Quick Sync: copies missing media and skips existing payloads.",
                ),
                MenuItem(
                    "Verify & Repair Media",
                    LIBRARY_FULL_SYNC_ACTION,
                    "Full Sync: verifies or repairs existing media and may take significantly longer.",
                ),
            )
        )
    storage = MENU_CATEGORIES["Storage"]
    if not capabilities.get("remote_validation", True):
        storage = tuple(item for item in storage if item.action != SETUP_ACTION)
    return {**MENU_CATEGORIES, "Library": tuple(library), "Storage": storage}


def menu_categories_for_mode(
    mode: str,
    offline_library_mode: bool = False,
    library_sync_enabled: bool = False,
) -> dict[str, tuple[MenuItem, ...]]:
    """Compatibility wrapper around the serialized-policy menu builder."""
    return menu_categories_for_state(
        {
            **_fallback_operating_state(mode),
            "operating_mode": (
                "offline"
                if offline_library_mode
                else "connected"
                if mode == "direct_nas"
                else "cache"
            ),
            "connected_mode": not offline_library_mode and mode == "direct_nas",
            "cache_mode": not offline_library_mode and mode != "direct_nas",
            "offline_mode": offline_library_mode,
        },
        library_sync_enabled,
    )


ROOT_MENU_ITEMS = root_menu_items_for_state(_fallback_operating_state("smart_cache"))

# Actions dispatched through the reusable long-running operation screen
# (see operation_screen.py) instead of a quick blocking uidata JSON call.
# Reusing this screen for a later action (update, repair, diagnostics,
# mount/reconnect, sync) is just adding an entry here — never a change to
# the screen itself. Each argv is an existing, already-safe CLI entry
# point (never a new backend command).
_OPERATIONS: dict[str, OperationSpec] = {
    "connection-mount": OperationSpec(
        title="Mount / Reconnect", args=("uidata", "connection-mount")
    ),
    "connection-unmount": OperationSpec(
        title="Unmount", args=("uidata", "connection-unmount")
    ),
    "refresh": OperationSpec(title="Refresh Catalog", args=("uidata", "refresh-progress")),
    "library-offline": OperationSpec(
        title="Offline",
        args=("uidata", "library-offline"),
        exits_after_mode_change=True,
    ),
    "library-cache": OperationSpec(
        title="Cached Storage",
        args=("uidata", "library-cache"),
        exits_after_mode_change=True,
    ),
    "library-connected": OperationSpec(
        title="Direct",
        args=("uidata", "library-connected"),
        exits_after_mode_change=True,
    ),
    "update-check": OperationSpec(
        title="Check for Updates",
        args=("uidata", "update-check"),
        max_runtime=35.0,
    ),
    "update-install": OperationSpec(
        title="Update ROMCloud", args=("uidata", "update-install"), arms_gui_relaunch=True
    ),
}
_MODE_TRANSITION_ACTIONS = frozenset(
    {"library-connected", "library-cache", "library-offline"}
)

_BG_COLOR = (18, 18, 24)
_CARD_BG = (40, 40, 50)
_FG_COLOR = (230, 230, 230)
_SELECTED_BG = (60, 90, 160)
_SUCCESS_COLOR = (90, 200, 120)
_WARNING_COLOR = (230, 180, 60)
_ERROR_COLOR = (220, 90, 90)
_HINT_COLOR = (150, 150, 150)
_MESSAGE_COLORS = {
    "success": _SUCCESS_COLOR,
    "warning": _WARNING_COLOR,
    "error": _ERROR_COLOR,
    "info": _FG_COLOR,
}

_STATE_LABELS = {
    OperationState.STARTING: "Starting…",
    OperationState.RUNNING: "Running…",
    OperationState.SUCCEEDED: "Succeeded",
    OperationState.FAILED: "Failed",
}
_OPERATION_HINT_RUNNING = "B/Esc cancel and return to menu — Exit always wins"
_OPERATION_HINT_FINISHED = "A/Enter/Esc/Tap return to menu   Up/Down scroll"

# Fallback used only if the display driver reports a nonsensical size
# (e.g. a headless/dummy driver) — never a "design resolution" the real
# layout is scaled against.
_FALLBACK_SIZE = (1280, 720)
_MIN_SANE_DIMENSION = 240

_TARGET_FPS = 30
_HINT_TEXT = "Up/Down/Left/Right select   Enter confirm   Esc exit"
_WIZARD_HINT = "A/Enter select   B/Esc back   Menu/Tab technical details"

_LOGGER = logging.getLogger(__name__)


def format_result(action: str, result: BackendResult) -> str:
    """Render a :class:`BackendResult` as a single display line.

    Pure function — no pygame — so it is fully unit-tested.
    """
    if not result.ok:
        return f"Error: {result.error}"
    if action == "connection-status":
        state = str(result.data.get("state", "unknown")).replace("_", " ").title()
        source = result.data.get("source", "")
        mount_point = result.data.get("mount_point", "")
        return f"{state} | {source} → {mount_point}"
    if action in ("status", "healthcheck"):
        source_type = result.data.get("source_type")
        source_description = result.data.get("source_description")
        source_bits = [bit for bit in (source_type, source_description) if bit]
        source_prefix = " | ".join(source_bits)
        if action == "status":
            stats = [
                f"games={result.data.get('games_total', 0)}",
                f"cached={result.data.get('cached', 0)}",
                f"pinned={result.data.get('pinned', 0)}",
            ]
            body = f"{' | '.join(source_bits)} | {' '.join(stats)}" if source_prefix else " ".join(stats)
            operating_state = result.data.get("operating_state", {})
            selected = (
                operating_state.get("operating_mode")
                if isinstance(operating_state, dict)
                else None
            )
            if selected is None and result.data.get("offline_library_mode"):
                selected = "offline"
            if selected in {"connected", "cache", "offline"}:
                mode_labels = {
                    "connected": "Direct",
                    "cache": "Cached Storage",
                    "offline": "Offline",
                }
                body += f" | {mode_labels[str(selected)]}"
            return f"{action}: {body}"
        reachable = result.data.get("source_reachable")
        body = f"{source_prefix} | {'reachable' if reachable else 'unreachable'}" if source_prefix else (
            "reachable" if reachable else "unreachable"
        )
        if result.data.get("remote_data_configured"):
            remote = (
                "writable"
                if result.data.get("remote_data_reachable")
                else "unreachable/read-only"
            )
            body += f" | ROMCloud data: {remote}"
        return f"{action}: {body}"
    return f"{action}: {result.data}"


def classify_message_kind(action: str, result: BackendResult) -> str:
    """Classify a quick backend call's outcome for dashboard display —
    distinguishes an outright failure from a merely-unreachable-but-not-broken
    healthcheck result, so the UI never paints a soft warning the same red
    as a real failure.

    Pure function — no pygame — so it is fully unit-tested.
    """
    if not result.ok:
        return "error"
    if action == "connection-status":
        if result.data.get("state") == "error":
            return "error"
        if result.data.get("state") in ("disconnected", "connecting"):
            return "warning"
    if action == "healthcheck" and (
        result.data.get("source_reachable") is False
        or (
            result.data.get("remote_data_configured")
            and result.data.get("remote_data_reachable") is False
        )
    ):
        return "warning"
    return "success"


def start_operation(action: str, romcloud_bin: str, *, popen=None) -> OperationScreenState:  # noqa: ANN001
    """Launch the backend subprocess for *action* and wrap it in a fresh
    :class:`OperationScreenState`. *popen* is injectable for tests."""
    spec = _OPERATIONS[action]
    argv = [romcloud_bin, *spec.args]
    runner_kwargs = {"max_runtime": spec.max_runtime}
    if spec.max_runtime is not None:
        runner_kwargs["timeout_message"] = (
            "Update check timed out; check internet connectivity and try again."
        )
    if popen is not None:
        runner_kwargs["popen"] = popen
    runner = OperationRunner(argv, **runner_kwargs)
    runner.start()
    return OperationScreenState(
        title=spec.title,
        runner=runner,
        arms_gui_relaunch=spec.arms_gui_relaunch,
        exits_after_mode_change=spec.exits_after_mode_change,
    )


def operation_summary_message(operation: OperationScreenState) -> tuple[str, str]:
    """The dashboard message/kind to show after returning from a finished
    operation screen — this is how the dashboard "refreshes" its status
    line to reflect what just happened.

    Pure function — no pygame — so it is fully unit-tested.
    """
    if operation.succeeded:
        return f"{operation.title}: succeeded", "success"
    detail = operation.runner.error
    suffix = f" ({detail})" if detail else ""
    return f"{operation.title}: failed{suffix}", "error"


def completed_mode_transition_requires_exit(operation: OperationScreenState) -> bool:
    """Return whether a completed operation owns the normal GUI exit handoff.

    A mode transition is terminal only after the backend confirms that the
    requested mode really changed and that the existing EmulationStation
    restart path was requested.  Same-mode no-ops and failed transitions stay
    in ROMCloud.  This helper never touches ``GuiRelaunchCoordinator``.
    """
    if (
        not operation.exits_after_mode_change
        or not operation.is_finished
        or not operation.succeeded
    ):
        return False
    result = operation_result(operation.runner)
    return bool(
        result.ok
        and result.data.get("mode_changed")
        and result.data.get("es_restart_requested")
    )


def render_completed_mode_transition_exit(
    operation: OperationScreenState,
    splash: SplashRenderer,
) -> bool:
    """Paint the terminal mode-change notice before normal GUI shutdown."""
    if not completed_mode_transition_requires_exit(operation):
        return False
    splash.render(
        "Mode changed successfully.",
        "Refreshing EmulationStation game list…",
        1.0,
    )
    return True


def request_relaunch_for_completed_update(
    operation: OperationScreenState,
    relaunch: GuiRelaunchCoordinator,
) -> bool:
    """Enter the terminal relaunch state for one confirmed update success."""
    if not operation.arms_gui_relaunch or not operation.is_finished:
        return False
    if not operation.succeeded:
        relaunch.mark_update_failed()
        return False
    result = operation_result(operation.runner)
    # The final successful JSON is written only after perform_update has
    # returned, so the updater's install/reconciliation/post-update work is
    # committed before this process is allowed to enter its terminal state.
    progress_complete = result.ok
    return relaunch.mark_update_succeeded(progress_complete=progress_complete)


def render_completed_update_relaunch(
    operation: OperationScreenState,
    relaunch: GuiRelaunchCoordinator,
    splash: SplashRenderer,
) -> bool:
    """Confirm update success and paint the terminal frame before shutdown."""
    if not request_relaunch_for_completed_update(operation, relaunch):
        return False
    splash.render("Update complete", "Restarting ROMCloud…", 1.0)
    return True


def initial_screen_for_status(status: BackendResult) -> str:
    """Choose startup routing from the backend's structural setup state.

    Routes to the first-run wizard only when the backend explicitly reports
    a fresh or structurally incomplete installation. A failed backend call
    (subprocess timeout/error, e.g. while the system is still finishing a
    resume-from-suspend) is a communication failure, not evidence the
    installation is unconfigured — it must never be treated as "fresh" and
    silently open the setup wizard over an already-configured install.
    """
    if status.ok and status.data.get("state") in ("fresh", "partial"):
        return "wizard"
    return "menu"


def run_app(
    romcloud_bin: str,
    *,
    relaunch_popen=None,  # noqa: ANN001 - injectable process factory for tests
    relaunch_failure_log: Path = RELAUNCH_FAILURE_LOG,
) -> int:
    """Entry point. Returns a process exit code; never raises.

    Any failure (pygame missing, display init failure, unexpected crash)
    is caught here and reported to stderr with a non-zero exit code —
    Batocera's Ports launcher must always get control back cleanly.
    """
    diagnostics = DisplayDiagnostics(romcloud_bin)
    diagnostics.record("python_run_app_start", environment=diagnostics.environment())
    diagnostics.record("pygame_import_before")
    try:
        import pygame
    except ImportError as exc:
        diagnostics.record("pygame_import_failed", error=str(exc))
        print(
            f"error: pygame is not available under this Python interpreter: {exc}",
            file=sys.stderr,
        )
        return 1
    try:
        sdl_version = pygame.get_sdl_version()
    except Exception:  # noqa: BLE001 - version reporting is diagnostic only
        sdl_version = "unknown"
    diagnostics.record(
        "pygame_import_after",
        pygame_version=getattr(getattr(pygame, "version", None), "ver", "unknown"),
        sdl_version=sdl_version,
    )

    try:
        relaunch = GuiRelaunchCoordinator(romcloud_bin)
        exit_code = _run(pygame, romcloud_bin, relaunch, diagnostics)
        diagnostics.record(
            "pygame_run_returned",
            exit_code=exit_code,
            relaunch_pending=relaunch.relaunch_pending,
        )
        if not relaunch.relaunch_pending:
            return exit_code
        kwargs = {"failure_log_path": relaunch_failure_log}
        if relaunch_popen is not None:
            kwargs["popen"] = relaunch_popen
        diagnostics.record("replacement_launch_before", launcher=str(relaunch.launcher))
        result = relaunch.launch_once(**kwargs)
        diagnostics.record(
            "replacement_launch_after",
            attempted=result.attempted,
            launched=result.launched,
            error=result.error,
        )
        if result.launched:
            return 0
        print(relaunch_failure_message(result), file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001 — must never crash Batocera's Ports flow
        diagnostics.record("python_run_app_failed", error=str(exc))
        print(f"error: ports UI crashed: {exc}", file=sys.stderr)
        return 1


def _detect_screen_size(  # noqa: ANN001
    pygame,
    diagnostics: DisplayDiagnostics | None = None,
) -> tuple[int, int]:
    """Current display resolution, defensively clamped.

    Some drivers (e.g. a headless/dummy SDL video driver) can report a
    zero or nonsensical size; fall back to a safe default rather than
    ever computing a layout against a degenerate (0, 0) screen.
    """
    if diagnostics is not None:
        diagnostics.record("display_info_before")
    try:
        info = pygame.display.Info()
        w, h = info.current_w, info.current_h
        if diagnostics is not None:
            diagnostics.record("display_info_after", width=w, height=h)
    except Exception as exc:  # noqa: BLE001
        w, h = _FALLBACK_SIZE
        if diagnostics is not None:
            diagnostics.record(
                "display_info_failed",
                error=str(exc),
                fallback_width=w,
                fallback_height=h,
            )

    if w < _MIN_SANE_DIMENSION or h < _MIN_SANE_DIMENSION:
        if diagnostics is not None:
            diagnostics.record(
                "display_info_rejected",
                width=w,
                height=h,
                fallback_width=_FALLBACK_SIZE[0],
                fallback_height=_FALLBACK_SIZE[1],
            )
        return _FALLBACK_SIZE
    return w, h


def _open_display(  # noqa: ANN001
    pygame,
    screen_w: int,
    screen_h: int,
    diagnostics: DisplayDiagnostics | None = None,
):
    """Open a desktop-sized borderless window without changing video mode.

    Exclusive fullscreen remains the first fallback for drivers that cannot
    create a borderless window, followed by the historical windowed fallback.
    """
    requested = (screen_w, screen_h)
    os.environ.setdefault("SDL_VIDEO_WINDOW_POS", "0,0")
    driver = "unknown"
    try:
        driver = pygame.display.get_driver()
    except Exception:  # noqa: BLE001 - diagnostic only
        pass

    if diagnostics is not None:
        diagnostics.record(
            "display_open_before",
            requested_size=list(requested),
            preferred_path="borderless-desktop",
            video_driver=driver,
            environment=diagnostics.environment(),
        )
    try:
        screen = pygame.display.set_mode(requested, pygame.NOFRAME)
    except Exception as exc:  # noqa: BLE001
        try:
            _LOGGER.warning(
                "Borderless display initialization failed; falling back to exclusive fullscreen: %s",
                exc,
            )
        except Exception:  # noqa: BLE001 - diagnostics must not break fallback
            pass
        if diagnostics is not None:
            diagnostics.record("display_borderless_failed", error=str(exc))
    else:
        _record_display_opened(
            pygame,
            screen,
            "borderless-desktop",
            pygame.NOFRAME,
            diagnostics,
        )
        return screen

    try:
        screen = pygame.display.set_mode(requested, pygame.FULLSCREEN)
    except Exception as exc:  # noqa: BLE001
        try:
            _LOGGER.warning(
                "Exclusive fullscreen initialization failed; falling back to windowed mode: %s",
                exc,
            )
        except Exception:  # noqa: BLE001 - diagnostics must not break fallback
            pass
        if diagnostics is not None:
            diagnostics.record("display_exclusive_failed", error=str(exc))
        screen = pygame.display.set_mode(requested)
        selected_path = "windowed-fallback"
        selected_flags = 0
    else:
        selected_path = "exclusive-fullscreen-fallback"
        selected_flags = pygame.FULLSCREEN

    _record_display_opened(
        pygame,
        screen,
        selected_path,
        selected_flags,
        diagnostics,
    )
    return screen


def _record_display_opened(  # noqa: ANN001
    pygame,
    screen,
    selected_path: str,
    selected_flags: int,
    diagnostics: DisplayDiagnostics | None,
) -> None:
    """Record the selected path and SDL's post-creation display dimensions."""
    surface_size = list(screen.get_size())
    try:
        _LOGGER.info(
            "Display path selected: %s (%sx%s)",
            selected_path,
            surface_size[0],
            surface_size[1],
        )
    except Exception:  # noqa: BLE001 - diagnostics must not break startup
        pass

    if diagnostics is None:
        return

    fields: dict[str, object] = {
        "selected_path": selected_path,
        "selected_flags": selected_flags,
        "surface_size": surface_size,
    }
    try:
        info = pygame.display.Info()
        fields["display_info_size"] = [info.current_w, info.current_h]
    except Exception as exc:  # noqa: BLE001 - diagnostic only
        fields["display_info_error"] = str(exc)
    diagnostics.record("display_open_after", **fields)


def _load_startup_backend_state(  # noqa: ANN001
    splash: SplashRenderer,
    romcloud_bin: str,
) -> tuple[BackendResult, BackendResult | None]:
    """Paint real startup stages around the existing synchronous checks."""
    splash.render("Starting ROMCloud…", "Display ready", 0.12)
    splash.render("Starting ROMCloud…", "Loading setup and configuration…", 0.25)
    setup_status = call_backend(romcloud_bin, "setup-status")
    if initial_screen_for_status(setup_status) == "wizard":
        splash.render("Starting ROMCloud…", "Preparing setup…", 0.40)
        return setup_status, None

    splash.render("Starting ROMCloud…", "Checking source availability…", 0.40)
    connection = call_backend(romcloud_bin, "connection-status")
    splash.render("Starting ROMCloud…", "Loading ROMCloud interface…", 0.75)
    return setup_status, connection


def _build_fonts(pygame, layout: Layout):  # noqa: ANN001
    return {
        "title": pygame.font.SysFont(None, layout.fonts.title),
        "body": pygame.font.SysFont(None, layout.fonts.body),
        "hint": pygame.font.SysFont(None, layout.fonts.hint),
    }


_REMAPPABLE_ACTIONS: tuple[Action, ...] = (
    Action.UP,
    Action.DOWN,
    Action.LEFT,
    Action.RIGHT,
    Action.CONFIRM,
    Action.BACK,
    Action.PREVIOUS_PAGE,
    Action.NEXT_PAGE,
    Action.MENU,
)
_CONTROLLER_ACTION_LABELS: dict[Action, str] = {
    Action.UP: "Up",
    Action.DOWN: "Down",
    Action.LEFT: "Left",
    Action.RIGHT: "Right",
    Action.CONFIRM: "Confirm / A",
    Action.BACK: "Back / B",
    Action.PREVIOUS_PAGE: "Previous Page / LB",
    Action.NEXT_PAGE: "Next Page / RB",
    Action.MENU: "Menu / Start",
}
_CONTROLLER_TEST_HINT = "D-pad/stick select   A/Enter remap   B/Esc back"


class _ControllerTestScreenState:
    """Local UI state for controller mapping/diagnostics — which
    action slot is focused, and whether a remap capture is in progress."""

    def __init__(self) -> None:
        self.selected_index = 0
        self.remap_instance_id: Optional[int] = None

    def select(self, index: int) -> None:
        self.selected_index = max(0, min(index, len(_REMAPPABLE_ACTIONS) - 1))


def cancel_owned_background_work(
    *,
    update_check: UpdateCheckState | None,
    operation_screen: OperationScreenState | None,
    wizard: WizardState | None,
    savesync_screen: SaveSyncScreenState | None,
    library_sync_screen: LibrarySyncScreenState | None,
) -> None:
    """Cancel every subprocess still owned by this GUI session."""
    owners = (
        (update_check, "cancel"),
        (operation_screen, "cancel_pending"),
        (wizard, "cancel_pending"),
        (savesync_screen, "cancel_pending"),
        (library_sync_screen, "cancel_pending"),
    )
    for owner, method_name in owners:
        if owner is None:
            continue
        try:
            getattr(owner, method_name)()
        except Exception:  # noqa: BLE001 - shutdown must continue regardless
            _LOGGER.exception("Could not cancel GUI-owned background work")


def _run(  # noqa: ANN001
    pygame,
    romcloud_bin: str,
    relaunch: GuiRelaunchCoordinator,
    diagnostics: DisplayDiagnostics,
) -> int:
    diagnostics.record("pygame_init_before")
    pygame.init()
    try:
        video_initialized: bool | str = bool(pygame.display.get_init())
    except Exception:  # noqa: BLE001 - state reporting is diagnostic only
        video_initialized = "unknown"
    diagnostics.record(
        "pygame_init_after",
        video_initialized=video_initialized,
    )
    # Best-effort: joystick/controller subsystem init failures (e.g. no
    # input backend on a minimal SDL build) must never prevent the rest of
    # the UI from working — keyboard/touch remain fully usable regardless.
    try:
        pygame.joystick.init()
    except Exception:  # noqa: BLE001
        pass
    controller_module = getattr(pygame, "controller", None)
    if controller_module is not None:
        try:
            controller_module.init()
        except Exception:  # noqa: BLE001
            pass

    input_debug: InputDebugLogger | None = None
    wizard: WizardState | None = None
    operation_screen: Optional[OperationScreenState] = None
    savesync_screen: Optional[SaveSyncScreenState] = None
    library_sync_screen: Optional[LibrarySyncScreenState] = None
    update_check: UpdateCheckState | None = None

    try:
        screen_w, screen_h = _detect_screen_size(pygame, diagnostics)
        screen = _open_display(pygame, screen_w, screen_h, diagnostics)
        screen_w, screen_h = screen.get_size()
        diagnostics.record("layout_dimensions_selected", width=screen_w, height=screen_h)
        pygame.display.set_caption("ROMCloud")
        splash = SplashRenderer(pygame, screen)

        setup_status, connection = _load_startup_backend_state(splash, romcloud_bin)
        operating_state = operating_state_from_status(setup_status.data)
        library_sync_enabled = bool(setup_status.data.get("library_sync_enabled", False))
        state = NavigationState(
            root_menu_items_for_state(operating_state),
            menu_categories_for_state(operating_state, library_sync_enabled),
        )
        if initial_screen_for_status(setup_status) == "wizard":
            wizard = WizardState(setup_status)
        layout = compute_layout(screen_w, screen_h, len(state.items))
        fonts = _build_fonts(pygame, layout)
        clock = pygame.time.Clock()

        input_manager = InputManager(pygame, romcloud_bin)
        try:
            input_manager.controllers.open_existing_devices(pygame.joystick.get_count())
        except Exception:  # noqa: BLE001
            pass

        input_debug = InputDebugLogger(pygame)
        try:
            input_debug.log_startup(
                joystick_count=pygame.joystick.get_count(),
                controller_module_present=controller_module is not None,
                snapshots=input_manager.controllers.snapshots(),
            )
        except Exception:  # noqa: BLE001 - diagnostics must never affect the UI
            pass

        controller_test = _ControllerTestScreenState()
        activity = ActivityLog(max_events=250)
        catalog_progress = CatalogRefreshProgress()
        update_check = UpdateCheckState()
        capabilities = operating_state.get("capabilities", {})
        if not isinstance(capabilities, dict) or capabilities.get("update_network", True):
            update_check.start(romcloud_bin)
        splash.render("Starting ROMCloud…", "Finishing startup…", 0.90)
        current_screen = "wizard" if wizard is not None else "menu"
        message: Optional[str] = None
        message_kind = "info"
        if current_screen == "menu":
            assert connection is not None
            message = format_result("connection-status", connection)
            message_kind = classify_message_kind("connection-status", connection)
        splash.render("Starting ROMCloud…", "Ready", 1.0)

        running = True
        text_input_active = False
        while running:
            dt_ms = clock.tick(_TARGET_FPS)
            dt = dt_ms / 1000.0
            now = pygame.time.get_ticks() / 1000.0

            for event in pygame.event.get():
                if event.type == pygame.QUIT:
                    running = False
                    continue
                if event.type == pygame.VIDEORESIZE:
                    screen_w, screen_h = event.w, event.h
                    layout = compute_layout(screen_w, screen_h, len(state.items))
                    fonts = _build_fonts(pygame, layout)
                    continue
                if (
                    event.type == getattr(pygame, "MOUSEWHEEL", object())
                    and layout.activity_rect is not None
                ):
                    rows = max(
                        1,
                        layout.activity_rect.h // max(1, layout.fonts.hint + 7),
                    )
                    activity.scroll(-int(getattr(event, "y", 0)), rows)
                    continue

                if input_debug is not None:
                    try:
                        input_debug.log_event(event)
                    except Exception:  # noqa: BLE001 - logging is best-effort only
                        pass

                if current_screen == "menu":
                    rects = list(layout.card_rects)
                    if update_check.update_available:
                        rects.append(_update_banner_rect(layout))
                elif current_screen == "wizard" and wizard is not None:
                    rects = _wizard_rects(layout, wizard)
                elif current_screen == OPERATION_SCREEN:
                    # The whole safe area is one big "tap anywhere" target
                    # — the operation screen's only touch interaction is
                    # dismissing it once finished (see handle_operation_event).
                    rects = (layout.safe_area,)
                elif current_screen == "savesync":
                    if (
                        savesync_screen is not None
                        and savesync_screen.step == CONFLICTS
                    ):
                        rects = conflict_action_rects(layout)
                    else:
                        rects = (layout.safe_area,)
                elif current_screen == "library_sync":
                    rects = (layout.safe_area,)
                else:
                    rects = ()
                ievent = input_manager.handle_event(
                    event,
                    screen_w=screen_w,
                    screen_h=screen_h,
                    rects=rects,
                    text_mode=current_screen == "wizard" and wizard is not None and wizard.is_text_mode,
                    now=now,
                )

                if current_screen == "controller_test" and controller_test.remap_instance_id is not None:
                    # While awaiting a remap capture, raw controller events
                    # are consumed by ControllerManager itself (it returns
                    # no action for the captured event) — nothing else to
                    # do here except notice capture completed, or let a
                    # physical keyboard BACK cancel a stuck capture (e.g.
                    # a controller with no obvious "back" button to press).
                    if input_manager.controllers.remap_pending_action is None:
                        controller_test.remap_instance_id = None
                    elif ievent.action == Action.BACK:
                        input_manager.controllers.cancel_remap()
                        controller_test.remap_instance_id = None
                    continue

                if ievent.action is None:
                    continue

                if current_screen == "menu":
                    item = state.selected_item
                    if (
                        ievent.touch_index == len(state.items)
                        and update_check.update_available
                    ):
                        state.open_category("Maintenance", action="update-install")
                        layout = compute_layout(screen_w, screen_h, len(state.items))
                    elif ievent.action == Action.MENU and update_check.update_available:
                        state.open_category("Maintenance", action="update-install")
                        layout = compute_layout(screen_w, screen_h, len(state.items))
                    elif (
                        ievent.action == Action.CONFIRM
                        and item.action == "update-install"
                        and update_check.checking
                    ):
                        message = "Update check is still running. Please wait."
                        message_kind = "info"
                    elif ievent.action == Action.CONFIRM and item.action == SAVESYNC_ACTION:
                        savesync_screen = SaveSyncScreenState(romcloud_bin=romcloud_bin)
                        savesync_screen.start_loading()
                        current_screen = "savesync"
                    elif (
                        ievent.action == Action.CONFIRM
                        and item.action
                        in (LIBRARY_QUICK_SYNC_ACTION, LIBRARY_FULL_SYNC_ACTION)
                    ):
                        library_sync_screen = LibrarySyncScreenState(
                            romcloud_bin=romcloud_bin,
                            sync_mode=(
                                "full"
                                if item.action == LIBRARY_FULL_SYNC_ACTION
                                else "quick"
                            ),
                        )
                        library_sync_screen.start_preview()
                        current_screen = "library_sync"
                    elif ievent.action == Action.CONFIRM and item.action == SETUP_ACTION:
                        wizard = WizardState(call_backend(romcloud_bin, "setup-status"))
                        current_screen = "wizard"
                    else:
                        running, current_screen, message, message_kind, new_operation = _handle_menu_event(
                            ievent, state, layout, romcloud_bin, running, message, message_kind,
                            operation_screen,
                        )
                        if new_operation is not None:
                            operation_screen = new_operation
                        layout = compute_layout(screen_w, screen_h, len(state.items))
                elif current_screen == "savesync" and savesync_screen is not None:
                    current_screen = _handle_savesync_event(ievent, savesync_screen)
                    if current_screen == "menu":
                        savesync_screen.cancel_pending()
                        savesync_screen = None
                elif current_screen == "library_sync" and library_sync_screen is not None:
                    current_screen = _handle_library_sync_event(
                        ievent, library_sync_screen
                    )
                    if current_screen == "menu":
                        library_sync_screen = None
                elif current_screen == "wizard" and wizard is not None:
                    if ievent.action == Action.BACK and wizard.step == WizardStep.WELCOME:
                        running = False
                    elif (
                        ievent.touch_index is not None
                        and not wizard.osk_visible
                        and ievent.touch_index == len(_wizard_option_rows(layout, wizard))
                    ):
                        if wizard.step == WizardStep.WELCOME:
                            running = False
                        else:
                            wizard.back()
                    else:
                        if ievent.touch_index is not None and not wizard.osk_visible:
                            rows = _wizard_option_rows(layout, wizard)
                            if 0 <= ievent.touch_index < len(rows):
                                actual_index = rows[ievent.touch_index][0]
                                ievent = InputEvent(
                                    action=ievent.action,
                                    touch_index=actual_index,
                                    text=ievent.text,
                                    source=ievent.source,
                                )
                        wizard.handle_event(ievent, rects, romcloud_bin)
                elif current_screen == "controller_test":
                    current_screen = _handle_controller_test_event(
                        ievent, controller_test, input_manager,
                    )
                elif current_screen == OPERATION_SCREEN and operation_screen is not None:
                    if render_completed_update_relaunch(
                        operation_screen, relaunch, splash
                    ):
                        # Ignore this and all remaining queued application
                        # input.  The old process may render the final state,
                        # but it must never return to normal controls.
                        update_check.update_available = False
                        current_screen = "restarting"
                        running = False
                    elif render_completed_mode_transition_exit(
                        operation_screen, splash
                    ):
                        # The backend process has reconciled presentation,
                        # requested the ES restart, and exited.  Close the old
                        # GUI through the normal pygame cleanup path; never arm
                        # the updater's replacement-launch coordinator.
                        current_screen = "mode-transition-exit"
                        running = False
                    elif operation_screen.title == "Refresh Catalog":
                        if ievent.action == Action.UP:
                            catalog_progress.scroll(-1, 6)
                        elif ievent.action == Action.DOWN:
                            catalog_progress.scroll(1, 6)
                    if running and not relaunch.terminal:
                        current_screen = handle_operation_event(ievent, operation_screen)
                    if current_screen == "menu" and not relaunch.terminal:
                        if operation_screen.title == "Check for Updates":
                            update_check = UpdateCheckState.completed(
                                operation_result(operation_screen.runner)
                            )
                            message = (
                                update_check.banner
                                if update_check.update_available
                                else "ROMCloud is up to date"
                                if update_check.status == "current"
                                else f"Update check failed: {update_check.error}"
                            )
                            message_kind = (
                                "warning" if update_check.update_available else
                                "success" if update_check.status == "current" else "error"
                            )
                        elif operation_screen.title in ("Mount / Reconnect", "Unmount"):
                            connection = call_backend(romcloud_bin, "connection-status")
                            message = format_result("connection-status", connection)
                            message_kind = classify_message_kind("connection-status", connection)
                        elif operation_screen.title in ("Direct", "Cached Storage", "Offline"):
                            setup_status = call_backend(romcloud_bin, "setup-status")
                            operating_state = operating_state_from_status(setup_status.data)
                            library_sync_enabled = bool(
                                setup_status.data.get("library_sync_enabled", False)
                            )
                            state = NavigationState(
                                root_menu_items_for_state(operating_state),
                                menu_categories_for_state(operating_state, library_sync_enabled),
                            )
                            if bool(operating_state.get("offline_mode", False)):
                                update_check.update_available = False
                            message, message_kind = operation_summary_message(operation_screen)
                        else:
                            message, message_kind = operation_summary_message(operation_screen)
                        operation_screen = None

            # Drain output/state every frame regardless of whether an input
            # event arrived this frame — the whole point of the operation
            # screen is that the backend keeps working (and producing
            # output) while the UI keeps rendering and waiting for input.
            for line in update_check.poll():
                activity.ingest(line.text)

            if current_screen == OPERATION_SCREEN and operation_screen is not None:
                for line in operation_screen.poll():
                    event = activity.ingest(line.text)
                    if event is not None:
                        catalog_progress.ingest(event)
                if operation_screen.arms_gui_relaunch and operation_screen.is_finished:
                    if render_completed_update_relaunch(
                        operation_screen, relaunch, splash
                    ):
                        update_check.update_available = False
                        current_screen = "restarting"
                        running = False
                elif operation_screen.is_finished:
                    if render_completed_mode_transition_exit(operation_screen, splash):
                        current_screen = "mode-transition-exit"
                        running = False
            elif current_screen == "savesync" and savesync_screen is not None:
                for line in savesync_screen.poll():
                    activity.ingest(line.text)
                savesync_screen.update(dt)
            elif current_screen == "library_sync" and library_sync_screen is not None:
                for line in library_sync_screen.poll():
                    activity.ingest(line.text)
                library_sync_screen.update_confirm(dt)
            elif current_screen == "wizard" and wizard is not None:
                for line in wizard.poll():
                    activity.ingest(line.text)
                if wizard.finished:
                    current_screen = "menu"
                    setup_status = call_backend(romcloud_bin, "setup-status")
                    operating_state = operating_state_from_status(setup_status.data)
                    library_sync_enabled = bool(
                        setup_status.data.get("library_sync_enabled", False)
                    )
                    state = NavigationState(
                        root_menu_items_for_state(operating_state),
                        menu_categories_for_state(operating_state, library_sync_enabled),
                    )
                    wizard = None
                    message = "Setup complete"
                    message_kind = "success"

            should_capture_text = bool(
                current_screen == "wizard"
                and wizard is not None
                and wizard.is_text_mode
            )
            if should_capture_text != text_input_active:
                method_name = "start_text_input" if should_capture_text else "stop_text_input"
                method = getattr(getattr(pygame, "key", None), method_name, None)
                if method is not None:
                    method()
                text_input_active = should_capture_text

            if current_screen == "menu":
                for action in input_manager.update(dt):
                    _apply_direction(state, layout, action)
            elif current_screen == "wizard" and wizard is not None:
                rects = _wizard_rects(layout, wizard)
                for action in input_manager.update(dt):
                    wizard.update_direction(action, rects)

            if current_screen == "menu":
                _render_menu(
                    pygame,
                    screen,
                    fonts,
                    layout,
                    state,
                    message,
                    message_kind,
                    activity,
                    update_check,
                )
            elif current_screen == "controller_test":
                _render_controller_test(pygame, screen, fonts, layout, input_manager, controller_test)
            elif current_screen == OPERATION_SCREEN and operation_screen is not None:
                _render_operation(
                    pygame,
                    screen,
                    fonts,
                    layout,
                    operation_screen,
                    activity,
                    catalog_progress,
                )
            elif current_screen == "savesync" and savesync_screen is not None:
                _render_savesync(
                    pygame, screen, fonts, layout, savesync_screen, activity
                )
            elif current_screen == "library_sync" and library_sync_screen is not None:
                _render_library_sync(
                    pygame, screen, fonts, layout, library_sync_screen, activity
                )
            elif current_screen == "wizard" and wizard is not None:
                _render_wizard(
                    pygame, screen, fonts, layout, wizard, wizard.activity
                )
        return 0
    finally:
        cancel_owned_background_work(
            update_check=update_check,
            operation_screen=operation_screen,
            wizard=wizard,
            savesync_screen=savesync_screen,
            library_sync_screen=library_sync_screen,
        )
        if input_debug is not None:
            try:
                input_debug.close()
            except Exception:  # noqa: BLE001
                pass
        diagnostics.record("pygame_quit_before")
        pygame.quit()
        diagnostics.record("pygame_quit_after")


def _apply_direction(
    state: MenuState | NavigationState, layout: Layout, action: Action
) -> None:
    direction = ACTION_DIRECTIONS.get(action)
    if direction is None:
        return
    dx, dy = direction
    # Navigation is intentionally a compact vertical control list.  Left and
    # right retain the historical next/previous behavior for accessibility
    # and for controllers whose hats are exposed oddly by SDL.
    step = dy if dy else dx
    state.select((state.selected_index + step) % len(state.items))


def _handle_menu_event(
    ievent: InputEvent,
    state: MenuState | NavigationState,
    layout: Layout,
    romcloud_bin: str,
    running: bool,
    message: Optional[str],
    message_kind: str,
    active_operation: Optional[OperationScreenState] = None,
) -> tuple[bool, str, Optional[str], str, Optional[OperationScreenState]]:
    action = ievent.action
    next_screen = "menu"

    if ievent.touch_index is not None:
        state.select(ievent.touch_index)

    if action in ACTION_DIRECTIONS:
        _apply_direction(state, layout, action)
        return running, next_screen, None, message_kind, None

    if action == Action.CONFIRM:
        item = state.selected_item
        if isinstance(state, NavigationState) and state.enter_selected_category():
            return running, next_screen, message, message_kind, None
        if item.action == BACK_ACTION and isinstance(state, NavigationState):
            state.back()
        elif item.action == EXIT_ACTION:
            running = False
        elif item.action == CONTROLLER_TEST_ACTION:
            next_screen = "controller_test"
        elif item.action == ACTIVE_MODE_ACTION:
            message = f"{item.label} is active."
            message_kind = "success"
        elif item.action in _OPERATIONS:
            if (
                item.action in _MODE_TRANSITION_ACTIONS
                and active_operation is not None
                and not active_operation.is_finished
            ):
                # Keep the original runner and its progress visible.  This is
                # a defensive debounce for queued/repeated controller,
                # keyboard, or touch confirmations while a transition is
                # already active; never launch another backend worker.
                return (
                    running,
                    OPERATION_SCREEN,
                    message,
                    message_kind,
                    active_operation,
                )
            next_screen = OPERATION_SCREEN
            operation = start_operation(item.action, romcloud_bin)
            return running, next_screen, message, message_kind, operation
        else:
            result = call_backend(romcloud_bin, item.action)
            message = format_result(item.action, result)
            message_kind = classify_message_kind(item.action, result)
        return running, next_screen, message, message_kind, None

    if action == Action.BACK:
        if not isinstance(state, NavigationState):
            running = False
            return running, next_screen, message, message_kind, None
        if state.back():
            return running, next_screen, message, message_kind, None
        # Root-level Back should not terminate from arbitrary selections.
        # Keep explicit exit ownership on the Exit item itself.
        if state.selected_item.action == EXIT_ACTION:
            running = False
        return running, next_screen, message, message_kind, None

    return running, next_screen, message, message_kind, None


def _handle_controller_test_event(
    ievent: InputEvent,
    controller_test: _ControllerTestScreenState,
    input_manager: InputManager,
) -> str:
    action = ievent.action

    if action in ACTION_DIRECTIONS:
        dx, dy = ACTION_DIRECTIONS[action]
        step = dx if dx else dy
        controller_test.select(controller_test.selected_index + step)
        return "controller_test"

    if action == Action.CONFIRM:
        snapshots = input_manager.controllers.snapshots()
        if snapshots:
            target_action = _REMAPPABLE_ACTIONS[controller_test.selected_index]
            instance_id = snapshots[0].instance_id
            if input_manager.controllers.begin_remap(instance_id, target_action):
                controller_test.remap_instance_id = instance_id
        return "controller_test"

    if action == Action.BACK or action == Action.MENU:
        input_manager.controllers.cancel_remap()
        controller_test.remap_instance_id = None
        return "menu"

    return "controller_test"


def _handle_savesync_event(ievent: InputEvent, savesync_screen: SaveSyncScreenState) -> str:
    """Translate one semantic input event into SaveSync screen-state
    changes without depending on pygame.

    Returns the screen name to switch to next ("savesync" to stay, "menu"
    to leave). Availability checking is orthogonal to the workflow step, so
    Dashboard navigation and Back remain live while the bounded check runs.
    Foreground preview, commit, and settings operations remain
    non-interruptible here.
    """
    step = savesync_screen.step

    if step == CONFLICTS:
        savesync_screen.handle_conflict_event(ievent)
        return "savesync"

    if step == DASHBOARD:
        if ievent.action in ACTION_DIRECTIONS:
            _, dy = ACTION_DIRECTIONS[ievent.action]
            if dy:
                savesync_screen.select(savesync_screen.selected_index + dy)
        if ievent.action == Action.CONFIRM:
            if savesync_screen.confirm_dashboard_selection() == "back":
                savesync_screen.cancel_pending()
                return "menu"
            return "savesync"
        if ievent.action == Action.BACK:
            savesync_screen.cancel_pending()
            return "menu"
        return "savesync"

    if step == PREVIEW:
        if ievent.action == Action.CONFIRM:
            savesync_screen.begin_confirm()
            savesync_screen.handle_confirm_event(ievent)
        elif ievent.action == Action.BACK:
            savesync_screen.return_to_dashboard()
        return "savesync"

    if step == CONFIRMING:
        savesync_screen.handle_confirm_event(ievent)
        return "savesync"

    if step == RESULT:
        if ievent.action in (Action.CONFIRM, Action.BACK):
            savesync_screen.return_to_dashboard()
        return "savesync"

    if step == SETTINGS:
        if ievent.action in ACTION_DIRECTIONS:
            _, dy = ACTION_DIRECTIONS[ievent.action]
            if dy:
                savesync_screen.select_setting(
                    savesync_screen.settings_selected_index + dy
                )
        elif ievent.action == Action.CONFIRM:
            savesync_screen.confirm_settings_selection()
        elif ievent.action == Action.BACK:
            savesync_screen.return_to_dashboard()
        return "savesync"

    return "savesync"  # PREVIEWING / COMMITTING / APPLYING_SETTINGS: wait


def _handle_library_sync_event(
    ievent: InputEvent, screen: LibrarySyncScreenState
) -> str:
    """Drive Quick confirm or Full hold-confirm through the shared workflow."""
    if screen.step == LIBRARY_PREFLIGHT:
        if ievent.action == Action.CONFIRM:
            if screen.is_full:
                screen.begin_confirm()
                screen.handle_confirm_event(ievent)
            else:
                screen.start_sync()
        elif ievent.action == Action.BACK:
            return "menu"
    elif screen.step == LIBRARY_CONFIRMING:
        screen.handle_confirm_event(ievent)
    elif screen.step == LIBRARY_IMPORTING:
        if ievent.action == Action.BACK:
            screen.cancel_import()
    elif screen.step == LIBRARY_RESULT:
        if ievent.action == Action.CONFIRM and screen.error:
            screen.retry()
        elif ievent.action in (Action.CONFIRM, Action.BACK):
            return "menu"
    return "library_sync"


def _render_menu(  # noqa: ANN001
    pygame,
    screen,
    fonts: dict,
    layout: Layout,
    state: MenuState | NavigationState,
    message: Optional[str],
    message_kind: str,
    activity: ActivityLog,
    update_check: UpdateCheckState,
) -> None:
    screen.fill(_BG_COLOR)

    title_text = state.title if isinstance(state, NavigationState) else "ROMCloud"
    title = fonts["title"].render(title_text, True, _FG_COLOR)
    screen.blit(title, (layout.safe_area.x, layout.safe_area.y))

    if update_check.update_available:
        banner_area = _update_banner_rect(layout)
        pygame.draw.rect(
            screen,
            _CARD_BG,
            (banner_area.x, banner_area.y, banner_area.w, banner_area.h),
            border_radius=5,
        )
        banner = fonts["hint"].render(
            f"{update_check.banner}   [Update ROMCloud]", True, _WARNING_COLOR
        )
        banner_rect = banner.get_rect(center=banner_area.center)
        screen.blit(banner, banner_rect)

    for i, (item, rect) in enumerate(zip(state.items, layout.card_rects)):
        color = (
            _SELECTED_BG
            if i == state.selected_index
            else (45, 75, 55)
            if item.active
            else _CARD_BG
        )
        pygame.draw.rect(screen, color, (rect.x, rect.y, rect.w, rect.h), border_radius=6)
        if item.active:
            pygame.draw.rect(
                screen,
                _SUCCESS_COLOR,
                (rect.x, rect.y, rect.w, rect.h),
                width=3,
                border_radius=6,
            )
        label = fonts["body"].render(item.label, True, _FG_COLOR)
        center_x, center_y = rect.center
        label_center_y = center_y - (layout.fonts.hint // 2 if item.description else 0)
        label_rect = label.get_rect(center=(center_x, label_center_y))
        screen.blit(label, label_rect)
        if item.description:
            max_chars = max(12, rect.w // max(6, layout.fonts.hint // 2))
            description = fonts["hint"].render(
                item.description[:max_chars], True, _HINT_COLOR
            )
            description_rect = description.get_rect(
                center=(center_x, label_rect.bottom + layout.fonts.hint)
            )
            screen.blit(description, description_rect)

    if message:
        max_chars = max(1, layout.message_rect.w // 8)
        color = _MESSAGE_COLORS.get(message_kind, _FG_COLOR)
        text = fonts["body"].render(message[:max_chars], True, color)
        screen.blit(text, (layout.message_rect.x, layout.message_rect.y))

    _render_activity_panel(pygame, screen, fonts, layout, activity)

    hint_text = _HINT_TEXT
    if update_check.update_available:
        hint_text = "Tab opens Update ROMCloud   " + hint_text
    hint = fonts["hint"].render(hint_text, True, _HINT_COLOR)
    screen.blit(hint, (layout.hint_rect.x, layout.hint_rect.y))

    pygame.display.flip()


def _update_banner_rect(layout: Layout) -> Rect:
    width = min(layout.header_rect.w // 2, max(260, layout.header_rect.w * 2 // 5))
    return Rect(
        layout.header_rect.right - width,
        layout.header_rect.y,
        width,
        layout.header_rect.h,
    )


def _render_activity_panel(  # noqa: ANN001
    pygame,
    screen,
    fonts: dict,
    layout: Layout,
    activity: ActivityLog,
    *,
    details: bool | None = None,
) -> None:
    rect = layout.activity_rect
    if rect is None:
        return
    pygame.draw.rect(screen, _CARD_BG, (rect.x, rect.y, rect.w, rect.h), border_radius=6)
    title = fonts["body"].render("Activity", True, _FG_COLOR)
    screen.blit(title, (rect.x + 12, rect.y + 10))
    line_h = layout.fonts.hint + 7
    top = rect.y + layout.fonts.body + 22
    rows = max(1, (rect.bottom - top - 10) // line_h)
    show_details = activity.details_expanded if details is None else details
    user_events = [
        event
        for event in activity.events
        if event.stage not in {"system_progress", "overall_progress"}
    ]
    offset = max(0, min(activity.scroll_offset, max(0, len(user_events) - rows)))
    end = len(user_events) - offset
    events = user_events[max(0, end - rows) : end]
    y = top
    max_chars = max(1, (rect.w - 24) // max(7, layout.fonts.hint // 2))
    for event in events:
        color = _ERROR_COLOR if event.status == "error" else (
            _SUCCESS_COLOR if event.status == "success" else _FG_COLOR
        )
        line = event.display_line
        text = fonts["hint"].render(line[:max_chars], True, color)
        screen.blit(text, (rect.x + 12, y))
        y += line_h
        if show_details and event.detail and y + line_h <= rect.bottom:
            detail = fonts["hint"].render(
                f"  {event.detail}"[:max_chars], True, _HINT_COLOR
            )
            screen.blit(detail, (rect.x + 12, y))
            y += line_h


def _render_controller_test(  # noqa: ANN001
    pygame,
    screen,
    fonts: dict,
    layout: Layout,
    input_manager: InputManager,
    controller_test: _ControllerTestScreenState,
) -> None:
    screen.fill(_BG_COLOR)

    title = fonts["title"].render("Setup Controller Mapping", True, _FG_COLOR)
    screen.blit(title, (layout.safe_area.x, layout.safe_area.y))

    snapshots = input_manager.controllers.snapshots()
    y = layout.safe_area.y + layout.fonts.title + 16
    line_h = layout.fonts.body + 6

    if not snapshots:
        text = fonts["body"].render("No controller detected.", True, _FG_COLOR)
        screen.blit(text, (layout.safe_area.x, y))
    else:
        snap = snapshots[0]
        info_lines = [
            f"Name: {snap.identity.name}",
            f"GUID: {snap.identity.guid or '(unavailable)'}",
            f"Recognized as game controller: {'yes' if snap.is_game_controller else 'no (raw/fallback mapping)'}",
            f"Custom mapping: {'yes' if snap.using_custom_mapping else 'no'}",
            f"Held direction: {snap.held_direction}",
            f"Last action: {snap.last_action.value if snap.last_action else '(none)'}",
            f"Stick: x={snap.axis_x:+.2f} y={snap.axis_y:+.2f}",
        ]
        for line in info_lines:
            text = fonts["body"].render(line, True, _FG_COLOR)
            screen.blit(text, (layout.safe_area.x, y))
            y += line_h

    y += line_h // 2
    for i, action in enumerate(_REMAPPABLE_ACTIONS):
        is_selected = i == controller_test.selected_index
        is_awaiting = controller_test.remap_instance_id is not None and is_selected
        color = _SELECTED_BG if is_selected else _CARD_BG
        suffix = "  (press a button...)" if is_awaiting else ""
        pygame.draw.rect(screen, color, (layout.safe_area.x, y, 260, line_h), border_radius=4)
        label = fonts["body"].render(f"{_CONTROLLER_ACTION_LABELS[action]}{suffix}", True, _FG_COLOR)
        screen.blit(label, (layout.safe_area.x + 8, y))
        y += line_h + 4

    hint = fonts["hint"].render(_CONTROLLER_TEST_HINT, True, _HINT_COLOR)
    screen.blit(hint, (layout.hint_rect.x, layout.hint_rect.y))

    pygame.display.flip()


def _render_operation(  # noqa: ANN001
    pygame,
    screen,
    fonts: dict,
    layout: Layout,
    operation: OperationScreenState,
    activity: ActivityLog,
    catalog_progress: CatalogRefreshProgress,
) -> None:
    """Renders the reusable long-running operation screen — a title, a
    state label, and a bounded/scrolled/wrapped view of the subprocess's
    captured output. Deliberately plain (no monospace terminal styling):
    this is a 10-foot/Steam Deck status view, not a terminal emulator."""
    screen.fill(_BG_COLOR)

    title = fonts["title"].render(operation.title, True, _FG_COLOR)
    screen.blit(title, (layout.header_rect.x, layout.header_rect.y))

    state = operation.state
    state_color = _SUCCESS_COLOR if state == OperationState.SUCCEEDED else (
        _ERROR_COLOR if state == OperationState.FAILED else _FG_COLOR
    )
    state_y = layout.navigation_rect.y
    state_label = fonts["body"].render(_STATE_LABELS[state], True, state_color)
    screen.blit(state_label, (layout.safe_area.x, state_y))

    output_top = state_y + layout.fonts.body + 12
    output_bottom = layout.navigation_rect.bottom - 8
    line_h = layout.fonts.body + 4
    viewport_rows = max(1, (output_bottom - output_top) // line_h)

    if operation.title == "Refresh Catalog" and catalog_progress.systems:
        _render_catalog_progress(
            pygame,
            screen,
            fonts,
            layout.navigation_rect,
            catalog_progress,
            top=output_top,
        )
    elif operation.title in ("Direct", "Cached Storage", "Offline"):
        _render_mode_progress(
            pygame,
            screen,
            fonts,
            layout.navigation_rect,
            activity,
            top=output_top,
        )
    else:
        max_chars = max(1, layout.navigation_rect.w // 10)
        lines = wrap_lines(
            display_lines(operation.runner, details=operation.details_expanded), max_chars
        )
        start, end = visible_window(len(lines), viewport_rows, operation.scroll_offset)

        y = output_top
        for line in lines[start:end]:
            color = _ERROR_COLOR if line.startswith("! ") else _FG_COLOR
            text = fonts["body"].render(line, True, color)
            screen.blit(text, (layout.navigation_rect.x, y))
            y += line_h

    _render_activity_panel(
        pygame,
        screen,
        fonts,
        layout,
        activity,
        details=operation.details_expanded,
    )

    detail_hint = "   Left/Right technical details"
    hint_text = (
        _OPERATION_HINT_FINISHED if operation.is_finished else _OPERATION_HINT_RUNNING
    ) + detail_hint
    hint = fonts["hint"].render(hint_text, True, _HINT_COLOR)
    screen.blit(hint, (layout.hint_rect.x, layout.hint_rect.y))

    pygame.display.flip()


def _draw_progress_bar(  # noqa: ANN001
    pygame,
    screen,
    rect: Rect,
    fraction: float | None,
    *,
    failed: bool = False,
) -> None:
    pygame.draw.rect(screen, _CARD_BG, (rect.x, rect.y, rect.w, rect.h), border_radius=4)
    if fraction is None:
        # Animate a short segment for work whose duration cannot be measured,
        # without claiming a fabricated percentage.
        segment_w = max(8, rect.w // 5)
        ticks = 0
        pygame_time = getattr(pygame, "time", None)
        get_ticks = getattr(pygame_time, "get_ticks", None)
        if callable(get_ticks):
            ticks = int(get_ticks())
        travel = max(1, rect.w - segment_w)
        offset = int((ticks / 900.0 % 1.0) * travel)
        pygame.draw.rect(
            screen,
            _WARNING_COLOR,
            (rect.x + offset, rect.y, segment_w, rect.h),
            border_radius=4,
        )
        return
    fill_w = int(rect.w * max(0.0, min(1.0, fraction)))
    if fill_w:
        pygame.draw.rect(
            screen,
            _ERROR_COLOR if failed else _SUCCESS_COLOR,
            (rect.x, rect.y, fill_w, rect.h),
            border_radius=4,
        )


def _render_mode_progress(  # noqa: ANN001
    pygame,
    screen,
    fonts: dict,
    area: Rect,
    activity: ActivityLog,
    *,
    top: int,
) -> None:
    """Render mode phases with the same truthful green/yellow bar language."""
    event = next(
        (
            item
            for item in reversed(activity.events)
            if item.operation == "operating_mode"
        ),
        None,
    )
    if event is None:
        message = "Preparing mode"
        fraction = None
        failed = False
    else:
        suffix = ""
        if event.current is not None and event.total is not None:
            suffix = f"  {event.current:,} / {event.total:,}"
        message = f"{event.message}{suffix}"
        if event.current is not None and event.total is not None:
            fraction = 1.0 if event.total == 0 else event.current / event.total
        elif event.status == "success" and event.stage == "complete":
            fraction = 1.0
        else:
            fraction = None
        failed = event.status == "error"

    label = fonts["body"].render(message, True, _ERROR_COLOR if failed else _FG_COLOR)
    screen.blit(label, (area.x, top))
    bar_h = max(8, fonts["hint"].get_height() // 2)
    bar = Rect(area.x, top + fonts["body"].get_height() + 8, area.w, bar_h)
    _draw_progress_bar(pygame, screen, bar, fraction, failed=failed)


def _render_catalog_progress(  # noqa: ANN001
    pygame,
    screen,
    fonts: dict,
    area: Rect,
    progress: CatalogRefreshProgress,
    *,
    top: int,
) -> None:
    label = fonts["hint"].render("Overall", True, _FG_COLOR)
    screen.blit(label, (area.x, top))
    bar_h = max(8, fonts["hint"].get_height() // 2)
    overall_bar = Rect(area.x, top + fonts["hint"].get_height() + 3, area.w, bar_h)
    _draw_progress_bar(pygame, screen, overall_bar, progress.overall_fraction)
    y = overall_bar.bottom + 14
    row_h = max(44, fonts["body"].get_height() + bar_h + 10)
    rows = max(1, (area.bottom - y) // row_h)
    for row in progress.visible_systems(rows):
        status = "Failed" if row.status == "error" else (
            "Done" if row.status == "success" else "Waiting" if row.status == "queued" else "Running"
        )
        suffix = ""
        if row.determinate and row.current is not None and row.total is not None:
            suffix = f" {row.current}/{row.total}"
        line = fonts["hint"].render(
            f"{row.system}   {status}{suffix}",
            True,
            _ERROR_COLOR if row.status == "error" else _FG_COLOR,
        )
        screen.blit(line, (area.x, y))
        bar = Rect(area.x, y + fonts["hint"].get_height() + 2, area.w, bar_h)
        fraction = 1.0 if row.status == "success" and row.total == 0 else row.fraction
        _draw_progress_bar(
            pygame,
            screen,
            bar,
            fraction,
            failed=row.status == "error",
        )
        y += row_h


def _savesync_body_lines(savesync_screen: SaveSyncScreenState) -> list[str]:
    """Pure text content for the current SaveSync step — unit-tested
    directly, independent of rendering."""
    step = savesync_screen.step
    status = savesync_screen.status

    if step == DASHBOARD:
        remote_label = (
            "Checking…"
            if savesync_screen.remote_availability == REMOTE_CHECKING
            else "Available"
            if savesync_screen.remote_availability == REMOTE_AVAILABLE
            else "Unavailable"
        )
        if "xbox_enabled" in status:
            xbox_label = "enabled" if status["xbox_enabled"] else "disabled"
        else:
            xbox_label = "loading…" if savesync_screen.status_loading else "unknown"
        last_upload = status.get("last_upload")
        last_download = status.get("last_download")
        reconcile = status.get("last_reconcile")
        empty_history = (
            "loading…"
            if savesync_screen.status_loading
            else "unavailable"
            if savesync_screen.status_error
            else "never"
        )
        lines = [
            f"Remote: {remote_label}",
            f"Original Xbox: {xbox_label}",
            "Eligible scope: all supported save layouts",
            (
                f"Last reconciliation: {reconcile.get('conflicts', 0)} conflict(s) preserved"
                if reconcile
                else f"Last reconciliation: {empty_history}"
            ),
            f"Last upload: {last_upload['timestamp'] if last_upload else empty_history}",
            f"Last download: {last_download['timestamp'] if last_download else empty_history}",
        ]
        if status.get("sync_status"):
            lines.insert(
                3,
                "Save state: "
                f"{status['sync_status']}; "
                f"{int(status.get('active_conflicts', 0))} conflict(s) require attention",
            )
        if status.get("remote_configured") is False:
            lines.append("ROMCloud data storage is not configured.")
        if savesync_screen.status_loading:
            lines.append("Local status: Loading…")
        if savesync_screen.status_error:
            lines.append(f"Local status unavailable: {savesync_screen.status_error}")
        if savesync_screen.error:
            lines.append(f"SaveSync: {savesync_screen.error}")
        return [*lines, "", *savesync_screen.dashboard_items]
    if step == PREVIEWING:
        if savesync_screen.result_mode == "quick-sync":
            return ["Running Quick Sync..."]
        if savesync_screen.result_mode == "full-sync":
            return ["Running Full Sync..."]
        return ["Comparing local and remote saves..."]
    if step == PREVIEW:
        summary = savesync_screen.preview_summary
        return [
            "AUTHORITATIVE REPLACEMENT",
            f"Added:     {summary.get('added', 0)}",
            f"Changed:   {summary.get('changed', 0)}",
            f"Removed:   {summary.get('removed', 0)}",
            f"Conflicts: {summary.get('conflicts', 0)}",
            f"Unchanged: {summary.get('unchanged', 0)}",
            f"Transfer:  {_save_size(summary.get('transfer_bytes', 0))}",
            "",
            (
                "Remote eligible data will be replaced by this device."
                if savesync_screen.direction == "upload"
                else "This device's eligible data will be replaced by remote data."
            ),
            "Press Confirm and hold for 3 seconds to apply, Back to cancel.",
        ]
    if step == CONFIRMING:
        return ["Keep holding Confirm to continue.", "Release to cancel."]
    if step == COMMITTING:
        return ["Applying changes..."]
    if step == RESULT:
        if savesync_screen.error:
            return [f"Failed: {savesync_screen.error}", "Press Confirm to return."]
        record = savesync_screen.result
        if savesync_screen.result_mode == "quick-sync":
            status = str(record.get("status", ""))
            if status == "requires-full-sync":
                reason = str(record.get("reason", "")).strip()
                detail = f" ({reason})" if reason else ""
                return [
                    f"Quick Sync requires Full Sync first{detail}.",
                    "Press Confirm to return.",
                ]
            if status == "unchanged":
                return [
                    "Quick Sync: no remote SaveSync changes detected.",
                    "Press Confirm to return.",
                ]
            if status == "deferred":
                return [
                    "Quick Sync deferred: active gameplay data is protected.",
                    "Press Confirm to return.",
                ]
            if status == "reconciled":
                generation = record.get("cursor_after", "unknown")
                processed = int(record.get("processed_entries", 0))
                return [
                    f"Quick Sync complete through generation {generation}.",
                    f"Journal entries processed: {processed}",
                    "Press Confirm to return.",
                ]
            return ["Quick Sync complete.", "Press Confirm to return."]
        if savesync_screen.result_mode == "full-sync":
            report = record.get("report", {})
            uploaded = int(report.get("uploaded", 0))
            downloaded = int(report.get("downloaded", 0))
            conflicts = int(report.get("conflicts", 0))
            cursor = record.get("quick_sync_cursor_generation", "unknown")
            return [
                "Full Sync complete.",
                f"Uploaded: {uploaded}  Downloaded: {downloaded}  Conflicts: {conflicts}",
                f"Quick Sync cursor: {cursor}",
                "Press Confirm to return.",
            ]
        return [
            f"Done. {record.get('artifact_count', 0)} artifact(s).",
            "Press Confirm to return.",
        ]
    if step == SETTINGS:
        if "xbox_enabled" in status:
            xbox_label = "enabled" if status["xbox_enabled"] else "disabled"
        else:
            xbox_label = "loading…" if savesync_screen.status_loading else "unknown"
        lines = [
            f"Original Xbox: {xbox_label}",
            "The virtual drive is explicit opt-in; disabling restores the safe default.",
        ]
        if savesync_screen.error:
            lines.append(f"SaveSync: {savesync_screen.error}")
        return [*lines, "", *savesync_screen.settings_items]
    if step == APPLYING_SETTINGS:
        return ["Applying setting..."]
    return []


def _save_size(num_bytes: int) -> str:
    value = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{int(value)} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} TB"


def _library_sync_body_lines(screen: LibrarySyncScreenState) -> list[str]:
    if screen.step == LIBRARY_PREFLIGHTING:
        return ["Inspecting source game lists…", "No media is being hashed or copied yet."]
    if screen.step == LIBRARY_PREFLIGHT:
        preview = screen.preview
        systems = preview.get("systems", [])
        systems_text = ", ".join(str(item) for item in systems) or "none"
        lines = [
            screen.sync_label,
            f"Eligible catalog games: {int(preview.get('games_eligible', 0)):,}",
            f"Systems ({len(systems)}): {systems_text}",
            (
                f"Game lists: {int(preview.get('gamelist_files', 0)):,} "
                f"({_save_size(int(preview.get('gamelist_bytes', 0)))})"
            ),
            f"Artwork references: {int(preview.get('artwork_references', 0)):,}",
            f"Video references: {int(preview.get('video_references', 0)):,}",
            f"Other media references: {int(preview.get('other_media_references', 0)):,}",
            "Transfer bytes: counted only as files are copied",
            str(preview.get("duration_note", "Duration depends on library size and storage speed.")),
            "",
            "Source game lists and source media will not be modified.",
        ]
        if screen.is_full:
            lines.extend(
                [
                    "Verifies and repairs existing media payloads.",
                    "This may take significantly longer on large libraries.",
                    "Press Confirm, then hold for 3 seconds to start. Back cancels.",
                ]
            )
        else:
            lines.extend(
                [
                    "Copies missing media and skips existing payloads.",
                    "Press Confirm to start Quick Sync. Back cancels.",
                ]
            )
        return lines
    if screen.step == LIBRARY_CONFIRMING:
        return [
            "Hold to Start Full Sync",
            "Keep holding Confirm for 3 seconds. Release or press Back to cancel.",
        ]
    if screen.step == LIBRARY_IMPORTING:
        event = screen.latest_progress
        lines = [f"Running {screen.sync_label}…"]
        if event is not None:
            lines.append(event.message)
            if event.detail:
                lines.append(event.detail)
        lines.extend(["", "Back cancels safely; the operation can be retried."])
        return lines
    if screen.step == LIBRARY_RESULT:
        if screen.error:
            return [
                ("Canceled: " if screen.cancelled else "Failed: ") + screen.error,
                "Press Confirm to retry the preflight, or Back to return.",
            ]
        return [
            f"{screen.sync_label} complete.",
            f"Metadata added: {int(screen.result.get('metadata_added', 0)):,}",
            f"Metadata updated: {int(screen.result.get('metadata_updated', 0)):,}",
            f"Media examined: {int(screen.result.get('media_examined', 0)):,}",
            f"Media skipped: {int(screen.result.get('media_skipped', 0)):,}",
            f"Full-file hashes: {int(screen.result.get('media_hashed', 0)):,}",
            f"Bytes fully hashed: {_save_size(int(screen.result.get('media_bytes_hashed', 0)))}",
            f"Media copied: {int(screen.result.get('media_transferred', 0)):,}",
            f"Actual bytes transferred: {_save_size(int(screen.result.get('media_bytes_transferred', 0)))}",
            f"Games rendered: {int(screen.result.get('rendered', 0)):,}",
            "Press Confirm or Back to return.",
        ]
    return []


def _render_library_sync(  # noqa: ANN001
    pygame,
    screen_surface,
    fonts: dict,
    layout: Layout,
    state: LibrarySyncScreenState,
    activity: ActivityLog,
) -> None:
    screen_surface.fill(_BG_COLOR)
    title = fonts["title"].render(
        f"Library {state.sync_label}", True, _FG_COLOR
    )
    screen_surface.blit(title, (layout.header_rect.x, layout.header_rect.y))

    y = layout.navigation_rect.y
    line_h = layout.fonts.body + 6
    max_chars = max(20, layout.navigation_rect.w // 11)
    for line in wrap_lines(_library_sync_body_lines(state), max_chars):
        text = fonts["body"].render(
            line,
            True,
            _ERROR_COLOR if state.step == LIBRARY_RESULT and state.error else _FG_COLOR,
        )
        screen_surface.blit(text, (layout.navigation_rect.x, y))
        y += line_h

    if state.step == LIBRARY_CONFIRMING:
        bar = Rect(
            layout.navigation_rect.x,
            y + line_h // 2,
            min(layout.navigation_rect.w, 560),
            max(8, layout.fonts.body // 2),
        )
        _draw_progress_bar(pygame, screen_surface, bar, state.confirm.progress)
    elif state.step in (LIBRARY_PREFLIGHTING, LIBRARY_IMPORTING):
        bar = Rect(
            layout.navigation_rect.x,
            y + line_h // 2,
            min(layout.navigation_rect.w, 560),
            max(8, layout.fonts.body // 2),
        )
        _draw_progress_bar(
            pygame, screen_surface, bar, state.progress_fraction
        )

    _render_activity_panel(pygame, screen_surface, fonts, layout, activity)
    hint = fonts["hint"].render(_HINT_TEXT, True, _HINT_COLOR)
    screen_surface.blit(hint, (layout.hint_rect.x, layout.hint_rect.y))
    pygame.display.flip()


def _render_savesync(  # noqa: ANN001
    pygame,
    screen,
    fonts: dict,
    layout: Layout,
    savesync_screen: SaveSyncScreenState,
    activity: ActivityLog,
) -> None:
    if savesync_screen.step == CONFLICTS and savesync_screen.resolver is not None:
        render_conflict_resolver(
            pygame,
            screen,
            fonts,
            layout,
            savesync_screen.resolver,
            conflict_action_rects(layout),
            colors={
                "bg": _BG_COLOR,
                "card": _CARD_BG,
                "error": _ERROR_COLOR,
                "fg": _FG_COLOR,
                "hint": _HINT_COLOR,
                "selected": _SELECTED_BG,
                "success": _SUCCESS_COLOR,
            },
            draw_progress=_draw_progress_bar,
        )
        return

    screen.fill(_BG_COLOR)

    title = fonts["title"].render("SaveSync", True, _FG_COLOR)
    screen.blit(title, (layout.header_rect.x, layout.header_rect.y))

    y = layout.navigation_rect.y
    line_h = layout.fonts.body + 6
    for i, line in enumerate(_savesync_body_lines(savesync_screen)):
        is_selected_item = (
            savesync_screen.step == DASHBOARD
            and line in savesync_screen.dashboard_items
            and savesync_screen.dashboard_items.index(line)
            == savesync_screen.selected_index
        )
        is_selected_item = is_selected_item or (
            savesync_screen.step == SETTINGS
            and line in savesync_screen.settings_items
            and savesync_screen.settings_items.index(line)
            == savesync_screen.settings_selected_index
        )
        color = _SELECTED_BG if is_selected_item else _FG_COLOR
        text = fonts["body"].render(line, True, color)
        screen.blit(text, (layout.navigation_rect.x, y))
        y += line_h

    if savesync_screen.step == CONFIRMING:
        bar_x = layout.navigation_rect.x
        bar_y = y + line_h // 2
        bar_w = max(1, min(layout.navigation_rect.w, 560))
        bar_h = max(8, layout.fonts.body // 2)
        pygame.draw.rect(screen, _CARD_BG, (bar_x, bar_y, bar_w, bar_h), border_radius=4)
        fill_w = int(bar_w * savesync_screen.confirm.progress)
        if fill_w > 0:
            pygame.draw.rect(
                screen,
                _SUCCESS_COLOR,
                (bar_x, bar_y, fill_w, bar_h),
                border_radius=4,
            )

    _render_activity_panel(pygame, screen, fonts, layout, activity)

    hint = fonts["hint"].render(_HINT_TEXT, True, _HINT_COLOR)
    screen.blit(hint, (layout.hint_rect.x, layout.hint_rect.y))

    pygame.display.flip()


def _wizard_content_rect(layout: Layout) -> Rect:
    return compute_wizard_regions(layout).content


_WIZARD_CONTINUE_LABELS = {
    "Start Setup",
    "Resume / Repair Setup",
    "Review / Change Setup",
    "Continue",
    "Finish",
    "Retry",
}


def _wizard_option_rows(
    layout: Layout, wizard: WizardState
) -> list[tuple[int, str, Rect]]:
    """Visible option rows, keeping the footer action statically anchored."""
    options = wizard.options
    if not options:
        return []
    regions = compute_wizard_regions(layout, osk_visible=False)
    continuation_index = (
        len(options) - 1 if options[-1] in _WIZARD_CONTINUE_LABELS else None
    )
    ordinary_count = len(options) - (1 if continuation_index is not None else 0)
    content = regions.content
    controls_top = content.y + min(content.h // 3, layout.fonts.body * 4)
    controls = Rect(
        content.x,
        controls_top,
        content.w,
        max(1, content.bottom - controls_top),
    )
    gap = max(6, int(controls.h * 0.012))
    max_rows = max(1, (controls.h + gap) // (44 + gap))
    start = 0
    if ordinary_count > max_rows and wizard.selected_index < ordinary_count:
        start = max(
            0,
            min(wizard.selected_index - max_rows + 1, ordinary_count - max_rows),
        )
    ordinary_indices = list(range(start, min(ordinary_count, start + max_rows)))
    rects = compute_vertical_control_rects(controls, len(ordinary_indices))
    rows = [
        (actual_index, options[actual_index], rect)
        for actual_index, rect in zip(ordinary_indices, rects)
    ]
    if continuation_index is not None:
        rows.append(
            (continuation_index, options[continuation_index], regions.continue_button)
        )
    return rows


def _wizard_rects(layout: Layout, wizard: WizardState) -> list[Rect]:
    regions = compute_wizard_regions(layout, osk_visible=wizard.osk_visible)
    if wizard.osk is not None and wizard.osk_visible:
        assert regions.osk is not None
        return compute_osk_layout(regions.osk, wizard.osk.keys)
    # Pointer-only Back target. Controller/keyboard Back remains semantic and
    # does not enter the ordinary option focus order.
    return [*(row[2] for row in _wizard_option_rows(layout, wizard)), regions.back_button]


def _wizard_body_lines(wizard: WizardState) -> list[str]:
    context = [*wizard.context_lines]
    if wizard.notice:
        context.append(wizard.notice)
    if wizard.step == WizardStep.WELCOME:
        return [*context, *wizard.issues]
    if wizard.step in (
        WizardStep.SOURCE_BROWSE,
        WizardStep.REMOTE_BROWSE,
        WizardStep.LOCAL_BROWSE,
    ):
        location = wizard.browser_path or "/ (share root)"
        files = [
            str(entry.get("name", ""))
            for entry in wizard.browser_entries
            if not entry.get("is_directory")
        ]
        lines = [*context, f"Current folder: {location}"]
        if files:
            preview = ", ".join(files[:4])
            suffix = "…" if len(files) > 4 else ""
            lines.append(f"Files here: {preview}{suffix}")
        return lines
    if wizard.step == WizardStep.SYSTEMS:
        validation = []
        if wizard.source_validation.get("connected"):
            validation.append("\u2713 Connected")
        if wizard.source_validation.get("read_verified"):
            validation.append("\u2713 Read access verified")
        systems = (
            ["No recognized Batocera system folders were found."]
            if not wizard.systems
            else [f"{len(wizard.systems)} systems: {', '.join(wizard.systems)}"]
        )
        return [*context, *validation, *systems]
    if wizard.step == WizardStep.GAME_ACCESS:
        return [
            *context,
            "You can switch among Direct, Cached Storage, and Offline later from the main menu.",
        ]
    if wizard.step == WizardStep.LIBRARY_SYNC:
        return [
            *context,
            "Existing source/NAS gamelist.xml files may be read to initialize metadata.",
        ]
    if wizard.step == WizardStep.REVIEW:
        lines = [
            *context,
            (
                f"ROM library: //{wizard.server}/{wizard.share}"
                f"{f'/{wizard.source_remote_path}' if wizard.source_remote_path else ''} [Read only]"
                if wizard.source_type == "smb"
                else f"ROM library: {wizard.rom_root} [Local]"
            ),
            "\u2713 Connected  \u2713 Read access verified",
            f"Systems: {len(wizard.systems)}",
            "Game access: " + (
                "Direct (source required while playing)"
                if wizard.game_access_mode == "direct_nas"
                else "Cached Storage"
            ),
            (
                f"ROMCloud data: //{wizard.remote_server}/{wizard.remote_share} [Read/write]"
                if wizard.remote_data_type == "smb"
                else f"ROMCloud data: {wizard.remote_data_root}"
                if wizard.remote_data_type == "local"
                else "ROMCloud data: not configured (SaveSync unavailable)"
            ),
            f"Library Sync: {'enabled' if wizard.library_sync_enabled else 'disabled'}",
        ]
        if wizard.game_access_mode == "smart_cache":
            lines.append(f"Cache: {wizard.cache_root} ({wizard.max_size_gb:g} GB max)")
        if wizard.remote_data_type == "smb" and wizard.remote_validation:
            lines.insert(-1, "\u2713 Connected  \u2713 Read access verified")
            lines.insert(-1, "Write and cleanup will be verified before setup completes.")
        return lines
    if wizard.step == WizardStep.DONE:
        lines = [
            *context,
            (
                f"ROM library: //{wizard.applied_summary.get('server', wizard.server)}/{wizard.applied_summary.get('share', wizard.share)} [Read only]"
                if wizard.source_type == "smb"
                else f"ROM library: {wizard.rom_root} [Local]"
            ),
            f"Detected systems: {wizard.applied_summary.get('system_count', len(wizard.systems))}",
        ]
        if wizard.applied_summary.get("source_validation", {}).get("connected"):
            lines.extend(["\u2713 Connected", "\u2713 Read access verified"])
        if wizard.remote_data_type == "smb":
            lines.append(
                f"ROMCloud data: //{wizard.remote_server}/{wizard.remote_share} [Read/write]"
            )
        else:
            lines.append(
                f"ROMCloud data: {wizard.applied_summary.get('remote_data_type', wizard.remote_data_type)}"
            )
        remote_validation = wizard.applied_summary.get("remote_data_validation") or {}
        if remote_validation:
            lines.extend(
                [
                    "\u2713 Connected",
                    "\u2713 Read access verified",
                    "\u2713 Write access verified",
                    "\u2713 Cleanup verified",
                ]
            )
        if wizard.applied_summary.get("save_sync_initialized"):
            lines.append("\u2713 Initial Full Sync complete — Quick Sync ready")
        if wizard.game_access_mode == "smart_cache":
            lines.append(
                f"Cache size: {wizard.applied_summary.get('max_size_gb', wizard.max_size_gb):g} GB"
            )
        else:
            lines.append("Direct: the source must remain reachable while playing.")
        if wizard.library_sync_enabled:
            lines.append(
                "Optional metadata was not synchronized. Use Library > Sync Missing Media when ready."
            )
        return lines
    return context


def _render_wizard_progress(  # noqa: ANN001
    pygame,
    screen,
    fonts: dict,
    layout: Layout,
    wizard: WizardState,
) -> None:
    progress = wizard.progress
    if progress is None:
        return
    area = layout.message_rect
    max_chars = max(1, area.w // max(7, layout.fonts.hint // 2))
    label = fonts["hint"].render(progress.label[:max_chars], True, _FG_COLOR)
    screen.blit(label, (area.x, area.y))
    bar_h = max(8, fonts["hint"].get_height() // 2)
    bar = Rect(area.x, area.y + fonts["hint"].get_height() + 5, area.w, bar_h)
    _draw_progress_bar(
        pygame,
        screen,
        bar,
        progress.fraction,
        failed=progress.status == "error",
    )


def _render_wizard(  # noqa: ANN001
    pygame,
    screen,
    fonts: dict,
    layout: Layout,
    wizard: WizardState,
    activity: ActivityLog,
) -> None:
    screen.fill(_BG_COLOR)
    title = fonts["title"].render(wizard.title, True, _FG_COLOR)
    screen.blit(title, (layout.safe_area.x, layout.safe_area.y))

    progress = fonts["hint"].render(f"Setup {wizard.step_number} of {len(tuple(WizardStep))}", True, _HINT_COLOR)
    progress_rect = progress.get_rect(topright=(layout.safe_area.x + layout.safe_area.w, layout.safe_area.y))
    screen.blit(progress, progress_rect)

    regions = compute_wizard_regions(layout, osk_visible=wizard.osk_visible)
    content = regions.content
    if wizard.osk is not None and wizard.osk_visible:
        assert regions.osk is not None
        context_lines = _wizard_body_lines(wizard)[:2]
        context_y = content.y
        for line in context_lines:
            context = fonts["hint"].render(line, True, _HINT_COLOR)
            screen.blit(context, (content.x, context_y))
            context_y += layout.fonts.hint + 4
        text_rect = compute_osk_text_rect(regions.osk)
        pygame.draw.rect(screen, _CARD_BG, (text_rect.x, text_rect.y, text_rect.w, text_rect.h), border_radius=6)
        display_value = wizard.osk.displayed_text or " "
        max_chars = max(1, text_rect.w // max(8, layout.fonts.body // 2))
        value = fonts["body"].render(display_value[-max_chars:], True, _FG_COLOR)
        screen.blit(value, (text_rect.x + 12, text_rect.y + max(4, (text_rect.h - value.get_height()) // 2)))
        for index, (key, rect) in enumerate(zip(wizard.osk.keys, _wizard_rects(layout, wizard))):
            color = _SELECTED_BG if index == wizard.osk.selected_index else _CARD_BG
            pygame.draw.rect(screen, color, (rect.x, rect.y, rect.w, rect.h), border_radius=5)
            label = fonts["body"].render(wizard.osk.key_label(key), True, _FG_COLOR)
            screen.blit(label, label.get_rect(center=rect.center))
    else:
        y = content.y
        line_h = layout.fonts.body + 7
        max_chars = max(1, content.w // max(8, layout.fonts.body // 2))
        body_lines = _wizard_body_lines(wizard)
        if wizard.osk is not None:
            body_lines.extend(
                ["", f"Current value: {wizard.osk.displayed_text}", "Start/options controller button opens the on-screen keyboard"]
            )
        for line in body_lines:
            for wrapped in wrap_lines([line], max_chars):
                text = fonts["body"].render(wrapped, True, _FG_COLOR)
                screen.blit(text, (content.x, y))
                y += line_h

        for index, label_text, rect in _wizard_option_rows(layout, wizard):
            color = _SELECTED_BG if index == wizard.selected_index else _CARD_BG
            pygame.draw.rect(screen, color, (rect.x, rect.y, rect.w, rect.h), border_radius=6)
            max_label_chars = max(1, rect.w // max(8, layout.fonts.body // 2))
            label = fonts["body"].render(label_text[:max_label_chars], True, _FG_COLOR)
            screen.blit(label, label.get_rect(center=rect.center))

    _render_activity_panel(
        pygame,
        screen,
        fonts,
        layout,
        activity,
        details=wizard.show_details,
    )

    _render_wizard_progress(pygame, screen, fonts, layout, wizard)

    if wizard.error and wizard.progress is None:
        max_chars = max(1, layout.message_rect.w // max(8, layout.fonts.body // 2))
        error = fonts["body"].render(wizard.error[:max_chars], True, _ERROR_COLOR)
        screen.blit(error, (layout.message_rect.x, layout.message_rect.y))

    pygame.draw.rect(
        screen,
        _CARD_BG,
        (
            regions.back_button.x,
            regions.back_button.y,
            regions.back_button.w,
            regions.back_button.h,
        ),
        border_radius=5,
    )
    back_label = fonts["hint"].render("Back", True, _FG_COLOR)
    screen.blit(back_label, back_label.get_rect(center=regions.back_button.center))
    continue_enabled = bool(
        wizard.options
        and wizard.options[-1] in _WIZARD_CONTINUE_LABELS
    )
    pygame.draw.rect(
        screen,
        _SELECTED_BG if continue_enabled else _CARD_BG,
        (
            regions.continue_button.x,
            regions.continue_button.y,
            regions.continue_button.w,
            regions.continue_button.h,
        ),
        border_radius=5,
    )
    continue_label = fonts["hint"].render(
        "Continue", True, _FG_COLOR if continue_enabled else _HINT_COLOR
    )
    screen.blit(
        continue_label,
        continue_label.get_rect(center=regions.continue_button.center),
    )
    pygame.display.flip()
