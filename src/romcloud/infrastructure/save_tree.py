"""Local filesystem scanning/materialization for SaveSync.

Both the local save root and the configured remote SaveSync dataset are plain
local filesystem paths. For SMB, the remote path is on the independently
configured read-write ROMCloud-data mount (see
:mod:`romcloud.infrastructure.mount`). This module therefore operates on real
:class:`~pathlib.Path` objects and can keep staging beside the live dataset.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from romcloud.core.exceptions import SaveSyncError
from romcloud.core.models.savesync import SaveArtifact
from romcloud.core.save_selection import SaveSelectionPolicy

_CHUNK = 1024 * 1024


@dataclass(frozen=True)
class ScanReport:
    artifacts: dict[str, SaveArtifact]
    excluded_files: int = 0
    excluded_bytes: int = 0
    optional_groups: tuple[tuple[str, int, int], ...] = ()


@dataclass(frozen=True)
class DirectoryPromotion:
    target: Path
    previous: Optional[Path]


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
    enabled_optional_groups: frozenset[str] = frozenset(),
) -> dict[str, SaveArtifact]:
    """Return ``{relative_path: SaveArtifact}`` for every file under *root*
    the policy selects. *relative_path* is relative to *root* itself (e.g.
    ``"duckstation/memcards/shared_card_1.mcd"``).

    Returns an empty mapping if *root* does not exist yet — a brand new
    save dataset, not an error.
    """
    return scan_tree_report(
        root,
        policy,
        enabled_optional_systems=enabled_optional_systems,
        enabled_optional_groups=enabled_optional_groups,
    ).artifacts


def scan_tree_report(
    root: Path,
    policy: SaveSelectionPolicy,
    *,
    enabled_optional_systems: frozenset[str] = frozenset(),
    enabled_optional_groups: frozenset[str] = frozenset(),
) -> ScanReport:
    """Scan only roots positively resolved from the supported-layout registry.

    Unknown systems and unsupported subtrees are neither entered nor counted as
    exclusions.  This distinction is important: an empty result must not hide a
    costly recursive classification walk through arbitrary user data.
    """
    roots = policy.watch_roots(
        root,
        enabled_optional_systems=enabled_optional_systems,
    )
    return _scan_watch_roots(
        roots,
        policy,
        enabled_optional_groups=enabled_optional_groups,
    )


def scan_mapped_tree_report(
    root: Path,
    policy: SaveSelectionPolicy,
    *,
    system: str,
    relative_prefix: str,
    enabled_optional_groups: frozenset[str] = frozenset(),
) -> ScanReport:
    """Scan an emulator tree stored outside the main saves root.

    Batocera v43 keeps RPCS3 ``dev_hdd0`` under its configuration directory.
    The returned paths are mapped into the same canonical ``ps3/rpcs3/dev_hdd0``
    namespace used by newer Batocera releases and by the remote dataset.
    """
    roots = policy.watch_roots(
        root,
        canonical_prefix=f"{system}/{relative_prefix.strip('/')}",
    )
    return _scan_watch_roots(
        roots,
        policy,
        enabled_optional_groups=enabled_optional_groups,
    )


def _iter_approved_files(root: Path, *, recursive: bool):
    """Yield regular files without following symlinks from one approved root."""
    if not recursive:
        for candidate in sorted(root.iterdir(), key=lambda path: path.name):
            if candidate.is_symlink() or not candidate.is_file():
                continue
            yield candidate
        return

    for current, directories, filenames in os.walk(root, followlinks=False):
        current_path = Path(current)
        directories[:] = sorted(
            dirname
            for dirname in directories
            if not (current_path / dirname).is_symlink()
        )
        for filename in sorted(filenames):
            candidate = current_path / filename
            if candidate.is_symlink() or not candidate.is_file():
                continue
            yield candidate


def _scan_watch_roots(
    roots,
    policy: SaveSelectionPolicy,
    *,
    enabled_optional_groups: frozenset[str],
) -> ScanReport:
    artifacts: dict[str, SaveArtifact] = {}
    for watch in roots:
        for file_path in _iter_approved_files(watch.path, recursive=watch.recursive):
            relative = file_path.relative_to(watch.path).as_posix()
            canonical = f"{watch.canonical_root}/{relative}".strip("/")
            system, separator, policy_relative = canonical.partition("/")
            if not separator:
                continue
            decision = policy.classify(
                system,
                policy_relative,
                enabled_optional_groups=enabled_optional_groups,
            )
            if not decision.included or not policy.is_canonical_path_supported(canonical):
                continue
            if canonical in artifacts:
                raise SaveSyncError(f"SaveSync found duplicate canonical path: {canonical}")
            size_bytes = file_path.stat().st_size
            artifacts[canonical] = SaveArtifact(canonical, size_bytes, hash_file(file_path))
    return ScanReport(artifacts)


def merge_scan_reports(*reports: ScanReport) -> ScanReport:
    artifacts: dict[str, SaveArtifact] = {}
    optional: dict[str, list[int]] = {}
    for report in reports:
        overlap = set(artifacts).intersection(report.artifacts)
        if overlap:
            raise SaveSyncError(
                f"SaveSync found duplicate canonical paths: {', '.join(sorted(overlap)[:3])}"
            )
        artifacts.update(report.artifacts)
        for group, files, size_bytes in report.optional_groups:
            stats = optional.setdefault(group, [0, 0])
            stats[0] += files
            stats[1] += size_bytes
    return ScanReport(
        artifacts,
        sum(report.excluded_files for report in reports),
        sum(report.excluded_bytes for report in reports),
        tuple((group, values[0], values[1]) for group, values in sorted(optional.items())),
    )


def materialize(dest_path: Path, *, fresh_source: Path, unchanged_source: Optional[Path] = None) -> None:
    """Place one file's content at *dest_path*.

    Tries a hardlink from *unchanged_source* first when given — a
    provably-identical file already present on the destination filesystem,
    the "don't transfer it when unchanged" fast path — falling back to a
    full chunked copy from *fresh_source* whenever a hardlink isn't
    possible (different filesystem, missing file, or unsupported).
    """
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    # Staging trees are often hardlink clones of the live tree. Never open an
    # existing staged hardlink for writing because that would mutate live data.
    dest_path.unlink(missing_ok=True)
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
        dst.flush()
        os.fsync(dst.fileno())


def new_staging_dir(sibling_of: Path) -> Path:
    """A staging directory on the same filesystem as *sibling_of*, so the
    swap in :func:`atomic_replace_dir` can be a plain (atomic) rename."""
    sibling_of.parent.mkdir(parents=True, exist_ok=True)
    staging = sibling_of.parent / f".{sibling_of.name}.staging-{uuid.uuid4().hex[:8]}"
    staging.mkdir(parents=True, exist_ok=True)
    return staging


def recover_interrupted_commit(target_dir: Path) -> None:
    """Recover or clean transaction artifacts left by an interrupted commit.

    If the live target exists, it is authoritative: abandoned staging and
    old backup directories are removed. If the target is absent and exactly
    one backup exists, interruption happened after the old dataset was moved
    aside, so that complete dataset is atomically restored before staging is
    discarded. Multiple backups are ambiguous and are left untouched for
    manual inspection rather than guessing which save dataset is correct.
    """
    parent = target_dir.parent
    if not parent.is_dir():
        return

    staging_dirs = sorted(parent.glob(f".{target_dir.name}.staging-*"))
    stable_previous = target_dir.with_name(f"{target_dir.name}.previous")
    legacy_backups = sorted(parent.glob(f"{target_dir.name}.previous-*"))
    backup_dirs = ([stable_previous] if stable_previous.exists() else []) + legacy_backups

    if target_dir.exists():
        for path in (*staging_dirs, *legacy_backups):
            shutil.rmtree(path, ignore_errors=True)
        # Keep the stable previous generation as deliberate rollback history.
        return

    if len(backup_dirs) > 1:
        raise SaveSyncError(
            f"Cannot recover interrupted SaveSync commit for {target_dir}: "
            f"found {len(backup_dirs)} previous datasets"
        )
    if backup_dirs:
        os.rename(backup_dirs[0], target_dir)

    for path in staging_dirs:
        shutil.rmtree(path, ignore_errors=True)


def promote_staging(new_dir: Path, target_dir: Path) -> DirectoryPromotion:
    """Promote verified staging and retain one known-good previous generation."""
    target_dir.parent.mkdir(parents=True, exist_ok=True)
    previous = target_dir.with_name(f"{target_dir.name}.previous")
    if previous.exists():
        shutil.rmtree(previous)
    if not target_dir.exists():
        os.rename(new_dir, target_dir)
        return DirectoryPromotion(target_dir, None)

    os.rename(target_dir, previous)
    try:
        os.rename(new_dir, target_dir)
    except OSError as commit_error:
        try:
            os.rename(previous, target_dir)
        except OSError as restore_error:
            raise SaveSyncError(
                f"SaveSync commit failed and the previous dataset could not be restored; "
                f"it remains at {previous}: {restore_error}"
            ) from commit_error
        raise
    return DirectoryPromotion(target_dir, previous)


def rollback_promotion(promotion: DirectoryPromotion) -> None:
    """Restore the pre-promotion generation after a later transaction failure."""
    if promotion.target.exists():
        shutil.rmtree(promotion.target)
    if promotion.previous is not None and promotion.previous.exists():
        os.rename(promotion.previous, promotion.target)


def atomic_replace_dir(new_dir: Path, target_dir: Path) -> DirectoryPromotion:
    """Atomically make *target_dir* become *new_dir*'s content.

    Never leaves *target_dir* partially replaced: if it doesn't exist yet,
    a single rename makes it appear fully-formed. If it does exist, the
    old directory is renamed aside, the new one renamed into place, and
    only then is the old one removed — if that second rename fails, the
    original is restored and the error re-raised, so *target_dir* is
    always either the complete previous dataset or the complete new one,
    never a mix.
    """
    return promote_staging(new_dir, target_dir)
