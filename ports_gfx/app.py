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

import sys
from typing import Optional

from ports_gfx.actions import ACTION_DIRECTIONS, Action
from ports_gfx.client import BackendResult, call_backend
from ports_gfx.input_debug import InputDebugLogger
from ports_gfx.input_manager import InputEvent, InputManager
from ports_gfx.layout import Layout, Rect, compute_card_rects, compute_layout, find_next_focus_index
from ports_gfx.menu import CONTROLLER_TEST_ACTION, EXIT_ACTION, MenuItem, MenuState
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
from ports_gfx.savesync_screen import (
    APPLYING_SETTINGS,
    COMMITTING,
    CONFIRMING,
    DASHBOARD,
    DASHBOARD_ITEMS,
    PREVIEW,
    PREVIEWING,
    RESULT,
    SETTINGS,
    SaveSyncScreenState,
)
from ports_gfx.wizard import WizardState, WizardStep

SAVESYNC_ACTION = "savesync"
"""Sentinel the UI layer interprets as "switch to the SaveSync screen"
rather than dispatching to the backend — SaveSync is a first-class
top-level menu entry, not a Settings submenu."""

MENU_ITEMS: tuple[MenuItem, ...] = (
    MenuItem("Catalog Status", "status"),
    MenuItem("Refresh Catalog", "refresh"),
    MenuItem("Health Check", "healthcheck"),
    MenuItem("Cache Status", "cache-status"),
    MenuItem("SaveSync", SAVESYNC_ACTION),
    MenuItem("Check for Updates", "update-check"),
    MenuItem("Controller Test", CONTROLLER_TEST_ACTION),
    MenuItem("Exit", EXIT_ACTION),
)

# Actions dispatched through the reusable long-running operation screen
# (see operation_screen.py) instead of a quick blocking uidata JSON call.
# Reusing this screen for a later action (update, repair, diagnostics,
# mount/reconnect, sync) is just adding an entry here — never a change to
# the screen itself. Each argv is an existing, already-safe CLI entry
# point (never a new backend command).
_OPERATIONS: dict[str, OperationSpec] = {
    "refresh": OperationSpec(title="Refresh Catalog", args=("refresh",)),
    "update-check": OperationSpec(title="Check for Updates", args=("update", "--check")),
}

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
_OPERATION_HINT_RUNNING = "Please wait — operation is running"
_OPERATION_HINT_FINISHED = "A/Enter/Esc/Tap return to menu   Up/Down scroll"

# Fallback used only if the display driver reports a nonsensical size
# (e.g. a headless/dummy driver) — never a "design resolution" the real
# layout is scaled against.
_FALLBACK_SIZE = (1280, 720)
_MIN_SANE_DIMENSION = 240

_TARGET_FPS = 30
_HINT_TEXT = "Up/Down/Left/Right select   Enter confirm   Esc exit"
_WIZARD_HINT = ""


def format_result(action: str, result: BackendResult) -> str:
    """Render a :class:`BackendResult` as a single display line.

    Pure function — no pygame — so it is fully unit-tested.
    """
    if not result.ok:
        return f"Error: {result.error}"
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
    runner = OperationRunner(argv) if popen is None else OperationRunner(argv, popen=popen)
    runner.start()
    return OperationScreenState(title=spec.title, runner=runner)


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


def initial_screen_for_status(status: BackendResult) -> str:
    """Choose startup routing from the backend's structural setup state."""
    if status.ok and status.data.get("state") == "configured":
        return "menu"
    return "wizard"


def run_app(romcloud_bin: str) -> int:
    """Entry point. Returns a process exit code; never raises.

    Any failure (pygame missing, display init failure, unexpected crash)
    is caught here and reported to stderr with a non-zero exit code —
    Batocera's Ports launcher must always get control back cleanly.
    """
    try:
        import pygame
    except ImportError as exc:
        print(
            f"error: pygame is not available under this Python interpreter: {exc}",
            file=sys.stderr,
        )
        return 1

    try:
        return _run(pygame, romcloud_bin)
    except Exception as exc:  # noqa: BLE001 — must never crash Batocera's Ports flow
        print(f"error: ports UI crashed: {exc}", file=sys.stderr)
        return 1


def _detect_screen_size(pygame) -> tuple[int, int]:  # noqa: ANN001
    """Current display resolution, defensively clamped.

    Some drivers (e.g. a headless/dummy SDL video driver) can report a
    zero or nonsensical size; fall back to a safe default rather than
    ever computing a layout against a degenerate (0, 0) screen.
    """
    try:
        info = pygame.display.Info()
        w, h = info.current_w, info.current_h
    except Exception:  # noqa: BLE001
        w, h = _FALLBACK_SIZE

    if w < _MIN_SANE_DIMENSION or h < _MIN_SANE_DIMENSION:
        return _FALLBACK_SIZE
    return w, h


def _open_display(pygame, screen_w: int, screen_h: int):  # noqa: ANN001
    """Open a full-screen window at *(screen_w, screen_h)*, falling back
    to a windowed surface of the same size if full-screen mode itself
    fails to initialize (e.g. an unusual/unsupported SDL video driver)."""
    try:
        return pygame.display.set_mode((screen_w, screen_h), pygame.FULLSCREEN)
    except Exception:  # noqa: BLE001
        return pygame.display.set_mode((screen_w, screen_h))


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
)
_CONTROLLER_TEST_HINT = "D-pad/stick select   A/Enter remap   B/Esc back"


class _ControllerTestScreenState:
    """Local UI state for the Controller Test/diagnostics screen — which
    action slot is focused, and whether a remap capture is in progress."""

    def __init__(self) -> None:
        self.selected_index = 0
        self.remap_instance_id: Optional[int] = None

    def select(self, index: int) -> None:
        self.selected_index = max(0, min(index, len(_REMAPPABLE_ACTIONS) - 1))


def _run(pygame, romcloud_bin: str) -> int:  # noqa: ANN001 — `pygame` module, imported lazily
    pygame.init()
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

    try:
        screen_w, screen_h = _detect_screen_size(pygame)
        screen = _open_display(pygame, screen_w, screen_h)
        pygame.display.set_caption("ROMCloud")

        state = MenuState(list(MENU_ITEMS))
        setup_status = call_backend(romcloud_bin, "setup-status")
        wizard: WizardState | None = None
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
        operation_screen: Optional[OperationScreenState] = None
        savesync_screen: Optional[SaveSyncScreenState] = None
        current_screen = "wizard" if wizard is not None else "menu"
        message: Optional[str] = None
        message_kind = "info"

        running = True
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

                if input_debug is not None:
                    try:
                        input_debug.log_event(event)
                    except Exception:  # noqa: BLE001 - logging is best-effort only
                        pass

                if current_screen == "menu":
                    rects = layout.card_rects
                elif current_screen == "wizard" and wizard is not None:
                    rects = _wizard_rects(layout, wizard)
                elif current_screen == OPERATION_SCREEN:
                    # The whole safe area is one big "tap anywhere" target
                    # — the operation screen's only touch interaction is
                    # dismissing it once finished (see handle_operation_event).
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
                    if ievent.action == Action.CONFIRM and item.action == SAVESYNC_ACTION:
                        savesync_screen = SaveSyncScreenState(romcloud_bin=romcloud_bin)
                        savesync_screen.refresh_status()
                        current_screen = "savesync"
                    else:
                        running, current_screen, message, message_kind, new_operation = _handle_menu_event(
                            ievent, state, layout, romcloud_bin, running, message, message_kind,
                        )
                        if new_operation is not None:
                            operation_screen = new_operation
                elif current_screen == "savesync" and savesync_screen is not None:
                    current_screen = _handle_savesync_event(ievent, savesync_screen)
                    if current_screen == "menu":
                        savesync_screen = None
                elif current_screen == "wizard" and wizard is not None:
                    if ievent.action == Action.BACK and wizard.step == WizardStep.WELCOME:
                        running = False
                    else:
                        wizard.handle_event(ievent, rects, romcloud_bin)
                elif current_screen == "controller_test":
                    current_screen = _handle_controller_test_event(
                        ievent, controller_test, input_manager,
                    )
                elif current_screen == OPERATION_SCREEN and operation_screen is not None:
                    current_screen = handle_operation_event(ievent, operation_screen)
                    if current_screen == "menu":
                        message, message_kind = operation_summary_message(operation_screen)
                        operation_screen = None

            # Drain output/state every frame regardless of whether an input
            # event arrived this frame — the whole point of the operation
            # screen is that the backend keeps working (and producing
            # output) while the UI keeps rendering and waiting for input.
            if current_screen == OPERATION_SCREEN and operation_screen is not None:
                operation_screen.poll()
            elif current_screen == "savesync" and savesync_screen is not None:
                savesync_screen.poll()
                savesync_screen.update_confirm(dt)
            elif current_screen == "wizard" and wizard is not None:
                wizard.poll()
                if wizard.finished:
                    current_screen = "menu"
                    wizard = None
                    message = "Setup complete"
                    message_kind = "success"

            if current_screen == "menu":
                for action in input_manager.update(dt):
                    _apply_direction(state, layout, action)
            elif current_screen == "wizard" and wizard is not None:
                rects = _wizard_rects(layout, wizard)
                for action in input_manager.update(dt):
                    wizard.update_direction(action, rects)

            if current_screen == "menu":
                _render_menu(pygame, screen, fonts, layout, state, message, message_kind)
            elif current_screen == "controller_test":
                _render_controller_test(pygame, screen, fonts, layout, input_manager, controller_test)
            elif current_screen == OPERATION_SCREEN and operation_screen is not None:
                _render_operation(pygame, screen, fonts, layout, operation_screen)
            elif current_screen == "savesync" and savesync_screen is not None:
                _render_savesync(pygame, screen, fonts, layout, savesync_screen)
            elif current_screen == "wizard" and wizard is not None:
                _render_wizard(pygame, screen, fonts, layout, wizard)

        return 0
    finally:
        if input_debug is not None:
            try:
                input_debug.close()
            except Exception:  # noqa: BLE001
                pass
        pygame.quit()


def _apply_direction(state: MenuState, layout: Layout, action: Action) -> None:
    direction = ACTION_DIRECTIONS.get(action)
    if direction is None:
        return
    new_index = find_next_focus_index(layout.card_rects, state.selected_index, *direction)
    state.select(new_index)


def _handle_menu_event(
    ievent: InputEvent,
    state: MenuState,
    layout: Layout,
    romcloud_bin: str,
    running: bool,
    message: Optional[str],
    message_kind: str,
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
        if item.action == EXIT_ACTION:
            running = False
        elif item.action == CONTROLLER_TEST_ACTION:
            next_screen = "controller_test"
        elif item.action in _OPERATIONS:
            next_screen = OPERATION_SCREEN
            operation = start_operation(item.action, romcloud_bin)
            return running, next_screen, message, message_kind, operation
        else:
            result = call_backend(romcloud_bin, item.action)
            message = format_result(item.action, result)
            message_kind = classify_message_kind(item.action, result)
        return running, next_screen, message, message_kind, None

    if action == Action.BACK:
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
    changes. Pure function — no pygame — so it is fully unit-tested.

    Returns the screen name to switch to next ("savesync" to stay, "menu"
    to leave). While a backend operation is in flight (PREVIEWING /
    COMMITTING / APPLYING_SETTINGS) input is ignored — the same "a
    legitimate operation is never interruptible mid-flight" rule the
    reusable operation screen already follows.
    """
    step = savesync_screen.step

    if step == DASHBOARD:
        if ievent.action in ACTION_DIRECTIONS:
            _, dy = ACTION_DIRECTIONS[ievent.action]
            if dy:
                savesync_screen.select(savesync_screen.selected_index + dy)
            return "savesync"
        if ievent.action == Action.CONFIRM:
            return "menu" if savesync_screen.confirm_dashboard_selection() == "back" else "savesync"
        if ievent.action == Action.BACK:
            return "menu"
        return "savesync"

    if step == PREVIEW:
        if ievent.action == Action.CONFIRM:
            savesync_screen.begin_confirm()
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
        if ievent.action == Action.CONFIRM:
            savesync_screen.set_xbox_enabled(not savesync_screen.status.get("xbox_enabled", False))
        elif ievent.action == Action.BACK:
            savesync_screen.return_to_dashboard()
        return "savesync"

    return "savesync"  # PREVIEWING / COMMITTING / APPLYING_SETTINGS: wait


def _render_menu(  # noqa: ANN001
    pygame,
    screen,
    fonts: dict,
    layout: Layout,
    state: MenuState,
    message: Optional[str],
    message_kind: str,
) -> None:
    screen.fill(_BG_COLOR)

    title = fonts["title"].render("ROMCloud", True, _FG_COLOR)
    screen.blit(title, (layout.safe_area.x, layout.safe_area.y))

    for i, (item, rect) in enumerate(zip(state.items, layout.card_rects)):
        color = _SELECTED_BG if i == state.selected_index else _CARD_BG
        pygame.draw.rect(screen, color, (rect.x, rect.y, rect.w, rect.h), border_radius=6)
        label = fonts["body"].render(item.label, True, _FG_COLOR)
        label_rect = label.get_rect(center=rect.center)
        screen.blit(label, label_rect)

    if message:
        max_chars = max(1, layout.message_rect.w // 8)
        color = _MESSAGE_COLORS.get(message_kind, _FG_COLOR)
        text = fonts["body"].render(message[:max_chars], True, color)
        screen.blit(text, (layout.message_rect.x, layout.message_rect.y))

    hint = fonts["hint"].render(_HINT_TEXT, True, _HINT_COLOR)
    screen.blit(hint, (layout.hint_rect.x, layout.hint_rect.y))

    pygame.display.flip()


def _render_controller_test(  # noqa: ANN001
    pygame,
    screen,
    fonts: dict,
    layout: Layout,
    input_manager: InputManager,
    controller_test: _ControllerTestScreenState,
) -> None:
    screen.fill(_BG_COLOR)

    title = fonts["title"].render("Controller Test", True, _FG_COLOR)
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
        label = fonts["body"].render(f"{action.value}{suffix}", True, _FG_COLOR)
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
) -> None:
    """Renders the reusable long-running operation screen — a title, a
    state label, and a bounded/scrolled/wrapped view of the subprocess's
    captured output. Deliberately plain (no monospace terminal styling):
    this is a 10-foot/Steam Deck status view, not a terminal emulator."""
    screen.fill(_BG_COLOR)

    title = fonts["title"].render(operation.title, True, _FG_COLOR)
    screen.blit(title, (layout.safe_area.x, layout.safe_area.y))

    state = operation.state
    state_color = _SUCCESS_COLOR if state == OperationState.SUCCEEDED else (
        _ERROR_COLOR if state == OperationState.FAILED else _FG_COLOR
    )
    state_y = layout.safe_area.y + layout.fonts.title + 8
    state_label = fonts["body"].render(_STATE_LABELS[state], True, state_color)
    screen.blit(state_label, (layout.safe_area.x, state_y))

    output_top = state_y + layout.fonts.body + 12
    output_bottom = layout.hint_rect.y - 8
    line_h = layout.fonts.body + 4
    viewport_rows = max(1, (output_bottom - output_top) // line_h)

    max_chars = max(1, layout.safe_area.w // 10)
    lines = wrap_lines(display_lines(operation.runner), max_chars)
    start, end = visible_window(len(lines), viewport_rows, operation.scroll_offset)

    y = output_top
    for line in lines[start:end]:
        color = _ERROR_COLOR if line.startswith("! ") else _FG_COLOR
        text = fonts["body"].render(line, True, color)
        screen.blit(text, (layout.safe_area.x, y))
        y += line_h

    hint_text = _OPERATION_HINT_FINISHED if operation.is_finished else _OPERATION_HINT_RUNNING
    hint = fonts["hint"].render(hint_text, True, _HINT_COLOR)
    screen.blit(hint, (layout.hint_rect.x, layout.hint_rect.y))

    pygame.display.flip()


def _savesync_body_lines(savesync_screen: SaveSyncScreenState) -> list[str]:
    """Pure text content for the current SaveSync step — unit-tested
    directly, independent of rendering."""
    step = savesync_screen.step
    status = savesync_screen.status

    if step == DASHBOARD:
        if status.get("remote_configured") is False:
            return [
                "ROMCloud data storage is not configured.",
                "SaveSync is unavailable until a writable destination is configured.",
                "",
                *DASHBOARD_ITEMS,
            ]
        reachable = status.get("remote_reachable")
        xbox_enabled = status.get("xbox_enabled", False)
        last_upload = status.get("last_upload")
        last_download = status.get("last_download")
        return [
            f"Remote: {'reachable' if reachable else 'unreachable'}",
            f"Original Xbox: {'enabled' if xbox_enabled else 'disabled'}",
            f"Last upload: {last_upload['timestamp'] if last_upload else 'never'}",
            f"Last download: {last_download['timestamp'] if last_download else 'never'}",
            "",
            *DASHBOARD_ITEMS,
        ]
    if step == PREVIEWING:
        return ["Comparing local and remote saves..."]
    if step == PREVIEW:
        summary = savesync_screen.preview_summary
        return [
            f"Added:     {summary.get('added', 0)}",
            f"Changed:   {summary.get('changed', 0)}",
            f"Removed:   {summary.get('removed', 0)}",
            f"Unchanged: {summary.get('unchanged', 0)}",
            "",
            "Press Confirm and hold for 3 seconds to apply, Back to cancel.",
        ]
    if step == CONFIRMING:
        percent = int(savesync_screen.confirm.progress * 100)
        return [f"Hold Confirm... {percent}%", "Release to cancel."]
    if step == COMMITTING:
        return ["Applying changes..."]
    if step == RESULT:
        if savesync_screen.error:
            return [f"Failed: {savesync_screen.error}", "Press Confirm to return."]
        record = savesync_screen.result
        return [
            f"Done. {record.get('artifact_count', 0)} artifact(s).",
            "Press Confirm to return.",
        ]
    if step == SETTINGS:
        xbox_enabled = status.get("xbox_enabled", False)
        return [
            "xemu stores Original Xbox saves inside its virtual hard drive,",
            "so ROMCloud must transfer the entire virtual drive to preserve",
            "them safely.",
            "",
            f"Original Xbox save sync: {'enabled' if xbox_enabled else 'disabled'}",
            "Press Confirm to toggle, Back to return.",
        ]
    if step == APPLYING_SETTINGS:
        return ["Applying setting..."]
    return []


def _render_savesync(  # noqa: ANN001
    pygame,
    screen,
    fonts: dict,
    layout: Layout,
    savesync_screen: SaveSyncScreenState,
) -> None:
    screen.fill(_BG_COLOR)

    title = fonts["title"].render("SaveSync", True, _FG_COLOR)
    screen.blit(title, (layout.safe_area.x, layout.safe_area.y))

    y = layout.safe_area.y + layout.fonts.title + 16
    line_h = layout.fonts.body + 6
    for i, line in enumerate(_savesync_body_lines(savesync_screen)):
        is_selected_item = (
            savesync_screen.step == DASHBOARD
            and line in DASHBOARD_ITEMS
            and DASHBOARD_ITEMS.index(line) == savesync_screen.selected_index
        )
        color = _SELECTED_BG if is_selected_item else _FG_COLOR
        text = fonts["body"].render(line, True, color)
        screen.blit(text, (layout.safe_area.x, y))
        y += line_h

    hint = fonts["hint"].render(_HINT_TEXT, True, _HINT_COLOR)
    screen.blit(hint, (layout.hint_rect.x, layout.hint_rect.y))

    pygame.display.flip()


def _wizard_content_rect(layout: Layout) -> Rect:
    top = layout.safe_area.y + layout.fonts.title + layout.fonts.hint + 20
    bottom = layout.hint_rect.y - 8
    return Rect(
        x=layout.safe_area.x,
        y=top,
        w=layout.safe_area.w,
        h=max(1, bottom - top),
    )


def _wizard_rects(layout: Layout, wizard: WizardState) -> list[Rect]:
    content = _wizard_content_rect(layout)
    if wizard.osk is not None:
        return compute_osk_layout(content, wizard.osk.keys)
    count = len(wizard.options)
    if count == 0:
        return []
    columns = 1 if count <= 4 else min(3, count)
    return compute_card_rects(content, count, columns)


def _wizard_body_lines(wizard: WizardState) -> list[str]:
    if wizard.step == WizardStep.WELCOME:
        return wizard.issues or ["Set up an SMB-hosted ROM library."]
    if wizard.step == WizardStep.SOURCE:
        return ["Select where your ROM library is stored."]
    if wizard.step == WizardStep.DISCOVER:
        return ["Connecting and finding accessible shares..."] if wizard.runner else []
    if wizard.step == WizardStep.DETECT:
        return [f"Checking //{wizard.server}/{wizard.share}..."] if wizard.runner else []
    if wizard.step == WizardStep.SYSTEMS:
        if not wizard.systems:
            return ["No recognized Batocera system folders were found."]
        return [f"{len(wizard.systems)} systems: {', '.join(wizard.systems)}"]
    if wizard.step == WizardStep.REMOTE_DATA:
        return [
            "Choose separate writable storage for synchronized ROMCloud data.",
            "SaveSync is unavailable if this step is skipped.",
        ]
    if wizard.step == WizardStep.REMOTE_DISCOVER:
        return ["Connecting and finding writable-data shares..."] if wizard.runner else []
    if wizard.step == WizardStep.REMOTE_VALIDATE:
        return [
            f"Checking //{wizard.remote_server}/{wizard.remote_share}..."
        ] if wizard.runner else []
    if wizard.step == WizardStep.REVIEW:
        return [
            f"SMB: //{wizard.server}/{wizard.share}",
            f"Systems: {len(wizard.systems)}",
            (
                f"ROMCloud data: //{wizard.remote_server}/{wizard.remote_share}"
                if wizard.remote_data_type == "smb"
                else f"ROMCloud data: {wizard.remote_data_root}"
                if wizard.remote_data_type == "local"
                else "ROMCloud data: not configured (SaveSync unavailable)"
            ),
            f"Cache: {wizard.cache_root} ({wizard.max_size_gb:g} GB max)",
        ]
    if wizard.step == WizardStep.APPLY:
        return [
            "Mounting source/data storage, validating writes, refreshing the catalog, "
            "and updating EmulationStation..."
        ] if wizard.runner else []
    if wizard.step == WizardStep.DONE:
        return [
            f"SMB source: //{wizard.applied_summary.get('server', wizard.server)}/{wizard.applied_summary.get('share', wizard.share)}",
            f"Detected systems: {wizard.applied_summary.get('system_count', len(wizard.systems))}",
            f"ROMCloud data: {wizard.applied_summary.get('remote_data_type', wizard.remote_data_type)}",
            f"Cache size: {wizard.applied_summary.get('max_size_gb', wizard.max_size_gb):g} GB",
            "ROMCloud is ready. Return to EmulationStation and restart or rescan it to show new games.",
        ]
    return []


def _render_wizard(  # noqa: ANN001
    pygame,
    screen,
    fonts: dict,
    layout: Layout,
    wizard: WizardState,
) -> None:
    screen.fill(_BG_COLOR)
    title = fonts["title"].render(wizard.title, True, _FG_COLOR)
    screen.blit(title, (layout.safe_area.x, layout.safe_area.y))

    progress = fonts["hint"].render(f"Setup {wizard.step_number} of {len(tuple(WizardStep))}", True, _HINT_COLOR)
    progress_rect = progress.get_rect(topright=(layout.safe_area.x + layout.safe_area.w, layout.safe_area.y))
    screen.blit(progress, progress_rect)

    content = _wizard_content_rect(layout)
    if wizard.osk is not None:
        text_rect = compute_osk_text_rect(content)
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
        for line in _wizard_body_lines(wizard):
            for wrapped in wrap_lines([line], max_chars):
                text = fonts["body"].render(wrapped, True, _FG_COLOR)
                screen.blit(text, (content.x, y))
                y += line_h

        for index, (label_text, rect) in enumerate(zip(wizard.options, _wizard_rects(layout, wizard))):
            color = _SELECTED_BG if index == wizard.selected_index else _CARD_BG
            pygame.draw.rect(screen, color, (rect.x, rect.y, rect.w, rect.h), border_radius=6)
            max_label_chars = max(1, rect.w // max(8, layout.fonts.body // 2))
            label = fonts["body"].render(label_text[:max_label_chars], True, _FG_COLOR)
            screen.blit(label, label.get_rect(center=rect.center))

    if wizard.error:
        max_chars = max(1, layout.message_rect.w // max(8, layout.fonts.body // 2))
        error = fonts["body"].render(wizard.error[:max_chars], True, _ERROR_COLOR)
        screen.blit(error, (layout.message_rect.x, layout.message_rect.y))

    if _WIZARD_HINT:
        hint = fonts["hint"].render(_WIZARD_HINT, True, _HINT_COLOR)
        screen.blit(hint, (layout.hint_rect.x, layout.hint_rect.y))
    pygame.display.flip()
