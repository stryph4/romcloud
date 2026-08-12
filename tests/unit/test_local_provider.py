"""Unit tests for LocalFilesystemProvider."""

from __future__ import annotations

import pytest
import subprocess
from pathlib import Path
from types import SimpleNamespace

from romcloud.infrastructure.providers.local import (
    LocalFilesystemProvider,
    StorageAccessResult,
    WritableLocalFilesystemProvider,
    WritableMountedFilesystemProvider,
    probe_directory_access,
    probe_directory_access_bounded,
)
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


class TestWritableRemoteDataProviders:
    def test_explicit_local_root_requires_a_real_write_probe(self, tmp_path):
        provider = WritableLocalFilesystemProvider()

        assert provider.is_reachable(str(tmp_path)) is True
        assert list(tmp_path.glob(".romcloud-write-probe-*")) == []
        assert provider.is_reachable(str(tmp_path / "missing")) is False

    def test_probe_verifies_read_write_readback_and_cleanup(self, tmp_path):
        result = probe_directory_access(tmp_path, writable=True)

        assert result.ok is True
        assert result.as_dict() == {
            "connected": True,
            "read_verified": True,
            "write_verified": True,
            "cleanup_verified": True,
        }
        assert list(tmp_path.glob(".romcloud-write-probe-*")) == []

    def test_listable_but_non_creatable_directory_fails_write_probe(
        self, tmp_path, monkeypatch
    ):
        real_open = Path.open

        def deny_probe_create(path, mode="r", *args, **kwargs):
            if path.name.startswith(".romcloud-write-probe-") and mode == "x":
                raise PermissionError("read-only share")
            return real_open(path, mode, *args, **kwargs)

        monkeypatch.setattr(Path, "open", deny_probe_create)
        result = probe_directory_access(tmp_path, writable=True)

        assert result.connected and result.read_verified
        assert result.write_verified is False
        assert "write access failed" in result.detail

    def test_readback_failure_still_attempts_cleanup(self, tmp_path, monkeypatch):
        real_read_text = Path.read_text
        real_unlink = Path.unlink
        cleanup_calls = []

        def wrong_probe_content(path, *args, **kwargs):
            if path.name.startswith(".romcloud-write-probe-"):
                return "unexpected content"
            return real_read_text(path, *args, **kwargs)

        def track_unlink(path, *args, **kwargs):
            if path.name.startswith(".romcloud-write-probe-"):
                cleanup_calls.append(path)
            return real_unlink(path, *args, **kwargs)

        monkeypatch.setattr(Path, "read_text", wrong_probe_content)
        monkeypatch.setattr(Path, "unlink", track_unlink)

        result = probe_directory_access(tmp_path, writable=True)

        assert result.write_verified is False
        assert result.cleanup_verified is True
        assert len(cleanup_calls) == 1
        assert "content did not match" in result.detail

    def test_write_failure_after_create_still_attempts_cleanup(self, tmp_path, monkeypatch):
        real_open = Path.open
        real_unlink = Path.unlink
        cleanup_calls = []

        class FailingWrite:
            def __init__(self, handle):
                self.handle = handle

            def __enter__(self):
                self.handle.__enter__()
                return self

            def write(self, content):
                self.handle.write("partial")
                raise OSError("write interrupted")

            def __exit__(self, *args):
                return self.handle.__exit__(*args)

        def fail_probe_write(path, mode="r", *args, **kwargs):
            handle = real_open(path, mode, *args, **kwargs)
            if path.name.startswith(".romcloud-write-probe-") and mode == "x":
                return FailingWrite(handle)
            return handle

        def track_unlink(path, *args, **kwargs):
            if path.name.startswith(".romcloud-write-probe-"):
                cleanup_calls.append(path)
            return real_unlink(path, *args, **kwargs)

        monkeypatch.setattr(Path, "open", fail_probe_write)
        monkeypatch.setattr(Path, "unlink", track_unlink)

        result = probe_directory_access(tmp_path, writable=True)

        assert result.write_verified is False
        assert result.cleanup_verified is True
        assert len(cleanup_calls) == 1
        assert list(tmp_path.glob(".romcloud-write-probe-*")) == []

    def test_cleanup_failure_is_surfaced(self, tmp_path, monkeypatch):
        real_unlink = Path.unlink

        def fail_probe_cleanup(path, *args, **kwargs):
            if path.name.startswith(".romcloud-write-probe-"):
                raise PermissionError("delete denied")
            return real_unlink(path, *args, **kwargs)

        monkeypatch.setattr(Path, "unlink", fail_probe_cleanup)
        result = probe_directory_access(tmp_path, writable=True)

        assert result.write_verified is True
        assert result.cleanup_verified is False
        assert "cleanup failed" in result.detail
        probe = next(tmp_path.glob(".romcloud-write-probe-*"))
        real_unlink(probe)

    def test_exclusive_probe_never_overwrites_existing_file(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "romcloud.infrastructure.providers.local.uuid.uuid4",
            lambda: SimpleNamespace(hex="fixed"),
        )
        existing = tmp_path / ".romcloud-write-probe-fixed"
        existing.write_text("user data")

        result = probe_directory_access(tmp_path, writable=True)

        assert result.ok is False
        assert existing.read_text() == "user data"

    def test_mounted_root_rejects_bare_mountpoint(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "romcloud.infrastructure.mount.is_target_mounted_cifs",
            lambda path, **kwargs: False,
        )

        provider = WritableMountedFilesystemProvider(
            expected_server="nas.local", expected_share="ROMCloud"
        )
        assert provider.is_reachable(str(tmp_path)) is False
        assert list(tmp_path.iterdir()) == []

    def test_mounted_root_requires_both_rw_mode_and_write_permission(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setattr(
            "romcloud.infrastructure.mount.is_target_mounted_cifs",
            lambda path, **kwargs: True,
        )
        monkeypatch.setattr(
            "romcloud.infrastructure.providers.local.probe_directory_access",
            lambda path, **kwargs: StorageAccessResult(
                True, True, False, False, "not writable"
            ),
        )

        provider = WritableMountedFilesystemProvider(
            expected_server="nas.local", expected_share="ROMCloud"
        )
        assert provider.is_reachable(str(tmp_path)) is False


def test_network_filesystem_probe_timeout_is_safely_abandoned(tmp_path):
    class BlockedProcess:
        returncode = None
        killed = False

        def communicate(self, timeout):
            raise subprocess.TimeoutExpired(["probe"], timeout)

        def kill(self):
            self.killed = True

        def wait(self, timeout):
            raise subprocess.TimeoutExpired(["probe"], timeout)

    process = BlockedProcess()
    result = probe_directory_access_bounded(
        tmp_path,
        writable=False,
        timeout=0.01,
        popen=lambda *_args, **_kwargs: process,
    )

    assert result.ok is False
    assert "timed out" in result.detail
    assert process.killed is True
