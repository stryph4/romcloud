"""Focused tests for the reusable Pygame splash renderer."""

from __future__ import annotations

from types import SimpleNamespace

from ports_gfx.splash import SplashRenderer


class _Surface:
    def __init__(self, size: tuple[int, int]) -> None:
        self.size = size
        self.blits: list[tuple[object, tuple[int, int]]] = []

    def get_size(self) -> tuple[int, int]:
        return self.size

    def convert(self):
        return self

    def fill(self, _color) -> None:
        pass

    def blit(self, source, position) -> None:
        self.blits.append((source, position))


class _Font:
    def render(self, text, _antialias, _color):
        return text


def test_splash_scales_background_without_distortion_and_flips():
    screen = _Surface((1000, 1000))
    background = _Surface((1600, 900))
    scaled: list[tuple[int, int]] = []
    flips: list[bool] = []

    def smoothscale(_image, size):
        scaled.append(size)
        return _Surface(size)

    pygame = SimpleNamespace(
        image=SimpleNamespace(load=lambda _path: background),
        transform=SimpleNamespace(smoothscale=smoothscale),
        draw=SimpleNamespace(rect=lambda *_args, **_kwargs: None),
        font=SimpleNamespace(SysFont=lambda *_args: _Font()),
        display=SimpleNamespace(flip=lambda: flips.append(True)),
    )

    SplashRenderer(pygame, screen).render("Starting ROMCloud…", "Display ready", 0.12)

    assert scaled == [(1000, 562)]
    assert screen.blits[0][1] == (0, 219)
    assert flips == [True]


def test_missing_artwork_still_paints_and_flips():
    screen = _Surface((1280, 720))
    flips: list[bool] = []
    pygame = SimpleNamespace(
        image=SimpleNamespace(load=lambda _path: (_ for _ in ()).throw(OSError("missing"))),
        transform=SimpleNamespace(),
        draw=SimpleNamespace(rect=lambda *_args, **_kwargs: None),
        font=SimpleNamespace(SysFont=lambda *_args: _Font()),
        display=SimpleNamespace(flip=lambda: flips.append(True)),
    )

    SplashRenderer(pygame, screen).render("Starting ROMCloud…", "Display ready", 0.0)

    assert flips == [True]
