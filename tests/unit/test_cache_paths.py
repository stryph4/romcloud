"""Unit tests for romcloud.core.cache_paths — the cache layout invariant.

Invariant under test: ``cached asset path = <cache_root>/<system>/<asset path
relative to that system's source root>``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from romcloud.core.cache_paths import resolve_cache_path, system_relative_path
from romcloud.core.exceptions import CacheError


class TestSystemRelativePath:
    def test_strips_leading_system_segment(self):
        assert system_relative_path("ps2", "ps2/Game.iso").parts == ("Game.iso",)

    def test_preserves_nested_subdirectories(self):
        assert system_relative_path("ps2", "ps2/discs/1/Game.iso").parts == (
            "discs",
            "1",
            "Game.iso",
        )

    def test_rejects_absolute_path(self):
        with pytest.raises(CacheError):
            system_relative_path("ps2", "/etc/passwd")

    def test_rejects_empty_path(self):
        with pytest.raises(CacheError):
            system_relative_path("ps2", "")

    def test_rejects_mismatched_system(self):
        with pytest.raises(CacheError):
            system_relative_path("ps2", "snes/Game.sfc")

    def test_rejects_traversal_segment(self):
        with pytest.raises(CacheError):
            system_relative_path("ps2", "ps2/../../etc/passwd")

    def test_rejects_path_with_no_filename(self):
        with pytest.raises(CacheError):
            system_relative_path("ps2", "ps2")

    def test_rejects_invalid_system_name(self):
        with pytest.raises(CacheError):
            system_relative_path("../etc", "../etc/Game.iso")


class TestResolveCachePath:
    def test_builds_expected_path(self, tmp_path: Path):
        result = resolve_cache_path(tmp_path, "ps2", "ps2/Game.iso")
        assert result == tmp_path / "ps2" / "Game.iso"

    def test_preserves_source_basename_exactly(self, tmp_path: Path):
        result = resolve_cache_path(tmp_path, "ps2", "ps2/Weird Name (USA) [!].iso")
        assert result.name == "Weird Name (USA) [!].iso"

    def test_two_systems_same_filename_do_not_collide(self, tmp_path: Path):
        ps2_path = resolve_cache_path(tmp_path, "ps2", "ps2/Game.rom")
        snes_path = resolve_cache_path(tmp_path, "snes", "snes/Game.rom")
        assert ps2_path != snes_path
        assert ps2_path == tmp_path / "ps2" / "Game.rom"
        assert snes_path == tmp_path / "snes" / "Game.rom"

    def test_two_subdirectories_same_filename_do_not_collide(self, tmp_path: Path):
        disc1 = resolve_cache_path(tmp_path, "ps2", "ps2/discs/1/Game.iso")
        disc2 = resolve_cache_path(tmp_path, "ps2", "ps2/discs/2/Game.iso")
        assert disc1 != disc2
        assert disc1 == tmp_path / "ps2" / "discs" / "1" / "Game.iso"
        assert disc2 == tmp_path / "ps2" / "discs" / "2" / "Game.iso"

    def test_cannot_escape_cache_root_via_traversal(self, tmp_path: Path):
        with pytest.raises(CacheError):
            resolve_cache_path(tmp_path, "ps2", "ps2/../../outside")
