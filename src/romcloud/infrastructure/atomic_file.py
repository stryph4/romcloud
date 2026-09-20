"""Atomic file writes.

Writes content to a temporary file in the same directory as the target,
flushes and synchronizes that file, then renames it into place with
:func:`os.replace` — which is atomic on POSIX filesystems (the destination
either has its old content or its fully new content; never a partial write).
The parent directory is synchronized where supported so the replacement
survives a crash or power loss. Used anywhere a file must never be left
half-written (e.g. ``romcloud.toml``, credentials files).
"""

from __future__ import annotations

import errno
import os
import tempfile
from pathlib import Path
from typing import Optional


_UNSUPPORTED_DIRECTORY_FSYNC_ERRNOS = frozenset(
    value
    for value in (
        errno.EINVAL,
        getattr(errno, "ENOTSUP", None),
        getattr(errno, "EOPNOTSUPP", None),
    )
    if value is not None
)


def atomic_write_text(path: Path, content: str, *, mode: Optional[int] = None) -> None:
    """Atomically write *content* to *path*.

    If *mode* is given, the temp file is created with exactly those
    permission bits from the start (rather than writing then chmod'ing),
    so the final file is never briefly world/group-readable.
    """
    _atomic_write(path, content, mode=mode)


def atomic_write_bytes(path: Path, content: bytes, *, mode: Optional[int] = None) -> None:
    """Atomically write exact *content* bytes to *path*.

    Unlike text mode, this preserves canonical line endings on every host.
    """
    _atomic_write(path, content, mode=mode)


def _atomic_write(
    path: Path, content: str | bytes, *, mode: Optional[int]
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)

    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    tmp_path = Path(tmp_name)
    descriptor_owned = True
    try:
        if mode is not None:
            os.chmod(tmp_path, mode)
        if isinstance(content, bytes):
            handle = os.fdopen(fd, "wb")
        else:
            handle = os.fdopen(fd, "w", encoding="utf-8")
        descriptor_owned = False
        with handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
        _fsync_directory(path.parent)
    except BaseException:
        if descriptor_owned:
            try:
                os.close(fd)
            except OSError:
                pass
        tmp_path.unlink(missing_ok=True)
        raise


def _fsync_directory(directory: Path) -> None:
    """Persist a replacement directory entry where the platform supports it."""
    if os.name == "nt":
        # Windows' ``os.fsync`` maps to ``_commit``, which accepts file handles
        # but does not provide a supported directory-handle equivalent.
        return

    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    try:
        descriptor = os.open(directory, flags)
    except OSError as exc:
        if exc.errno in _UNSUPPORTED_DIRECTORY_FSYNC_ERRNOS:
            return
        raise
    try:
        try:
            os.fsync(descriptor)
        except OSError as exc:
            if exc.errno not in _UNSUPPORTED_DIRECTORY_FSYNC_ERRNOS:
                raise
    finally:
        os.close(descriptor)
