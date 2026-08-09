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
from ports_gfx.layout import Layout, compute_layout, find_next_focus_index
from ports_gfx.menu import CONTROLLER_TEST_ACTION, EXIT_ACTION, MenuItem, MenuState

MENU_ITEMS: tuple[MenuItem, ...] = (
    MenuItem("Catalog Status", "status"),
    MenuItem("Refresh Catalog", "refresh"),
    MenuItem("Health Check", "healthcheck"),
    MenuItem("Cache Status", "cache-status"),
    MenuItem("Controller Test", CONTROLLER_TEST_ACTION),
    MenuItem("Exit", EXIT_ACTION),
)

_BG_COLOR = (18, 18, 24)
_CARD_BG = (40, 40, 50)
_FG_COLOR = (230, 230, 230)
_SELECTED_BG = (60, 90, 160)
_ERROR_COLOR = (220, 90, 90)
_HINT_COLOR = (150, 150, 150)

# Fallback used only if the display driver reports a nonsensical size
# (e.g. a headless/dummy driver) — never a "design resolution" the real
# layout is scaled against.
_FALLBACK_SIZE = (1280, 720)
_MIN_SANE_DIMENSION = 240

_TARGET_FPS = 30
_HINT_TEXT = "Up/Down/Left/Right select   Enter confirm   Esc exit"


def format_result(action: str, result: BackendResult) -> str:
    """Render a :class:`BackendResult` as a single display line.

    Pure function — no pygame — so it is fully unit-tested.
    """
    if not result.ok:
        return f"Error: {result.error}"
    return f"{action}: {result.data}"


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
        current_screen = "menu"
        message: Optional[str] = None
        message_is_error = False

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

                rects = layout.card_rects if current_screen == "menu" else ()
                ievent = input_manager.handle_event(
                    event, screen_w=screen_w, screen_h=screen_h, rects=rects, now=now,
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
                    running, current_screen, message, message_is_error = _handle_menu_event(
                        ievent, state, layout, romcloud_bin, running, message, message_is_error,
                    )
                elif current_screen == "controller_test":
                    current_screen = _handle_controller_test_event(
                        ievent, controller_test, input_manager,
                    )

            if current_screen == "menu":
                for action in input_manager.update(dt):
                    _apply_direction(state, layout, action)

            if current_screen == "menu":
                _render_menu(pygame, screen, fonts, layout, state, message, message_is_error)
            else:
                _render_controller_test(pygame, screen, fonts, layout, input_manager, controller_test)

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
    message_is_error: bool,
) -> tuple[bool, str, Optional[str], bool]:
    action = ievent.action
    next_screen = "menu"

    if ievent.touch_index is not None:
        state.select(ievent.touch_index)

    if action in ACTION_DIRECTIONS:
        _apply_direction(state, layout, action)
        return running, next_screen, None, message_is_error

    if action == Action.CONFIRM:
        item = state.selected_item
        if item.action == EXIT_ACTION:
            running = False
        elif item.action == CONTROLLER_TEST_ACTION:
            next_screen = "controller_test"
        else:
            result = call_backend(romcloud_bin, item.action)
            message = format_result(item.action, result)
            message_is_error = not result.ok
        return running, next_screen, message, message_is_error

    if action == Action.BACK:
        running = False
        return running, next_screen, message, message_is_error

    return running, next_screen, message, message_is_error


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


def _render_menu(  # noqa: ANN001
    pygame,
    screen,
    fonts: dict,
    layout: Layout,
    state: MenuState,
    message: Optional[str],
    message_is_error: bool,
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
        color = _ERROR_COLOR if message_is_error else _FG_COLOR
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


