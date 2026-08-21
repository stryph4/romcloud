"""Filesystem and provider-generic scanning/materialization for SaveSync.

The local save root, and the remote SaveSync dataset when its provider has
real filesystem semantics (Local, mounted SMB), are plain local filesystem
paths — this module operates on real :class:`~pathlib.Path` objects for
those and can keep staging beside the live dataset. A remote dataset served
by a protocol-only provider without filesystem semantics (e.g. SFTP) is
scanned/hashed instead through :func:`scan_provider_tree_report`, using only
the generic :meth:`~romcloud.core.storage.StorageProvider.walk`/
``open_binary`` read primitives — see
:mod:`romcloud.infrastructure.providers.sftp`. Materializing content *from*
either kind of remote source into a local destination is unaffected: the
destination is always a real local path, staged/committed exactly as today.
"""

from __future__ import annotations

import hashlib
import os
import posixpath
import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from romcloud.core.exceptions import SaveSyncError
from romcloud.core.models.savesync import SaveArtifact
from romcloud.core.save_selection import SaveSelectionPolicy
from romcloud.core.storage import StorageProvider

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


class ProviderTreeIndex:
    """Flat index of one :meth:`StorageProvider.walk` call.

    Resolves directory existence/listing for
    :meth:`~romcloud.core.save_selection.SaveSelectionPolicy.resolve_watch_roots_from_listing`
    and enumerates approved files, all from a single round-trip instead of
    per-directory I/O — important for a network provider like SFTP.
    """

    def __init__(self, entries) -> None:  # noqa: ANN001 - list[RemoteEntry]
        self._file_sizes: dict[str, int] = {}
        self._child_dirs: dict[str, set[str]] = {}
        self._known_dirs: set[str] = set()
        for entry in entries:
            if entry.is_directory:
                relative = entry.relative_path.strip("/")
                parent, _, name = relative.rpartition("/")
                if relative:
                    self._known_dirs.add(relative)
                    self._child_dirs.setdefault(parent, set()).add(name)
                continue
            relative = entry.relative_path
            self._file_sizes[relative] = entry.size_bytes or 0
            parent = ""
            for part in relative.split("/")[:-1]:
                child = f"{parent}/{part}" if parent else part
                self._known_dirs.add(child)
                self._child_dirs.setdefault(parent, set()).add(part)
                parent = child

    def dir_exists(self, relative: str) -> bool:
        return relative == "" or relative in self._known_dirs

    def list_subdirs(self, relative: str) -> tuple[str, ...]:
        return tuple(sorted(self._child_dirs.get(relative, ())))

    def files_under(
        self, relative_root: str, *, recursive: bool
    ) -> list[tuple[str, int]]:
        """Return ``(relative_to_root, size)`` for every file under
        *relative_root*, one level deep unless *recursive*."""
        prefix = f"{relative_root}/" if relative_root else ""
        results: list[tuple[str, int]] = []
        for path, size in self._file_sizes.items():
            if prefix:
                if not path.startswith(prefix):
                    continue
                remainder = path[len(prefix) :]
            else:
                remainder = path
            if not remainder or (not recursive and "/" in remainder):
                continue
            results.append((remainder, size))
        return results


def hash_provider_file(provider: StorageProvider, root: str, relative_path: str) -> str:
    """sha256 hex digest of a provider-hosted file, streamed without
    materializing a local temporary copy."""
    full_path = posixpath.join(root.rstrip("/"), relative_path) if relative_path else root
    digest = hashlib.sha256()
    with provider.open_binary(full_path) as fh:
        while True:
            chunk = fh.read(_CHUNK)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def scan_provider_tree_report(
    provider: StorageProvider,
    root: str,
    policy: SaveSelectionPolicy,
    *,
    enabled_optional_systems: frozenset[str] = frozenset(),
    enabled_optional_groups: frozenset[str] = frozenset(),
) -> ScanReport:
    """Provider-generic twin of :func:`scan_tree_report`.

    Used when the remote SaveSync dataset's provider has no real
    filesystem semantics (e.g. SFTP): enumerates the whole tree with one
    :meth:`~romcloud.core.storage.StorageProvider.walk` call, resolves the
    same dynamic-segment layouts via
    :meth:`~romcloud.core.save_selection.SaveSelectionPolicy.resolve_watch_roots_from_listing`,
    and hashes eligible files by streaming them through
    :meth:`~romcloud.core.storage.StorageProvider.open_binary` — never via
    a raw local :class:`~pathlib.Path`. Eligibility rules are identical to
    the local scanner; only the I/O primitives differ.
    """
    index = ProviderTreeIndex(provider.walk(root))
    watch_roots = policy.resolve_watch_roots_from_listing(
        index.dir_exists,
        index.list_subdirs,
        enabled_optional_systems=enabled_optional_systems,
    )
    artifacts: dict[str, SaveArtifact] = {}
    for watch in watch_roots:
        for relative, size_bytes in index.files_under(
            watch.relative_root, recursive=watch.recursive
        ):
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
            full_relative = (
                f"{watch.relative_root}/{relative}" if watch.relative_root else relative
            )
            artifacts[canonical] = SaveArtifact(
                canonical, size_bytes, hash_provider_file(provider, root, full_relative)
            )
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
