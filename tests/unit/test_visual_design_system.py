from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).parents[2]
STATIC = ROOT / "src" / "romcloud" / "web" / "static"


def test_browser_theme_centralizes_brand_and_semantic_tokens() -> None:
    css = (STATIC / "app.css").read_text(encoding="utf-8")
    for token in (
        "--font-ui", "--bg", "--surface", "--surface-raised", "--border",
        "--text", "--muted", "--accent", "--accent-blue", "--accent-violet",
        "--success", "--warning", "--danger", "--focus-fill",
        "--radius-control", "--radius-card",
    ):
        assert token in css


def test_browser_typography_and_focus_are_deliberate() -> None:
    css = (STATIC / "app.css").read_text(encoding="utf-8")
    assert 'Inter, "Noto Sans", "DejaVu Sans", "Segoe UI", Arial' in css
    assert "font-synthesis: none" in css
    assert ".content h1" in css and "font-weight: 780" in css
    assert ".game-title b" in css and ".game-title small" in css
    assert "outline: 4px solid var(--accent)" in css


def test_browser_keeps_dense_and_responsive_management_layouts() -> None:
    css = (STATIC / "app.css").read_text(encoding="utf-8")
    assert "grid-template-columns: 32px minmax(180px,1fr) minmax(150px,.5fr) auto" in css
    assert "@media (max-width: 820px)" in css
    assert "@media (max-width: 480px)" in css
    assert "@media (prefers-reduced-motion: reduce)" in css


def test_auth_dialog_status_and_progress_surfaces_are_themed() -> None:
    css = (STATIC / "app.css").read_text(encoding="utf-8")
    for selector in (
        ".auth", ".auth-card", ".dialog-card", ".badge.cached",
        ".badge.pinned", ".badge.incomplete", ".badge.remote_only",
        ".banner", ".job progress", ".controller-status",
    ):
        assert selector in css
