"""Reusable ROMCloud splash/progress rendering for display handoffs."""

from __future__ import annotations

from pathlib import Path


_BACKGROUND = (3, 6, 20)
_PANEL = (5, 9, 28)
_TEXT = (238, 242, 255)
_MUTED = (170, 185, 215)
_TRACK = (35, 46, 78)
_PROGRESS = (30, 190, 245)
_SPLASH_ASSET = Path(__file__).resolve().parent / "assets" / "splash.png"


class SplashRenderer:
    """Paint a branded, aspect-preserving splash with dynamic progress."""

    def __init__(self, pygame, screen, *, asset_path: Path = _SPLASH_ASSET) -> None:  # noqa: ANN001
        self._pygame = pygame
        self._screen = screen
        self._background = None
        try:
            self._background = pygame.image.load(str(asset_path)).convert()
        except Exception:  # noqa: BLE001 - fallback still paints an intentional frame
            self._background = None

    def render(self, title: str, status: str, progress: float) -> None:
        """Render and immediately flip one monotonic stage frame."""
        pygame = self._pygame
        screen = self._screen
        width, height = screen.get_size()
        screen.fill(_BACKGROUND)

        if self._background is not None:
            image_w, image_h = self._background.get_size()
            scale = min(width / image_w, height / image_h)
            scaled_size = (max(1, round(image_w * scale)), max(1, round(image_h * scale)))
            image = pygame.transform.smoothscale(self._background, scaled_size)
            screen.blit(image, ((width - scaled_size[0]) // 2, (height - scaled_size[1]) // 2))

        panel_h = max(112, round(height * 0.18))
        panel_y = height - panel_h
        pygame.draw.rect(screen, _PANEL, (0, panel_y, width, panel_h))

        margin = max(24, round(width * 0.06))
        title_font = pygame.font.SysFont(None, max(30, round(height * 0.045)))
        status_font = pygame.font.SysFont(None, max(22, round(height * 0.032)))
        title_surface = title_font.render(title, True, _TEXT)
        status_surface = status_font.render(status, True, _MUTED)
        screen.blit(title_surface, (margin, panel_y + round(panel_h * 0.15)))
        screen.blit(status_surface, (margin, panel_y + round(panel_h * 0.48)))

        bar_x = margin
        bar_w = max(1, width - 2 * margin)
        bar_h = max(8, round(height * 0.012))
        bar_y = height - max(18, round(panel_h * 0.14)) - bar_h
        pygame.draw.rect(screen, _TRACK, (bar_x, bar_y, bar_w, bar_h), border_radius=bar_h // 2)
        fraction = max(0.0, min(1.0, float(progress)))
        fill_w = round(bar_w * fraction)
        if fill_w:
            pygame.draw.rect(
                screen,
                _PROGRESS,
                (bar_x, bar_y, fill_w, bar_h),
                border_radius=bar_h // 2,
            )
        pygame.display.flip()
