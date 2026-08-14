"""ROMCloud's native visual and typography design tokens.

Pygame remains intentionally simple: solid, high-contrast surfaces derived
from the product artwork, with a bright cyan focus ring that reads clearly
at TV distance.  Browser CSS has its own adapter for the same design system.
"""

from __future__ import annotations


# Core surfaces
BACKGROUND = (3, 7, 20)
SURFACE = (9, 19, 39)
SURFACE_RAISED = (15, 31, 56)
BORDER = (35, 69, 111)

# Typography
TEXT = (239, 245, 255)
MUTED_TEXT = (170, 188, 216)
FONT_STACK = "Inter,Noto Sans,DejaVu Sans,Liberation Sans,Segoe UI,Arial"

# Product accents and semantic status colors
ACCENT = (38, 215, 242)
ACCENT_BLUE = (42, 133, 255)
ACCENT_VIOLET = (139, 80, 246)
FOCUS_FILL = (20, 67, 115)
ACTIVE_FILL = (18, 65, 62)
SUCCESS = (89, 218, 145)
WARNING = (246, 195, 83)
ERROR = (255, 108, 125)
PROGRESS_TRACK = (25, 45, 75)

CARD_RADIUS = 10
CONTROL_RADIUS = 8
FOCUS_BORDER_WIDTH = 4


def system_font(pygame, size: int, *, strong: bool = False):  # noqa: ANN001
    """Resolve the deliberate ROMCloud system stack with a safe fallback.

    Pygame/SDL_ttf chooses the first installed family. Passing the full stack
    keeps Batocera (normally DejaVu/Noto) and desktop development hosts close
    without redistributing a font binary under an uncertain platform license.
    """

    try:
        return pygame.font.SysFont(FONT_STACK, size, strong)
    except Exception:  # noqa: BLE001 - the default Pygame font is the last resort
        return pygame.font.SysFont(None, size, strong)


def draw_card(  # noqa: ANN001
    pygame,
    screen,
    rect,
    *,
    focused: bool = False,
    active: bool = False,
    radius: int = CARD_RADIUS,
):
    """Draw a consistent native surface with unmistakable focus semantics."""

    bounds = (rect.x, rect.y, rect.w, rect.h) if hasattr(rect, "x") else rect
    fill = FOCUS_FILL if focused else (ACTIVE_FILL if active else SURFACE_RAISED)
    pygame.draw.rect(screen, fill, bounds, border_radius=radius)
    border = ACCENT if focused else (SUCCESS if active else BORDER)
    width = FOCUS_BORDER_WIDTH if focused else (3 if active else 1)
    pygame.draw.rect(screen, border, bounds, width=width, border_radius=radius)
