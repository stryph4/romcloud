"""Unit tests for `romcloud.infrastructure.atomic_file`."""

from __future__ import annotations

import errno
import os
from pathlib import Path

import pytest

import romcloud.infrastructure.atomic_file as atomic_file
from romcloud.infrastructure.atomic_file import atomic_write_bytes, atomic_write_text


@pytest.mark.parametrize(
    ("writer", "content"),
    (
        (atomic_write_text, "new text\n"),
        (atomic_write_bytes, b"new bytes\n"),
    ),
)
def test_atomic_writers_flush_and_fsync_before_replace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, writer, content
) -> None:
    path = tmp_path / "state"
    events: list[str] = []
    real_fdopen = atomic_file.os.fdopen
    real_fsync = atomic_file.os.fsync
    real_replace = atomic_file.os.replace

    class TrackedHandle:
        def __init__(self, handle) -> None:
            self._handle = handle

        def __enter__(self):
            self._handle.__enter__()
            return self

        def __exit__(self, *args):
            return self._handle.__exit__(*args)

        def write(self, value):
            events.append("write")
            return self._handle.write(value)

        def flush(self) -> None:
            events.append("flush")
            self._handle.flush()

        def fileno(self) -> int:
            return self._handle.fileno()

    def tracked_fdopen(*args, **kwargs):
        return TrackedHandle(real_fdopen(*args, **kwargs))

    def tracked_fsync(descriptor: int) -> None:
        events.append("fsync")
        real_fsync(descriptor)

    def tracked_replace(source: object, destination: object) -> None:
        events.append("replace")
        real_replace(source, destination)

    monkeypatch.setattr(atomic_file.os, "fdopen", tracked_fdopen)
    monkeypatch.setattr(atomic_file.os, "fsync", tracked_fsync)
    monkeypatch.setattr(atomic_file.os, "replace", tracked_replace)

    writer(path, content)

    assert events[:4] == ["write", "flush", "fsync", "replace"]
    if os.name != "nt":
        assert events == ["write", "flush", "fsync", "replace", "fsync"]


@pytest.mark.parametrize(
    ("writer", "content"),
    (
        (atomic_write_text, "replacement"),
        (atomic_write_bytes, b"replacement"),
    ),
)
def test_atomic_writers_preserve_original_and_clean_temp_after_file_fsync_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, writer, content
) -> None:
    path = tmp_path / "state"
    path.write_bytes(b"original")
    monkeypatch.setattr(
        atomic_file.os,
        "fsync",
        lambda _descriptor: (_ for _ in ()).throw(
            OSError("simulated file fsync failure")
        ),
    )

    with pytest.raises(OSError, match="simulated file fsync failure"):
        writer(path, content)

    assert path.read_bytes() == b"original"
    assert list(tmp_path.iterdir()) == [path]


def test_directory_fsync_ignores_only_unsupported_operation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    closed: list[int] = []
    monkeypatch.setattr(atomic_file.os, "name", "posix")
    monkeypatch.setattr(atomic_file.os, "open", lambda *_args: 42)
    monkeypatch.setattr(
        atomic_file.os,
        "fsync",
        lambda _descriptor: (_ for _ in ()).throw(OSError(errno.EINVAL, "unsupported")),
    )
    monkeypatch.setattr(atomic_file.os, "close", closed.append)

    atomic_file._fsync_directory(tmp_path)

    assert closed == [42]


def test_directory_fsync_propagates_other_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    closed: list[int] = []
    monkeypatch.setattr(atomic_file.os, "name", "posix")
    monkeypatch.setattr(atomic_file.os, "open", lambda *_args: 42)
    monkeypatch.setattr(
        atomic_file.os,
        "fsync",
        lambda _descriptor: (_ for _ in ()).throw(OSError(errno.EIO, "I/O failure")),
    )
    monkeypatch.setattr(atomic_file.os, "close", closed.append)

    with pytest.raises(OSError, match="I/O failure"):
        atomic_file._fsync_directory(tmp_path)

    assert closed == [42]


class TestAtomicWriteBytes:
    def test_preserves_exact_bytes_without_platform_newline_translation(
        self, tmp_path: Path
    ) -> None:
        path = tmp_path / "canonical.json"

        atomic_write_bytes(path, b'{"line":"one"}\n')

        assert path.read_bytes() == b'{"line":"one"}\n'

    def test_replace_failure_preserves_original_and_cleans_temporary(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        path = tmp_path / "canonical.json"
        path.write_bytes(b"original")
        monkeypatch.setattr(
            os,
            "replace",
            lambda *_args: (_ for _ in ()).throw(OSError("simulated failure")),
        )

        with pytest.raises(OSError, match="simulated failure"):
            atomic_write_bytes(path, b"replacement")

        assert path.read_bytes() == b"original"
        assert list(tmp_path.iterdir()) == [path]


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
