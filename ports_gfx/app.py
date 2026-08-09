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

Everything above the lazy pygame import (menu contents, result formatting)
is plain Python and unit-tested directly; the render/event loop itself is
not (mirrors how ``romcloud.ui.progress``/``romcloud.ui.maintenance`` leave
their curses render loops untested — they need a real display/terminal).
"""

from __future__ import annotations

import sys
from typing import Optional

from ports_gfx.client import BackendResult, call_backend
from ports_gfx.layout import Layout, compute_layout, find_next_focus_index
from ports_gfx.menu import EXIT_ACTION, MenuItem, MenuState

MENU_ITEMS: tuple[MenuItem, ...] = (
    MenuItem("Catalog Status", "status"),
    MenuItem("Refresh Catalog", "refresh"),
    MenuItem("Health Check", "healthcheck"),
    MenuItem("Cache Status", "cache-status"),
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


def _direction_for_key(pygame, key: int) -> Optional[tuple[int, int]]:  # noqa: ANN001
    if key in (pygame.K_UP, pygame.K_w):
        return (0, -1)
    if key in (pygame.K_DOWN, pygame.K_s):
        return (0, 1)
    if key in (pygame.K_LEFT, pygame.K_a):
        return (-1, 0)
    if key in (pygame.K_RIGHT, pygame.K_d):
        return (1, 0)
    return None


def _run(pygame, romcloud_bin: str) -> int:  # noqa: ANN001 — `pygame` module, imported lazily
    pygame.init()
    try:
        screen_w, screen_h = _detect_screen_size(pygame)
        screen = _open_display(pygame, screen_w, screen_h)
        pygame.display.set_caption("ROMCloud")

        state = MenuState(list(MENU_ITEMS))
        layout = compute_layout(screen_w, screen_h, len(state.items))
        fonts = _build_fonts(pygame, layout)
        clock = pygame.time.Clock()

        message: Optional[str] = None
        message_is_error = False

        running = True
        while running:
            for event in pygame.event.get():
                if event.type == pygame.QUIT:
                    running = False
                elif event.type == pygame.VIDEORESIZE:
                    screen_w, screen_h = event.w, event.h
                    layout = compute_layout(screen_w, screen_h, len(state.items))
                    fonts = _build_fonts(pygame, layout)
                elif event.type == pygame.KEYDOWN:
                    direction = _direction_for_key(pygame, event.key)
                    if direction is not None:
                        new_index = find_next_focus_index(
                            layout.card_rects, state.selected_index, *direction
                        )
                        state.select(new_index)
                        message = None
                    elif event.key in (pygame.K_RETURN, pygame.K_SPACE):
                        item = state.selected_item
                        if item.action == EXIT_ACTION:
                            running = False
                        else:
                            result = call_backend(romcloud_bin, item.action)
                            message = format_result(item.action, result)
                            message_is_error = not result.ok
                    elif event.key == pygame.K_ESCAPE:
                        running = False

            _render(pygame, screen, fonts, layout, state, message, message_is_error)
            clock.tick(_TARGET_FPS)

        return 0
    finally:
        pygame.quit()


def _render(  # noqa: ANN001
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

