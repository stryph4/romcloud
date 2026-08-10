"""Local filesystem scanning/materialization for SaveSync.

Both the local save root and the "remote" SaveSync dataset are plain local
filesystem paths — ROMCloud has exactly one storage provider, and a
network SMB/CIFS share is always mounted locally first (see
:mod:`romcloud.infrastructure.mount`) — so this module never needs its own
provider abstraction; it only ever operates on real :class:`~pathlib.Path`
objects already reachable on this machine.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import uuid
from pathlib import Path
from typing import Optional

from romcloud.core.models.savesync import SaveArtifact
from romcloud.core.save_selection import SaveSelectionPolicy

_CHUNK = 1024 * 1024


def hash_file(path: Path) -> str:
    """sha256 hex digest of *path*'s content, streamed in chunks."""
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        while True:
            chunk = fh.read(_CHUNK)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def scan_tree(
    root: Path,
    policy: SaveSelectionPolicy,
    *,
    enabled_optional_systems: frozenset[str] = frozenset(),
) -> dict[str, SaveArtifact]:
    """Return ``{relative_path: SaveArtifact}`` for every file under *root*
    the policy selects. *relative_path* is relative to *root* itself (e.g.
    ``"duckstation/memcards/shared_card_1.mcd"``).

    Returns an empty mapping if *root* does not exist yet — a brand new
    save dataset, not an error.
    """
    if not root.is_dir():
        return {}

    skip_dirs = policy.excluded_top_level_dirs()
    artifacts: dict[str, SaveArtifact] = {}

    for system_dir in sorted(p for p in root.iterdir() if p.is_dir()):
        system = system_dir.name
        if system in skip_dirs or not policy.is_known_system(system):
            continue
        if policy.is_optional(system) and system not in enabled_optional_systems:
            continue

        for file_path in sorted(system_dir.rglob("*")):
            if not file_path.is_file():
                continue
            rel_to_system = file_path.relative_to(system_dir).as_posix()
            if not policy.is_included(system, rel_to_system):
                continue
            relative_path = f"{system}/{rel_to_system}"
            artifacts[relative_path] = SaveArtifact(
                relative_path=relative_path,
                size_bytes=file_path.stat().st_size,
                content_hash=hash_file(file_path),
            )

    return artifacts


def materialize(dest_path: Path, *, fresh_source: Path, unchanged_source: Optional[Path] = None) -> None:
    """Place one file's content at *dest_path*.

    Tries a hardlink from *unchanged_source* first when given — a
    provably-identical file already present on the destination filesystem,
    the "don't transfer it when unchanged" fast path — falling back to a
    full chunked copy from *fresh_source* whenever a hardlink isn't
    possible (different filesystem, missing file, or unsupported).
    """
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    if unchanged_source is not None:
        try:
            os.link(unchanged_source, dest_path)
            return
        except OSError:
            pass
    with fresh_source.open("rb") as src, dest_path.open("wb") as dst:
        while True:
            chunk = src.read(_CHUNK)
            if not chunk:
                break
            dst.write(chunk)


def new_staging_dir(sibling_of: Path) -> Path:
    """A staging directory on the same filesystem as *sibling_of*, so the
    swap in :func:`atomic_replace_dir` can be a plain (atomic) rename."""
    sibling_of.parent.mkdir(parents=True, exist_ok=True)
    staging = sibling_of.parent / f".{sibling_of.name}.staging-{uuid.uuid4().hex[:8]}"
    staging.mkdir(parents=True, exist_ok=True)
    return staging


def atomic_replace_dir(new_dir: Path, target_dir: Path) -> None:
    """Atomically make *target_dir* become *new_dir*'s content.

    Never leaves *target_dir* partially replaced: if it doesn't exist yet,
    a single rename makes it appear fully-formed. If it does exist, the
    old directory is renamed aside, the new one renamed into place, and
    only then is the old one removed — if that second rename fails, the
    original is restored and the error re-raised, so *target_dir* is
    always either the complete previous dataset or the complete new one,
    never a mix.
    """
    target_dir.parent.mkdir(parents=True, exist_ok=True)
    if not target_dir.exists():
        os.rename(new_dir, target_dir)
        return

    backup_dir = target_dir.with_name(f"{target_dir.name}.previous-{uuid.uuid4().hex[:8]}")
    os.rename(target_dir, backup_dir)
    try:
        os.rename(new_dir, target_dir)
    except OSError:
        os.rename(backup_dir, target_dir)
        raise
    shutil.rmtree(backup_dir, ignore_errors=True)
