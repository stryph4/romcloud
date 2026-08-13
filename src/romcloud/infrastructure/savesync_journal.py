"""Durable remote SaveSync mutation journal.

The journal is provider-neutral metadata stored beside ROMCloud remote-data
content. It is an optimization for change discovery only; reconciliation still
verifies filesystem content and existing baselines before mutating data.
"""

from __future__ import annotations

import json
import os
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from romcloud.core.exceptions import SaveSyncError

SCHEMA_VERSION = 1
MAX_HISTORY = 512


def default_journal_path(remote_saves_root: Path) -> Path:
    return Path(remote_saves_root).parent / "savesync-journal.json"


def load(path: Path) -> dict[str, object]:
    journal_path = Path(path)
    if not journal_path.exists():
        return {
            "schema_version": SCHEMA_VERSION,
            "generation": 0,
            "history": [],
        }
    try:
        payload = json.loads(journal_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SaveSyncError(f"SaveSync journal is corrupt: {journal_path}") from exc
    return _validate_document(payload, path=journal_path)


def save(path: Path, document: dict[str, object]) -> None:
    journal_path = Path(path)
    validated = _validate_document(document, path=journal_path)
    content = json.dumps(validated, indent=2, sort_keys=True) + "\n"
    _durable_atomic_write_text(journal_path, content)


def load_or_reset(path: Path) -> dict[str, object]:
    """Load a valid document or replace a corrupt/incompatible one safely."""
    journal_path = Path(path)
    try:
        return load(journal_path)
    except SaveSyncError:
        if journal_path.exists():
            backup = journal_path.with_name(journal_path.name + ".corrupt")
            try:
                journal_path.replace(backup)
            except OSError:
                pass
        document = {
            "schema_version": SCHEMA_VERSION,
            "generation": 0,
            "history": [],
        }
        save(journal_path, document)
        return document


def append_mutations(
    path: Path,
    *,
    device_id: str,
    revision: str,
    timestamp: str,
    mutations: list[dict[str, object]],
    max_history: int = MAX_HISTORY,
) -> int:
    """Append committed mutation metadata and return new generation.

    The caller must invoke this only after the corresponding remote mutation
    commit has succeeded.
    """
    if not mutations:
        return int(load_or_reset(path)["generation"])
    if max_history < 1:
        raise SaveSyncError("SaveSync journal max_history must be positive")

    with journal_lock(path):
        document = load_or_reset(path)
        generation = int(document["generation"])
        history = list(document["history"])
        for mutation in mutations:
            generation += 1
            entry = {
                "generation": generation,
                "timestamp": timestamp,
                "device_id": device_id,
                "revision": revision,
                "system": _required_text(mutation.get("system"), "mutation.system"),
                "layout_id": _required_text(mutation.get("layout_id"), "mutation.layout_id"),
                "group_id": _optional_text(mutation.get("group_id")),
                "object_id": _optional_text(mutation.get("object_id")),
                "operation": _required_operation(mutation.get("operation")),
            }
            history.append(entry)
        if len(history) > max_history:
            history = history[-max_history:]
        next_document = {
            "schema_version": SCHEMA_VERSION,
            "generation": generation,
            "history": history,
        }
        save(path, next_document)
        return generation


@contextmanager
def journal_lock(path: Path) -> Iterator[None]:
    lock_path = Path(path).with_name(".savesync-journal.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+b") as handle:
        _lock_handle(handle)
        try:
            yield
        finally:
            _unlock_handle(handle)


def _validate_document(payload: object, *, path: Path) -> dict[str, object]:
    if not isinstance(payload, dict):
        raise SaveSyncError(f"SaveSync journal is invalid: {path}")
    version = payload.get("schema_version")
    if version != SCHEMA_VERSION:
        raise SaveSyncError(
            f"SaveSync journal schema version {version!r} is not supported"
        )
    generation = payload.get("generation")
    if isinstance(generation, bool) or not isinstance(generation, int) or generation < 0:
        raise SaveSyncError("SaveSync journal generation must be a non-negative integer")
    history = payload.get("history")
    if not isinstance(history, list):
        raise SaveSyncError("SaveSync journal history must be a list")

    last_generation = 0
    normalized: list[dict[str, object]] = []
    for index, item in enumerate(history):
        if not isinstance(item, dict):
            raise SaveSyncError(f"SaveSync journal entry {index} is invalid")
        entry_generation = item.get("generation")
        if (
            isinstance(entry_generation, bool)
            or not isinstance(entry_generation, int)
            or entry_generation <= 0
        ):
            raise SaveSyncError(f"SaveSync journal entry {index} generation is invalid")
        if entry_generation <= last_generation:
            raise SaveSyncError("SaveSync journal history must be strictly increasing")
        last_generation = entry_generation
        normalized.append(
            {
                "generation": entry_generation,
                "timestamp": _required_text(item.get("timestamp"), "entry.timestamp"),
                "device_id": _required_text(item.get("device_id"), "entry.device_id"),
                "revision": _required_text(item.get("revision"), "entry.revision"),
                "system": _required_text(item.get("system"), "entry.system"),
                "layout_id": _required_text(item.get("layout_id"), "entry.layout_id"),
                "group_id": _optional_text(item.get("group_id")),
                "object_id": _optional_text(item.get("object_id")),
                "operation": _required_operation(item.get("operation")),
            }
        )

    if normalized and generation < normalized[-1]["generation"]:
        raise SaveSyncError("SaveSync journal generation is behind retained history")

    return {
        "schema_version": SCHEMA_VERSION,
        "generation": generation,
        "history": normalized,
    }


def _required_text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise SaveSyncError(f"{label} must be non-empty text")
    return value


def _optional_text(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise SaveSyncError("SaveSync journal optional text field is invalid")
    text = value.strip()
    return text or None


def _required_operation(value: object) -> str:
    if value not in {"create", "update", "delete"}:
        raise SaveSyncError("SaveSync journal operation must be create/update/delete")
    return str(value)


def _lock_handle(handle) -> None:  # noqa: ANN001
    if os.name == "nt":
        import msvcrt

        handle.seek(0)
        if handle.tell() == handle.seek(0, os.SEEK_END):
            handle.write(b"\0")
            handle.flush()
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
    else:
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)


def _unlock_handle(handle) -> None:  # noqa: ANN001
    if os.name == "nt":
        import msvcrt

        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _durable_atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
        _fsync_directory(path.parent)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)
