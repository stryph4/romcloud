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
    """Scan selected content and summarize files intentionally left unmanaged."""
    if not root.is_dir():
        return ScanReport({})

    skip_dirs = policy.excluded_top_level_dirs()
    artifacts: dict[str, SaveArtifact] = {}
    excluded_files = 0
    excluded_bytes = 0
    optional: dict[str, list[int]] = {}

    for system_dir in sorted(
        p for p in root.iterdir() if p.is_dir() and not p.is_symlink()
    ):
        system = system_dir.name

        for file_path in sorted(system_dir.rglob("*")):
            if not file_path.is_file():
                continue
            if file_path.is_symlink():
                excluded_files += 1
                excluded_bytes += file_path.lstat().st_size
                continue
            size_bytes = file_path.stat().st_size
            rel_to_system = file_path.relative_to(system_dir).as_posix()
            decision = policy.classify(
                system,
                rel_to_system,
                enabled_optional_groups=enabled_optional_groups,
            )
            system_disabled = (
                system in skip_dirs
                or not policy.is_known_system(system)
                or (policy.is_optional(system) and system not in enabled_optional_systems)
            )
            if system_disabled or not decision.included:
                excluded_files += 1
                excluded_bytes += size_bytes
                group = decision.optional_group
                if group is not None and not decision.included:
                    stats = optional.setdefault(group, [0, 0])
                    stats[0] += 1
                    stats[1] += size_bytes
                continue
            relative_path = f"{system}/{rel_to_system}"
            artifacts[relative_path] = SaveArtifact(
                relative_path=relative_path,
                size_bytes=size_bytes,
                content_hash=hash_file(file_path),
            )

    # Root files do not belong to an emulator policy and remain unmanaged.
    for file_path in sorted(p for p in root.iterdir() if p.is_file()):
        excluded_files += 1
        excluded_bytes += file_path.lstat().st_size

    return ScanReport(
        artifacts,
        excluded_files=excluded_files,
        excluded_bytes=excluded_bytes,
        optional_groups=tuple(
            (group, values[0], values[1]) for group, values in sorted(optional.items())
        ),
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
    if not root.is_dir():
        return ScanReport({})
    artifacts: dict[str, SaveArtifact] = {}
    excluded_files = 0
    excluded_bytes = 0
    optional: dict[str, list[int]] = {}
    prefix = relative_prefix.strip("/")
    for file_path in sorted(root.rglob("*")):
        if not file_path.is_file():
            continue
        if file_path.is_symlink():
            excluded_files += 1
            excluded_bytes += file_path.lstat().st_size
            continue
        physical_relative = file_path.relative_to(root).as_posix()
        policy_relative = f"{prefix}/{physical_relative}"
        size_bytes = file_path.stat().st_size
        decision = policy.classify(
            system,
            policy_relative,
            enabled_optional_groups=enabled_optional_groups,
        )
        if not decision.included:
            excluded_files += 1
            excluded_bytes += size_bytes
            if decision.optional_group is not None:
                stats = optional.setdefault(decision.optional_group, [0, 0])
                stats[0] += 1
                stats[1] += size_bytes
            continue
        canonical = f"{system}/{policy_relative}"
        artifacts[canonical] = SaveArtifact(canonical, size_bytes, hash_file(file_path))
    return ScanReport(
        artifacts,
        excluded_files,
        excluded_bytes,
        tuple((group, values[0], values[1]) for group, values in sorted(optional.items())),
    )


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


def clone_tree(source: Path, destination: Path) -> None:
    """Clone a complete working tree into an empty staging directory.

    Files are hardlinked; symlinks are preserved and never followed. Staging
    is deliberately aborted when the destination filesystem cannot hardlink:
    silently falling back to copying policy-excluded RPCS3 installations or
    other large local content could duplicate hundreds of gigabytes merely to
    preserve it during a selected-data transaction.
    """
    if not source.is_dir():
        return

    def link_for_stage(src: str, dst: str) -> str:
        try:
            os.link(src, dst)
        except OSError as exc:
            raise SaveSyncError(
                "SaveSync cannot safely stage this tree because its filesystem "
                f"does not support hardlinks ({src}). No live data was changed."
            ) from exc
        return dst

    shutil.copytree(
        source,
        destination,
        copy_function=link_for_stage,
        symlinks=True,
        dirs_exist_ok=True,
    )


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
