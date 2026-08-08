"""Unit tests for `romcloud.infrastructure.atomic_file`."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from romcloud.infrastructure.atomic_file import atomic_write_text


class TestAtomicWriteText:
    def test_creates_file_with_content(self, tmp_path: Path):
        path = tmp_path / "config.toml"
        atomic_write_text(path, "hello=1\n")
        assert path.read_text() == "hello=1\n"

    def test_creates_parent_directory(self, tmp_path: Path):
        path = tmp_path / "nested" / "dir" / "config.toml"
        atomic_write_text(path, "x=1\n")
        assert path.read_text() == "x=1\n"

    def test_overwrites_existing_content_completely(self, tmp_path: Path):
        path = tmp_path / "config.toml"
        path.write_text("old content that is much longer than new")
        atomic_write_text(path, "new")
        assert path.read_text() == "new"

    def test_applies_requested_mode(self, tmp_path: Path):
        path = tmp_path / "secret"
        atomic_write_text(path, "hunter2", mode=0o600)
        mode = path.stat().st_mode & 0o777
        assert mode == 0o600

    def test_overwrite_preserves_requested_mode_even_if_old_file_was_looser(self, tmp_path: Path):
        path = tmp_path / "secret"
        path.write_text("stale")
        path.chmod(0o644)

        atomic_write_text(path, "fresh", mode=0o600)

        assert (path.stat().st_mode & 0o777) == 0o600
        assert path.read_text() == "fresh"

    def test_no_leftover_temp_file_after_success(self, tmp_path: Path):
        path = tmp_path / "config.toml"
        atomic_write_text(path, "x=1\n")
        remaining = list(tmp_path.iterdir())
        assert remaining == [path]

    def test_leaves_original_file_untouched_and_cleans_up_temp_on_replace_failure(self, tmp_path, monkeypatch):
        path = tmp_path / "config.toml"
        path.write_text("original content")

        def fake_replace(src, dst):
            raise OSError("simulated failure")

        monkeypatch.setattr(os, "replace", fake_replace)

        with pytest.raises(OSError):
            atomic_write_text(path, "new content")

        # Original file must be completely unchanged.
        assert path.read_text() == "original content"
        # No stray temp file left behind.
        remaining = list(tmp_path.iterdir())
        assert remaining == [path]
