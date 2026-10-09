"""Durable, path-bound evidence for ROMCloud-owned local roots.

Configuration is user-editable and therefore never constitutes deletion
authority.  Successful install/setup flows record the roots they created or
adopted in this ledger; lifecycle code compares the canonical path exactly
before authorizing recursive removal.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Mapping

from romcloud.infrastructure.atomic_file import atomic_write_text

LEDGER_FILENAME = "owned-roots.json"
SCHEMA_VERSION = 1
ROOT_KINDS = frozenset({"home", "data", "cache", "browser"})


def ledger_path(romcloud_home: Path) -> Path:
    return Path(romcloud_home) / "config" / LEDGER_FILENAME


def _canonical(path: Path) -> str:
    return os.path.realpath(os.fspath(Path(path)))


def read_owned_roots(romcloud_home: Path) -> dict[str, str]:
    path = ledger_path(romcloud_home)
    if path.is_symlink() or not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {}
    if not isinstance(payload, dict) or payload.get("schema_version") != SCHEMA_VERSION:
        return {}
    roots = payload.get("roots")
    if not isinstance(roots, dict):
        return {}
    result: dict[str, str] = {}
    for kind, raw in roots.items():
        if kind in ROOT_KINDS and isinstance(raw, str) and Path(raw).is_absolute():
            result[kind] = raw
    return result


def record_owned_roots(romcloud_home: Path, roots: Mapping[str, Path]) -> Path:
    """Merge trusted roots into the lifecycle ledger atomically.

    Callers must invoke this only after the corresponding install/setup work
    has succeeded; this helper deliberately does not infer ownership from a
    directory merely existing.
    """

    unknown = set(roots) - ROOT_KINDS
    if unknown:
        raise ValueError(f"Unsupported ROMCloud root kind(s): {sorted(unknown)}")
    home = Path(romcloud_home)
    if home.is_symlink():
        raise RuntimeError(f"Refusing ownership ledger beneath symlink: {home}")
    values = read_owned_roots(home)
    values.update({kind: _canonical(Path(path)) for kind, path in roots.items()})
    path = ledger_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(
        path,
        json.dumps({"schema_version": SCHEMA_VERSION, "roots": values}, sort_keys=True)
        + "\n",
        mode=0o600,
    )
    return path


def root_is_owned(romcloud_home: Path, kind: str, root: Path) -> bool:
    """Return positive ownership only for an exact canonical ledger match."""

    if kind not in ROOT_KINDS or Path(root).is_symlink():
        return False
    recorded = read_owned_roots(Path(romcloud_home)).get(kind)
    return recorded is not None and recorded == _canonical(Path(root))
