from __future__ import annotations

from types import SimpleNamespace

from ports_gfx import theme


def test_theme_has_high_contrast_brand_and_semantic_tokens() -> None:
    assert theme.BACKGROUND != theme.SURFACE != theme.SURFACE_RAISED
    assert theme.ACCENT != theme.SUCCESS != theme.WARNING != theme.ERROR
    assert sum(theme.TEXT) > sum(theme.MUTED_TEXT) > sum(theme.BACKGROUND)
    assert theme.FOCUS_BORDER_WIDTH >= 3
    assert theme.CARD_RADIUS > theme.CONTROL_RADIUS


def test_typography_stack_is_explicit_and_cross_platform() -> None:
    assert theme.FONT_STACK.startswith("Inter,Noto Sans,DejaVu Sans")
    assert "Segoe UI" in theme.FONT_STACK and "Arial" in theme.FONT_STACK


def test_system_font_uses_same_stack_and_distinct_heading_weight() -> None:
    calls: list[tuple[object, ...]] = []
    pygame = SimpleNamespace(
        font=SimpleNamespace(SysFont=lambda *args: calls.append(args) or args)
    )

    regular = theme.system_font(pygame, 24)
    heading = theme.system_font(pygame, 42, strong=True)

    assert regular == (theme.FONT_STACK, 24, False)
    assert heading == (theme.FONT_STACK, 42, True)


def test_system_font_falls_back_without_breaking_the_gui() -> None:
    calls: list[tuple[object, ...]] = []

    def sys_font(*args):
        calls.append(args)
        if args[0] is not None:
            raise RuntimeError("font lookup failed")
        return "fallback"

    pygame = SimpleNamespace(font=SimpleNamespace(SysFont=sys_font))
    assert theme.system_font(pygame, 28) == "fallback"
    assert calls == [(theme.FONT_STACK, 28, False), (None, 28, False)]
