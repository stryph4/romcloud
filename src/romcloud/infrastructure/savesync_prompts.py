"""Durable exact-ID handoff for game-stop SaveSync conflict prompts."""

from __future__ import annotations

import json
import os
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from romcloud.core.exceptions import SaveSyncError


def queue_path(data_root: Path) -> Path:
    return Path(data_root) / "savesync-conflict-prompts.json"


def pending_ids(data_root: Path) -> tuple[str, ...]:
    path = queue_path(data_root)
    with _queue_lock(path):
        return _read(path)


def enqueue(data_root: Path, conflict_ids: tuple[str, ...]) -> tuple[str, ...]:
    """Append conflict identities once, retaining their discovery order."""
    path = queue_path(data_root)
    with _queue_lock(path):
        current = list(_read(path))
        known = set(current)
        for conflict_id in conflict_ids:
            value = _conflict_id(conflict_id)
            if value not in known:
                current.append(value)
                known.add(value)
        _write(path, tuple(current))
        return tuple(current)


def contains(data_root: Path, conflict_id: str) -> bool:
    value = _conflict_id(conflict_id)
    return value in pending_ids(data_root)


def complete(data_root: Path, conflict_id: str) -> tuple[str, ...]:
    """Remove one handled or stale prompt without touching conflict state."""
    value = _conflict_id(conflict_id)
    path = queue_path(data_root)
    with _queue_lock(path):
        remaining = tuple(item for item in _read(path) if item != value)
        _write(path, remaining)
        return remaining


@contextmanager
def popup_process_lock(data_root: Path) -> Iterator[bool]:
    """Allow at most one short-lived popup process for this installation."""
    lock_path = Path(data_root) / ".savesync-conflict-popup.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+b") as handle:
        try:
            _lock(handle, nonblocking=True)
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            _unlock(handle)


@contextmanager
def _queue_lock(path: Path) -> Iterator[None]:
    lock_path = path.with_name(".savesync-conflict-prompts.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+b") as handle:
        _lock(handle, nonblocking=False)
        try:
            yield
        finally:
            _unlock(handle)


def _read(path: Path) -> tuple[str, ...]:
    if not path.exists():
        return ()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if payload.get("version") != 1:
            raise ValueError
        values = payload["conflict_ids"]
        if not isinstance(values, list):
            raise TypeError
        result = tuple(_conflict_id(value) for value in values)
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise SaveSyncError(
            f"SaveSync conflict prompt queue is invalid: {path}"
        ) from exc
    if len(set(result)) != len(result):
        raise SaveSyncError(f"SaveSync conflict prompt queue has duplicate IDs: {path}")
    return result


def _write(path: Path, conflict_ids: tuple[str, ...]) -> None:
    if not conflict_ids:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        else:
            _fsync_parent(path)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    document = json.dumps(
        {"version": 1, "conflict_ids": list(conflict_ids)},
        indent=2,
        sort_keys=True,
    ) + "\n"
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(document)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        _fsync_parent(path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _conflict_id(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("SaveSync conflict ID must be non-empty text")
    return value


def _fsync_parent(path: Path) -> None:
    if os.name == "nt":  # pragma: no cover - Batocera is POSIX
        return
    descriptor = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _lock(handle, *, nonblocking: bool) -> None:  # noqa: ANN001
    if os.name == "nt":  # pragma: no cover - Batocera is POSIX
        import msvcrt

        mode = msvcrt.LK_NBLCK if nonblocking else msvcrt.LK_LOCK
        msvcrt.locking(handle.fileno(), mode, 1)
        return
    import fcntl

    flags = fcntl.LOCK_EX | (fcntl.LOCK_NB if nonblocking else 0)
    fcntl.flock(handle.fileno(), flags)


def _unlock(handle) -> None:  # noqa: ANN001
    if os.name == "nt":  # pragma: no cover - Batocera is POSIX
        import msvcrt

        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        return
    import fcntl

    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
