"""Unit tests for LocalFilesystemProvider."""

from __future__ import annotations

import pytest
from pathlib import Path

from romcloud.core.providers.local import LocalFilesystemProvider
from romcloud.core.exceptions import ProviderError, ProviderNotReachableError, TransferError


@pytest.fixture
def source_tree(tmp_path: Path) -> Path:
    root = tmp_path / "roms"
    (root / "ps2").mkdir(parents=True)
    (root / "nes").mkdir(parents=True)
    (root / ".hidden_system").mkdir(parents=True)
    (root / "ps2" / "Game A.iso").write_bytes(b"a" * 1000)
    (root / "ps2" / "Game B.iso").write_bytes(b"b" * 2000)
    (root / "ps2" / ".hidden.txt").write_text("skip")
    (root / "nes" / "Mario.nes").write_bytes(b"n" * 500)
    return root


class TestLocalFilesystemProvider:
    def test_is_reachable_true(self, tmp_path):
        p = LocalFilesystemProvider()
        assert p.is_reachable(str(tmp_path)) is True

    def test_is_reachable_false(self):
        p = LocalFilesystemProvider()
        assert p.is_reachable("/nonexistent/path/xyz") is False

    def test_list_systems_returns_dirs(self, source_tree):
        p = LocalFilesystemProvider()
        systems = p.list_systems(str(source_tree))
        assert "ps2" in systems
        assert "nes" in systems

    def test_list_systems_excludes_hidden(self, source_tree):
        p = LocalFilesystemProvider()
        systems = p.list_systems(str(source_tree))
        assert ".hidden_system" not in systems

    def test_list_systems_nonexistent(self):
        p = LocalFilesystemProvider()
        with pytest.raises(ProviderNotReachableError):
            p.list_systems("/nonexistent/xyz")

    def test_list_entries_files(self, source_tree):
        p = LocalFilesystemProvider()
        entries = p.list_entries(str(source_tree), "ps2")
        names = [e.name for e in entries]
        assert "Game A.iso" in names
        assert "Game B.iso" in names

    def test_list_entries_excludes_hidden(self, source_tree):
        p = LocalFilesystemProvider()
        entries = p.list_entries(str(source_tree), "ps2")
        names = [e.name for e in entries]
        assert ".hidden.txt" not in names

    def test_list_entries_has_size(self, source_tree):
        p = LocalFilesystemProvider()
        entries = p.list_entries(str(source_tree), "ps2")
        by_name = {e.name: e for e in entries}
        assert by_name["Game A.iso"].size_bytes == 1000
        assert by_name["Game B.iso"].size_bytes == 2000

    def test_list_entries_is_not_directory(self, source_tree):
        p = LocalFilesystemProvider()
        entries = p.list_entries(str(source_tree), "ps2")
        for e in entries:
            assert e.is_directory is False

    def test_list_entries_bad_system(self, source_tree):
        p = LocalFilesystemProvider()
        with pytest.raises(ProviderError):
            p.list_entries(str(source_tree), "nonexistent")

    def test_transfer_file(self, source_tree, tmp_path):
        p = LocalFilesystemProvider()
        dest = tmp_path / "dest" / "Game A.iso"
        p.transfer_to(
            str(source_tree / "ps2" / "Game A.iso"),
            str(dest),
        )
        assert dest.exists()
        assert dest.read_bytes() == b"a" * 1000

    def test_transfer_reports_progress(self, source_tree, tmp_path):
        p = LocalFilesystemProvider()
        progress_calls = []
        dest = tmp_path / "out.iso"
        p.transfer_to(
            str(source_tree / "ps2" / "Game A.iso"),
            str(dest),
            on_progress=lambda done, total: progress_calls.append((done, total)),
        )
        assert len(progress_calls) > 0
        last_done, last_total = progress_calls[-1]
        assert last_done == last_total == 1000

    def test_transfer_resumes_complete_file(self, source_tree, tmp_path):
        """If destination already has the correct size, no data is copied."""
        p = LocalFilesystemProvider()
        dest = tmp_path / "Game A.iso"
        # Pre-populate with correct content
        dest.write_bytes(b"a" * 1000)
        mtime_before = dest.stat().st_mtime

        p.transfer_to(str(source_tree / "ps2" / "Game A.iso"), str(dest))
        # mtime should not change because the file was skipped
        assert dest.stat().st_mtime == mtime_before

    def test_transfer_missing_source(self, tmp_path):
        p = LocalFilesystemProvider()
        with pytest.raises(ProviderError):
            p.transfer_to("/nonexistent/file.iso", str(tmp_path / "out.iso"))

    def test_transfer_directory(self, tmp_path):
        src_dir = tmp_path / "src_game"
        src_dir.mkdir()
        (src_dir / "disc1.bin").write_bytes(b"x" * 100)
        (src_dir / "sub" / "file.dat").parent.mkdir()
        (src_dir / "sub" / "file.dat").write_bytes(b"y" * 50)

        dst_dir = tmp_path / "dst_game"
        p = LocalFilesystemProvider()
        p.transfer_to(str(src_dir), str(dst_dir))

        assert (dst_dir / "disc1.bin").read_bytes() == b"x" * 100
        assert (dst_dir / "sub" / "file.dat").read_bytes() == b"y" * 50

    def test_get_size_file(self, source_tree):
        p = LocalFilesystemProvider()
        size = p.get_size(str(source_tree / "ps2" / "Game A.iso"))
        assert size == 1000

    def test_get_size_directory(self, source_tree):
        p = LocalFilesystemProvider()
        size = p.get_size(str(source_tree / "ps2"))
        # 1000 + 2000 from game files; .hidden.txt ("skip" = 4 bytes) is also included
        # get_size is a raw recursive total — it doesn't skip hidden files.
        assert size is not None
        assert size >= 3000

    def test_provider_id(self):
        p = LocalFilesystemProvider()
        assert p.provider_id == "local"
