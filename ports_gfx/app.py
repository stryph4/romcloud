"""Pygame-based Batocera Ports UI — the actual rendering/event loop.

Runs under Batocera's SYSTEM Python (pygame/SDL already present there —
confirmed pygame 2.5.2 / SDL 2.32.8 on real hardware). ``pygame`` is
imported lazily, only inside :func:`run_app`, so merely importing this
module never fails on a machine without pygame (e.g. this repo's own dev
venv) — the failure is deferred to the point where the UI is actually
launched, where it can be reported clearly instead of via a module-import
traceback at ``python -m ports_gfx`` startup.

Everything above the lazy pygame import (menu contents, result formatting)
is plain Python and unit-tested directly; the render/event loop itself is
not (mirrors how ``romcloud.ui.progress``/``romcloud.ui.maintenance`` leave
their curses render loops untested — they need a real display/terminal).
"""

from __future__ import annotations

import sys
from typing import Optional

from ports_gfx.client import BackendResult, call_backend
from ports_gfx.menu import EXIT_ACTION, MenuItem, MenuState

MENU_ITEMS: tuple[MenuItem, ...] = (
    MenuItem("Catalog Status", "status"),
    MenuItem("Refresh Catalog", "refresh"),
    MenuItem("Health Check", "healthcheck"),
    MenuItem("Cache Status", "cache-status"),
    MenuItem("Exit", EXIT_ACTION),
)

_BG_COLOR = (18, 18, 24)
_FG_COLOR = (230, 230, 230)
_SELECTED_BG = (60, 90, 160)
_ERROR_COLOR = (220, 90, 90)
_HINT_COLOR = (150, 150, 150)

_WINDOW_SIZE = (960, 540)
_FONT_SIZE = 28
_TARGET_FPS = 30


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


def _run(pygame, romcloud_bin: str) -> int:  # noqa: ANN001 — `pygame` module, imported lazily
    pygame.init()
    try:
        screen = pygame.display.set_mode(_WINDOW_SIZE)
        pygame.display.set_caption("ROMCloud")
        font = pygame.font.SysFont(None, _FONT_SIZE)
        clock = pygame.time.Clock()

        state = MenuState(list(MENU_ITEMS))
        message: Optional[str] = None
        message_is_error = False

        running = True
        while running:
            for event in pygame.event.get():
                if event.type == pygame.QUIT:
                    running = False
                elif event.type == pygame.KEYDOWN:
                    if event.key in (pygame.K_UP, pygame.K_w):
                        state.move_up()
                        message = None
                    elif event.key in (pygame.K_DOWN, pygame.K_s):
                        state.move_down()
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

            _render(pygame, screen, font, state, message, message_is_error)
            clock.tick(_TARGET_FPS)

        return 0
    finally:
        pygame.quit()


def _render(pygame, screen, font, state: MenuState, message: Optional[str], message_is_error: bool) -> None:  # noqa: ANN001
    screen.fill(_BG_COLOR)

    title = font.render("ROMCloud", True, _FG_COLOR)
    screen.blit(title, (40, 30))

    for i, item in enumerate(state.items):
        y = 100 + i * 44
        if i == state.selected_index:
            pygame.draw.rect(screen, _SELECTED_BG, (30, y - 6, 400, 38))
        label = font.render(item.label, True, _FG_COLOR)
        screen.blit(label, (44, y))

    if message:
        color = _ERROR_COLOR if message_is_error else _FG_COLOR
        text = font.render(message[:100], True, color)
        screen.blit(text, (40, 420))

    hint = font.render("Up/Down select   Enter confirm   Esc exit", True, _HINT_COLOR)
    screen.blit(hint, (40, 480))

    pygame.display.flip()
