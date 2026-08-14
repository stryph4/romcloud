"""Ownership-aware discovery and removal of ROMCloud proxy files."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Iterable, Optional


def proxy_payload(path: Path) -> Optional[dict]:
    """Return a valid ROMCloud proxy payload, or ``None`` for foreign state."""
    if path.is_symlink() or path.suffix.lower() != ".romcloud":
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    if payload.get("romcloud_version") != "1":
        return None
    if not isinstance(payload.get("game_id"), str) or not payload["game_id"]:
        return None
    if not isinstance(payload.get("assets"), list):
        return None
    return payload


def is_within(path: Path, root: Path) -> bool:
    """Return whether *path* resolves within *root*, including symlink safety."""
    try:
        path.resolve().relative_to(root.resolve())
    except (OSError, ValueError):
        return False
    return True


def remove_owned_proxy_files(
    local_root: Path,
    *,
    manifest_records: Iterable[tuple[str, Path]] = (),
    keep_game_ids: Optional[set[str]] = None,
    remove_game_ids: Optional[set[str]] = None,
) -> int:
    """Remove only identity-matching ROMCloud proxies beneath *local_root*.

    Manifest paths are checked first, then the local ROM tree is scanned for
    signed orphan/duplicate proxies.  The latter matters when a legacy proxy
    file survived after its ownership row was lost or moved.  Invalid JSON,
    foreign payloads, symlinks, and paths outside the local ROM root are never
    removed.
    """
    removed: set[Path] = set()

    def selected(game_id: str) -> bool:
        if remove_game_ids is not None and game_id not in remove_game_ids:
            return False
        return keep_game_ids is None or game_id not in keep_game_ids

    for game_id, path in manifest_records:
        if not selected(game_id):
            continue
        payload = proxy_payload(path)
        if (
            payload is not None
            and payload["game_id"] == game_id
            and is_within(path, local_root)
        ):
            path.unlink(missing_ok=True)
            removed.add(path)

    if local_root.is_dir():
        for path in local_root.rglob("*.romcloud"):
            payload = proxy_payload(path)
            if (
                payload is not None
                and selected(payload["game_id"])
                and is_within(path, local_root)
            ):
                path.unlink(missing_ok=True)
                removed.add(path)

    return len(removed)
