"""Targeted, journaled replacement of allowlisted SaveSync artifacts.

The Batocera save root is not owned by ROMCloud.  A transaction must therefore
leave unknown siblings alone *without even cloning or walking them*.  This
module stages only the artifacts supplied by the positive eligibility scan and
applies their changes with atomic file renames.  A durable local journal and a
selected-content ``.savesync-previous`` generation make an interrupted multi-view
operation recoverable on the next SaveSync invocation.

The caller remains responsible for deriving ``current`` and ``desired`` from
fresh, hashed allowlist scans.  Cached dirty hints are never accepted here as
proof of file contents.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Callable, Iterable, Optional

from romcloud.core.exceptions import SaveSyncError, SaveSyncVerificationError
from romcloud.core.models.savesync import SaveArtifact
from romcloud.infrastructure.logging import get_logger
from romcloud.infrastructure.save_tree import hash_file, materialize


_OPERATION_ID = re.compile(r"[0-9a-fA-F]{32}\Z")
_SHA256 = re.compile(r"[0-9a-fA-F]{64}\Z")
_BACKUP_RETENTION_LIMIT_PER_VIEW = 1

log = get_logger("save_transaction")


@dataclass(frozen=True)
class SelectedView:
    """One physical destination participating in a logical transaction.

    ``current`` and ``desired`` use paths relative to ``root`` (not canonical
    SaveSync paths).  This keeps legacy RPCS3 mapping out of the transaction
    engine and makes journal recovery independent of the selection registry.
    """

    root: Path
    current: dict[str, SaveArtifact]
    desired: dict[str, SaveArtifact]
    source_for: Callable[[str, SaveArtifact], Path]


@dataclass(frozen=True)
class PreparedView:
    root: Path
    stage: Path
    previous: Path
    previous_candidate: Path
    # Only changed paths are journaled/materialized in current/desired.
    current: dict[str, SaveArtifact]
    desired: dict[str, SaveArtifact]
    # The live operation retains the caller's full conservative verification
    # scope. Recovery after a restart safely falls back to the journaled delta.
    verification_current: dict[str, SaveArtifact]
    verification_desired: dict[str, SaveArtifact]


@dataclass(frozen=True)
class _RestoreAction:
    relative: str
    original: Optional[SaveArtifact]


@dataclass(frozen=True)
class TransactionMetrics:
    """Physical storage scope for one prepared SaveSync transaction.

    Counts are summed across destination views. ``staged_*`` is the new
    content materialized before promotion; ``backed_up_*`` is the old content
    copied for rollback. SaveSync keeps at most one completed rollback
    generation per destination view.
    """

    changed_files: int
    staged_files: int
    staged_bytes: int
    backed_up_files: int
    backed_up_bytes: int
    destination_views: int
    backup_retention_limit_per_view: int = _BACKUP_RETENTION_LIMIT_PER_VIEW

    @property
    def duplicated_files(self) -> int:
        return self.staged_files + self.backed_up_files

    @property
    def duplicated_bytes(self) -> int:
        return self.staged_bytes + self.backed_up_bytes

    def to_dict(self) -> dict[str, int]:
        return {
            "changed_files": self.changed_files,
            "staged_files": self.staged_files,
            "staged_bytes": self.staged_bytes,
            "backed_up_files": self.backed_up_files,
            "backed_up_bytes": self.backed_up_bytes,
            "duplicated_files": self.duplicated_files,
            "duplicated_bytes": self.duplicated_bytes,
            "destination_views": self.destination_views,
            "backup_retention_limit_per_view": (
                self.backup_retention_limit_per_view
            ),
        }


@dataclass
class SelectedTransaction:
    operation_id: str
    journal_path: Path
    views: tuple[PreparedView, ...]
    metrics: TransactionMetrics
    _finished: bool = False
    _live_touched: bool = False

    def rollback(self) -> None:
        """Restore every view to its verified pre-transaction selection."""
        if self._finished:
            return
        if not self._live_touched:
            self._cleanup(remove_journal=True)
            self._finished = True
            log.info(
                "SaveSync transaction abandoned before promotion: "
                "operation_id=%s cleanup=complete metrics=%s",
                self.operation_id,
                self.metrics.to_dict(),
            )
            return
        # Preflight every view before restoring the first byte. A third live
        # version or corrupt rollback snapshot must preserve the whole observed
        # transaction for manual resolution, not leave a partially rolled-back
        # mixture merely because the unsafe path sorted late.
        plans: list[tuple[PreparedView, tuple[_RestoreAction, ...]]] = []
        try:
            for view in reversed(self.views):
                plans.append((view, _plan_restore(view)))
        except Exception as exc:
            raise SaveSyncError(
                "SaveSync could not safely roll back its interrupted transaction; "
                f"the recovery journal was preserved: {exc}"
            ) from exc

        errors: list[str] = []
        for view, plan in plans:
            try:
                _restore_view(view, plan, operation_id=self.operation_id)
            except Exception as exc:  # noqa: BLE001 - attempt every view
                errors.append(f"{view.root}: {exc}")
        if not errors:
            self._cleanup(remove_journal=True)
            self._finished = True
            log.info(
                "SaveSync transaction rolled back: operation_id=%s "
                "cleanup=complete metrics=%s",
                self.operation_id,
                self.metrics.to_dict(),
            )
            return
        raise SaveSyncError(
            "SaveSync could not completely roll back its interrupted transaction; "
            "the recovery journal was preserved: " + "; ".join(errors)
        )

    def finalize(self) -> None:
        """Publish rollback history and clean up after a durable receipt."""
        if self._finished:
            return
        # Until this method is called, each operation's verified pre-operation
        # snapshot remains isolated in ``previous_candidate``. Preparation can
        # therefore never destroy the stable selected-content history.
        for view in self.views:
            _validate_finalize_snapshot(view)
        for view in self.views:
            _install_previous(view)
        self._cleanup(remove_journal=True)
        self._finished = True
        log.info(
            "SaveSync transaction finalized: operation_id=%s metrics=%s",
            self.operation_id,
            self.metrics.to_dict(),
        )

    def _cleanup(self, *, remove_journal: bool) -> None:
        for view in self.views:
            _remove_owned_tree(view.stage)
            if remove_journal:
                _remove_owned_tree(view.previous_candidate)
        if remove_journal:
            _durable_unlink(self.journal_path)


def prepare_transaction(
    journal_path: Path,
    views: Iterable[SelectedView],
    *,
    operation_id: Optional[str] = None,
) -> SelectedTransaction:
    """Stage and verify every desired selected tree, then write the journal.

    Live destination paths are untouched until :func:`apply_transaction`.
    Existing stable ``.savesync-previous`` directories contain only allowlisted
    files; replacing them never requires traversing the live unsupported tree.
    """
    op_id = operation_id or uuid.uuid4().hex
    _validate_operation_id(op_id)
    raw_views = tuple(views)
    if not raw_views:
        raise SaveSyncError("SaveSync transaction must contain at least one view")
    journal = Path(journal_path)
    if journal.exists() or journal.is_symlink():
        raise SaveSyncError(
            f"An unresolved SaveSync transaction journal already exists: {journal}"
        )
    prepared: list[PreparedView] = []
    prepared_sources: list[SelectedView] = []
    created_owned: list[Path] = []
    journal_created = False
    try:
        seen_roots: list[Path] = []
        for raw_view in raw_views:
            root = _validated_root(Path(raw_view.root))
            _validate_distinct_root(root, seen_roots)
            seen_roots.append(root)
            _validate_manifest(raw_view.current)
            _validate_manifest(raw_view.desired)
            changed = _changed_paths(raw_view.current, raw_view.desired)
            if not changed:
                continue
            # Broad operations may scan a whole layout or every eligible save,
            # but rollback only needs the physical paths this transaction can
            # modify. Cropping here is the final invariant that prevents a
            # one-save operation from materializing a tree-sized generation.
            current = {
                relative: raw_view.current[relative]
                for relative in changed
                if relative in raw_view.current
            }
            desired = {
                relative: raw_view.desired[relative]
                for relative in changed
                if relative in raw_view.desired
            }
            stage = root.parent / f".{root.name}.savesync-stage-{op_id}"
            previous_candidate = (
                root.parent / f".{root.name}.savesync-previous-{op_id}"
            )
            previous = root.with_name(f"{root.name}.savesync-previous")
            for owned in (stage, previous_candidate):
                if owned.exists() or owned.is_symlink():
                    raise SaveSyncError(
                        f"SaveSync transaction path already exists: {owned}"
                    )
            prepared.append(
                PreparedView(
                    root=root,
                    stage=stage,
                    previous=previous,
                    previous_candidate=previous_candidate,
                    current=current,
                    desired=desired,
                    verification_current=dict(raw_view.current),
                    verification_desired=dict(raw_view.desired),
                )
            )
            prepared_sources.append(raw_view)

        if not prepared:
            raise SaveSyncError("SaveSync transaction contains no changed paths")
        metrics = _transaction_metrics(prepared)
        transaction = SelectedTransaction(op_id, journal, tuple(prepared), metrics)
        _create_journal(transaction, phase="preparing")
        journal_created = True
        for raw_view, view in zip(prepared_sources, prepared):
            for owned in (view.stage, view.previous_candidate):
                owned.mkdir(parents=True)
                created_owned.append(owned)

            _materialize_manifest(view.stage, view.desired, raw_view.source_for)
            _verify_manifest(view.stage, view.desired)
            _materialize_manifest(
                view.previous_candidate,
                view.current,
                lambda relative, _artifact, root=view.root: root / relative,
            )
            _verify_manifest(view.previous_candidate, view.current)
            _fsync_tree(view.stage)
            _fsync_tree(view.previous_candidate)
        _write_journal(transaction, phase="prepared")
        log.info(
            "SaveSync transaction prepared: operation_id=%s metrics=%s",
            transaction.operation_id,
            transaction.metrics.to_dict(),
        )
        return transaction
    except BaseException as exc:
        cleanup_errors: list[str] = []
        for owned in reversed(created_owned):
            try:
                _remove_owned_tree(owned)
            except OSError as cleanup_error:
                cleanup_errors.append(f"{owned}: {cleanup_error}")
        if journal_created and not cleanup_errors:
            _durable_unlink(journal)
        if cleanup_errors:
            raise SaveSyncError(
                "SaveSync preparation failed and owned transaction data could not "
                "be cleaned; the recovery journal was preserved: "
                + "; ".join(cleanup_errors)
            ) from exc
        log.info(
            "SaveSync transaction preparation aborted: operation_id=%s "
            "cleanup=complete",
            op_id,
        )
        raise


def apply_transaction(
    transaction: SelectedTransaction,
    *,
    verify_current: Callable[[Path, dict[str, SaveArtifact]], None],
    verify_desired: Callable[[Path, dict[str, SaveArtifact]], None],
) -> None:
    """Apply all prepared views, rolling every view back on any failure."""
    _write_journal(transaction, phase="applying")
    try:
        # A final full positive scan closes the preview/stage window.  It also
        # detects newly-created eligible paths, not merely edits to known files.
        for view in transaction.views:
            verify_current(view.root, view.verification_current)
        for view in transaction.views:
            _apply_view(view, transaction)
        for view in transaction.views:
            verify_desired(view.root, view.verification_desired)
        for view in transaction.views:
            _fsync_changed_live_paths(view)
        _write_journal(transaction, phase="promoted")
    except BaseException:
        transaction.rollback()
        raise


def recover_transaction(
    journal_path: Path,
    *,
    allowed_roots: Iterable[Path],
    completed_operation_id: Optional[str],
) -> bool:
    """Recover a prior process interruption.

    Returns ``True`` when a journal existed.  A promoted transaction is kept
    only when durable SaveSync state contains the matching completion receipt;
    otherwise the pre-operation selected content is restored.  Corrupt or
    out-of-scope journals are preserved and fail closed for manual inspection.
    """
    path = Path(journal_path)
    if not path.exists() and not path.is_symlink():
        return False
    transaction, phase = _read_journal(path, allowed_roots=allowed_roots)
    if phase == "promoted" and completed_operation_id == transaction.operation_id:
        transaction.finalize()
        return True
    transaction.rollback()
    return True


def _apply_view(view: PreparedView, transaction: SelectedTransaction) -> None:
    changed = {
        path
        for path in set(view.current) | set(view.desired)
        if not _same_artifact(view.current.get(path), view.desired.get(path))
    }
    for relative in sorted(changed):
        target = _safe_target(view.root, relative, create_parents=True)
        before = view.current.get(relative)
        if before is not None:
            _verify_one(target, before)
        elif target.exists() or target.is_symlink():
            raise SaveSyncVerificationError(
                f"SaveSync destination changed during staging: {target}"
            )
        desired = view.desired.get(relative)
        if desired is None:
            transaction._live_touched = True
            target.unlink(missing_ok=True)
            continue
        staged = _safe_target(view.stage, relative, create_parents=False)
        _verify_one(staged, desired)
        transaction._live_touched = True
        os.replace(staged, target)


def _plan_restore(view: PreparedView) -> tuple[_RestoreAction, ...]:
    changed = {
        path
        for path in set(view.current) | set(view.desired)
        if not _same_artifact(view.current.get(path), view.desired.get(path))
    }
    actions: list[_RestoreAction] = []
    for relative in sorted(changed):
        target = _safe_target(view.root, relative, create_parents=False)
        original = view.current.get(relative)
        desired = view.desired.get(relative)
        target_matches_original = _path_matches(target, original)
        target_matches_desired = _path_matches(target, desired)
        if target_matches_original:
            continue
        if not target_matches_desired:
            raise SaveSyncError(
                "SaveSync recovery found a third, unrecognized version at "
                f"{target}; it was preserved and the journal remains unresolved."
            )
        if original is not None:
            source = _safe_target(
                view.previous_candidate, relative, create_parents=False
            )
            _verify_one(source, original)
        actions.append(_RestoreAction(relative, original))
    return tuple(actions)


def _restore_view(
    view: PreparedView,
    actions: tuple[_RestoreAction, ...],
    *,
    operation_id: str,
) -> None:
    for action in actions:
        target = _safe_target(view.root, action.relative, create_parents=True)
        if action.original is None:
            target.unlink(missing_ok=True)
            continue
        source = _safe_target(
            view.previous_candidate, action.relative, create_parents=False
        )
        restore_tmp = (
            target.parent / f".{target.name}.savesync-restore-{operation_id}"
        )
        try:
            materialize(restore_tmp, fresh_source=source)
            _verify_one(restore_tmp, action.original)
            _fsync_file(restore_tmp)
            os.replace(restore_tmp, target)
        finally:
            restore_tmp.unlink(missing_ok=True)
    _verify_manifest(view.root, view.current)
    for relative in set(view.current) | set(view.desired):
        if not _same_artifact(view.current.get(relative), view.desired.get(relative)):
            target = _safe_target(view.root, relative, create_parents=False)
            if not _path_matches(target, view.current.get(relative)):
                raise SaveSyncVerificationError(
                    f"SaveSync rollback verification failed: {target}"
                )
    _fsync_changed_live_paths(view)


def _materialize_manifest(
    root: Path,
    manifest: dict[str, SaveArtifact],
    source_for: Callable[[str, SaveArtifact], Path],
) -> None:
    for relative, artifact in sorted(manifest.items()):
        destination = _safe_target(root, relative, create_parents=True)
        source = Path(source_for(relative, artifact))
        _verify_one(source, artifact)
        # Staging/rollback generations must never share an inode with a live
        # emulator file: emulators commonly update saves in place, which would
        # otherwise mutate the verified transaction snapshot too.
        materialize(destination, fresh_source=source)


def _verify_manifest(root: Path, manifest: dict[str, SaveArtifact]) -> None:
    for relative, artifact in manifest.items():
        _verify_one(_safe_target(root, relative, create_parents=False), artifact)


def _verify_one(path: Path, artifact: SaveArtifact) -> None:
    if path.is_symlink() or not path.is_file():
        raise SaveSyncVerificationError(f"Expected staged save file is missing: {path}")
    stat = path.stat()
    if stat.st_size != artifact.size_bytes or hash_file(path) != artifact.content_hash:
        raise SaveSyncVerificationError(f"SaveSync content verification failed: {path}")


def _path_matches(path: Path, artifact: Optional[SaveArtifact]) -> bool:
    if artifact is None:
        return not path.exists() and not path.is_symlink()
    if path.is_symlink() or not path.is_file():
        return False
    try:
        return (
            path.stat().st_size == artifact.size_bytes
            and hash_file(path) == artifact.content_hash
        )
    except OSError:
        return False


def _safe_target(root: Path, relative: str, *, create_parents: bool) -> Path:
    parts = _canonical_relative_parts(relative)
    root = _validated_root(Path(root))
    if create_parents:
        root.mkdir(parents=True, exist_ok=True)
    cursor = root
    for part in parts[:-1]:
        cursor /= part
        if cursor.is_symlink():
            raise SaveSyncError(f"SaveSync refuses symlinked destination parent: {cursor}")
        if cursor.exists() and not cursor.is_dir():
            raise SaveSyncError(f"SaveSync destination parent is not a directory: {cursor}")
        if create_parents:
            cursor.mkdir(exist_ok=True)
    return root.joinpath(*parts)


def _validate_manifest(manifest: dict[str, SaveArtifact]) -> None:
    if not isinstance(manifest, dict):
        raise SaveSyncError("SaveSync view manifest must be a dictionary")
    for relative, artifact in manifest.items():
        _canonical_relative_parts(relative)
        if not isinstance(artifact, SaveArtifact):
            raise SaveSyncError("SaveSync view manifest contains an invalid artifact")
        if artifact.relative_path != relative:
            # Views receive physical-relative artifacts; callers must rewrite
            # the artifact path along with the dictionary key.
            raise SaveSyncError(
                f"SaveSync view manifest path mismatch: {relative!r}"
            )
        if (
            isinstance(artifact.size_bytes, bool)
            or not isinstance(artifact.size_bytes, int)
            or artifact.size_bytes < 0
        ):
            raise SaveSyncError(f"Invalid SaveSync artifact size for {relative!r}")
        if (
            not isinstance(artifact.content_hash, str)
            or _SHA256.fullmatch(artifact.content_hash) is None
        ):
            raise SaveSyncError(
                f"Invalid SaveSync SHA-256 content hash for {relative!r}"
            )


def _canonical_relative_parts(relative: str) -> tuple[str, ...]:
    if (
        not isinstance(relative, str)
        or not relative
        or "\\" in relative
        or "\x00" in relative
        or relative.startswith("/")
    ):
        raise SaveSyncError(f"Unsafe SaveSync relative path: {relative!r}")
    pure = PurePosixPath(relative)
    parts = pure.parts
    if (
        pure.is_absolute()
        or not parts
        or pure.as_posix() != relative
        or any(part in ("", ".", "..") for part in parts)
        or re.fullmatch(r"[A-Za-z]:", parts[0]) is not None
    ):
        raise SaveSyncError(f"Unsafe SaveSync relative path: {relative!r}")
    return parts


def _absolute_path(path: Path) -> Path:
    return Path(os.path.abspath(os.fspath(path)))


def _validated_root(path: Path) -> Path:
    root = _absolute_path(path)
    if root == root.parent:
        raise SaveSyncError(f"SaveSync destination root is too broad: {root}")
    cursor = root
    while True:
        if cursor.is_symlink():
            raise SaveSyncError(
                f"SaveSync destination root has a symlinked ancestor: {cursor}"
            )
        if cursor.exists() and not cursor.is_dir():
            raise SaveSyncError(
                f"SaveSync destination root ancestor is not a directory: {cursor}"
            )
        if cursor == cursor.parent:
            break
        cursor = cursor.parent
    return root


def _validate_distinct_root(root: Path, prior: Iterable[Path]) -> None:
    for other in prior:
        if root == other or root.is_relative_to(other) or other.is_relative_to(root):
            raise SaveSyncError(
                f"SaveSync transaction views have duplicate or overlapping roots: "
                f"{root} and {other}"
            )


def _validate_operation_id(operation_id: str) -> None:
    if not isinstance(operation_id, str) or _OPERATION_ID.fullmatch(operation_id) is None:
        raise SaveSyncError("SaveSync operation ID must be a 32-character hexadecimal token")


def _same_artifact(
    left: Optional[SaveArtifact], right: Optional[SaveArtifact]
) -> bool:
    if left is None or right is None:
        return left is right
    return left.size_bytes == right.size_bytes and left.content_hash == right.content_hash


def _changed_paths(
    current: dict[str, SaveArtifact], desired: dict[str, SaveArtifact]
) -> frozenset[str]:
    return frozenset(
        path
        for path in set(current) | set(desired)
        if not _same_artifact(current.get(path), desired.get(path))
    )


def _transaction_metrics(views: Iterable[PreparedView]) -> TransactionMetrics:
    prepared = tuple(views)
    return TransactionMetrics(
        changed_files=sum(len(set(view.current) | set(view.desired)) for view in prepared),
        staged_files=sum(len(view.desired) for view in prepared),
        staged_bytes=sum(
            artifact.size_bytes
            for view in prepared
            for artifact in view.desired.values()
        ),
        backed_up_files=sum(len(view.current) for view in prepared),
        backed_up_bytes=sum(
            artifact.size_bytes
            for view in prepared
            for artifact in view.current.values()
        ),
        destination_views=len(prepared),
    )


def _artifact_dict(artifact: SaveArtifact) -> dict[str, object]:
    return {
        "relative_path": artifact.relative_path,
        "size_bytes": artifact.size_bytes,
        "content_hash": artifact.content_hash,
    }


def _manifest_dict(manifest: dict[str, SaveArtifact]) -> list[dict[str, object]]:
    return [_artifact_dict(manifest[path]) for path in sorted(manifest)]


def _manifest_from(payload: object) -> dict[str, SaveArtifact]:
    if not isinstance(payload, list):
        raise ValueError("manifest must be a list")
    result: dict[str, SaveArtifact] = {}
    for raw in payload:
        if not isinstance(raw, dict):
            raise ValueError("artifact must be an object")
        if set(raw) != {"relative_path", "size_bytes", "content_hash"}:
            raise ValueError("artifact has missing or unexpected fields")
        relative = raw["relative_path"]
        size = raw["size_bytes"]
        digest = raw["content_hash"]
        if not isinstance(relative, str):
            raise ValueError("artifact relative_path must be a string")
        if isinstance(size, bool) or not isinstance(size, int):
            raise ValueError("artifact size_bytes must be an integer")
        if not isinstance(digest, str):
            raise ValueError("artifact content_hash must be a string")
        artifact = SaveArtifact(
            relative_path=relative,
            size_bytes=size,
            content_hash=digest,
        )
        if artifact.relative_path in result:
            raise ValueError(f"duplicate artifact path: {artifact.relative_path}")
        result[artifact.relative_path] = artifact
    _validate_manifest(result)
    return result


def _validate_finalize_snapshot(view: PreparedView) -> None:
    if view.previous_candidate.exists() or view.previous_candidate.is_symlink():
        _verify_owned_manifest_exact(view.previous_candidate, view.current)
        return
    # Recovery may resume after the candidate was already renamed but before
    # the journal was durably removed. Accept only the exact expected snapshot.
    _verify_owned_manifest_exact(view.previous, view.current)


def _install_previous(view: PreparedView) -> None:
    candidate = view.previous_candidate
    if not candidate.exists() and not candidate.is_symlink():
        _verify_owned_manifest_exact(view.previous, view.current)
        return
    _verify_owned_manifest_exact(candidate, view.current)
    if view.previous.exists() or view.previous.is_symlink():
        _remove_owned_tree(view.previous)
    os.replace(candidate, view.previous)
    _fsync_directory(view.root.parent)
    _verify_owned_manifest_exact(view.previous, view.current)


def _verify_owned_manifest_exact(
    root: Path, manifest: dict[str, SaveArtifact]
) -> None:
    root = _validated_root(root)
    if not root.is_dir():
        raise SaveSyncVerificationError(
            f"Expected SaveSync rollback snapshot is missing: {root}"
        )
    observed: set[str] = set()
    for current, directories, filenames in os.walk(root, followlinks=False):
        current_path = Path(current)
        for dirname in directories:
            if (current_path / dirname).is_symlink():
                raise SaveSyncVerificationError(
                    f"SaveSync rollback snapshot contains a symlink: "
                    f"{current_path / dirname}"
                )
        for filename in filenames:
            candidate = current_path / filename
            if candidate.is_symlink() or not candidate.is_file():
                raise SaveSyncVerificationError(
                    f"SaveSync rollback snapshot contains an unsafe entry: {candidate}"
                )
            observed.add(candidate.relative_to(root).as_posix())
    if observed != set(manifest):
        raise SaveSyncVerificationError(
            f"SaveSync rollback snapshot manifest does not match: {root}"
        )
    _verify_manifest(root, manifest)


def _fsync_changed_live_paths(view: PreparedView) -> None:
    changed = {
        path
        for path in set(view.current) | set(view.desired)
        if not _same_artifact(view.current.get(path), view.desired.get(path))
    }
    directories: set[Path] = {view.root.parent}
    for relative in changed:
        target = _safe_target(view.root, relative, create_parents=False)
        if target.is_file() and not target.is_symlink():
            _fsync_file(target)
        cursor = target.parent
        while True:
            directories.add(cursor)
            if cursor == view.root.parent:
                break
            cursor = cursor.parent
    for directory in sorted(directories, key=lambda item: len(item.parts), reverse=True):
        _fsync_directory(directory)


def _fsync_tree(root: Path) -> None:
    root = _validated_root(root)
    directories: list[Path] = []
    for current, dirnames, filenames in os.walk(root, followlinks=False):
        current_path = Path(current)
        directories.append(current_path)
        for dirname in dirnames:
            if (current_path / dirname).is_symlink():
                raise SaveSyncError(
                    f"SaveSync transaction tree contains a symlink: "
                    f"{current_path / dirname}"
                )
        for filename in filenames:
            _fsync_file(current_path / filename)
    for directory in reversed(directories):
        _fsync_directory(directory)
    _fsync_directory(root.parent)


def _fsync_file(path: Path) -> None:
    if path.is_symlink() or not path.is_file():
        raise SaveSyncVerificationError(f"Cannot durably sync SaveSync file: {path}")
    # Windows requires a writable handle for ``os.fsync``; no bytes are
    # modified, but ``rb+`` keeps the same durability barrier cross-platform.
    with path.open("rb+") as handle:
        os.fsync(handle.fileno())


def _fsync_directory(directory: Path) -> None:
    """Best-effort directory-entry durability on supporting filesystems."""
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    try:
        descriptor = os.open(directory, flags)
    except OSError:
        return
    try:
        try:
            os.fsync(descriptor)
        except OSError:
            pass
    finally:
        try:
            os.close(descriptor)
        except OSError:
            pass


def _remove_owned_tree(path: Path) -> None:
    if path.is_symlink():
        raise SaveSyncError(f"SaveSync refuses to remove a symlinked owned path: {path}")
    if not path.exists():
        return
    if not path.is_dir():
        raise SaveSyncError(f"SaveSync owned path is not a directory: {path}")
    shutil.rmtree(path)
    _fsync_directory(path.parent)


def _durable_unlink(path: Path) -> None:
    if path.is_symlink():
        raise SaveSyncError(f"SaveSync transaction journal became a symlink: {path}")
    if not path.exists():
        return
    if not path.is_file():
        raise SaveSyncError(f"SaveSync transaction journal is not a file: {path}")
    path.unlink()
    _fsync_directory(path.parent)


def _durable_atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", dir=str(path.parent)
    )
    temporary = Path(temporary_name)
    descriptor_owned = True
    try:
        handle = os.fdopen(descriptor, "w", encoding="utf-8")
        descriptor_owned = False
        with handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    except BaseException:
        if descriptor_owned:
            try:
                os.close(descriptor)
            except OSError:
                pass
        temporary.unlink(missing_ok=True)
        raise


def _journal_payload(transaction: SelectedTransaction, *, phase: str) -> dict[str, object]:
    return {
        "version": 1,
        "operation_id": transaction.operation_id,
        "phase": phase,
        "views": [
            {
                "root": str(view.root.absolute()),
                "stage": str(view.stage.absolute()),
                "previous": str(view.previous.absolute()),
                "previous_candidate": str(view.previous_candidate.absolute()),
                "current": _manifest_dict(view.current),
                "desired": _manifest_dict(view.desired),
            }
            for view in transaction.views
        ],
    }


def _create_journal(transaction: SelectedTransaction, *, phase: str) -> None:
    """Durably create, but never replace, the transaction's recovery record."""
    path = transaction.journal_path
    path.parent.mkdir(parents=True, exist_ok=True)
    content = json.dumps(
        _journal_payload(transaction, phase=phase), indent=2, sort_keys=True
    ) + "\n"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    try:
        descriptor = os.open(path, flags, 0o600)
    except FileExistsError as exc:
        raise SaveSyncError(
            f"An unresolved SaveSync transaction journal already exists: {path}"
        ) from exc
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        _fsync_directory(path.parent)
    except BaseException:
        try:
            path.unlink(missing_ok=True)
            _fsync_directory(path.parent)
        except OSError:
            pass
        raise


def _write_journal(transaction: SelectedTransaction, *, phase: str) -> None:
    path = transaction.journal_path
    try:
        if path.is_symlink() or not path.is_file():
            raise ValueError("journal is missing or is not a regular file")
        existing = json.loads(path.read_text(encoding="utf-8"))
        if (
            not isinstance(existing, dict)
            or existing.get("operation_id") != transaction.operation_id
        ):
            raise ValueError("journal is not owned by this operation")
    except (OSError, UnicodeError, ValueError) as exc:
        raise SaveSyncError(
            f"SaveSync transaction journal changed unexpectedly; it was preserved at {path}: {exc}"
        ) from exc
    content = json.dumps(
        _journal_payload(transaction, phase=phase), indent=2, sort_keys=True
    ) + "\n"
    _durable_atomic_write_text(path, content)


def _read_journal(
    path: Path, *, allowed_roots: Iterable[Path]
) -> tuple[SelectedTransaction, str]:
    try:
        if path.is_symlink() or not path.is_file():
            raise ValueError("journal is not a regular file")
        allowed = {_validated_root(Path(root)) for root in allowed_roots}
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict) or set(payload) != {
            "version",
            "operation_id",
            "phase",
            "views",
        }:
            raise ValueError("journal has missing or unexpected fields")
        version = payload["version"]
        if isinstance(version, bool) or not isinstance(version, int) or version != 1:
            raise ValueError("unsupported journal version")
        operation_id = payload["operation_id"]
        _validate_operation_id(operation_id)
        phase = payload["phase"]
        if not isinstance(phase, str):
            raise ValueError("invalid journal phase")
        if phase not in {"preparing", "prepared", "applying", "promoted"}:
            raise ValueError("invalid journal phase")
        raw_views = payload["views"]
        if not isinstance(raw_views, list) or not raw_views:
            raise ValueError("journal has no views")
        views: list[PreparedView] = []
        seen_roots: list[Path] = []
        for raw in raw_views:
            if not isinstance(raw, dict) or set(raw) != {
                "root",
                "stage",
                "previous",
                "previous_candidate",
                "current",
                "desired",
            }:
                raise ValueError("invalid journal view")
            root = _journal_absolute_path(raw["root"], "root")
            root = _validated_root(root)
            _validate_distinct_root(root, seen_roots)
            seen_roots.append(root)
            if root not in allowed:
                raise ValueError(f"journal root is outside configured SaveSync roots: {root}")
            expected_stage = root.parent / f".{root.name}.savesync-stage-{operation_id}"
            expected_previous = root.with_name(
                f"{root.name}.savesync-previous"
            )
            expected_previous_candidate = (
                root.parent
                / f".{root.name}.savesync-previous-{operation_id}"
            )
            stage = _journal_absolute_path(raw["stage"], "stage")
            previous = _journal_absolute_path(raw["previous"], "previous")
            previous_candidate = _journal_absolute_path(
                raw["previous_candidate"], "previous_candidate"
            )
            if (
                stage != expected_stage
                or previous != expected_previous
                or previous_candidate != expected_previous_candidate
            ):
                raise ValueError("journal contains an unexpected transaction path")
            current = _manifest_from(raw["current"])
            desired = _manifest_from(raw["desired"])
            views.append(
                PreparedView(
                    root,
                    stage,
                    previous,
                    previous_candidate,
                    current,
                    desired,
                    current,
                    desired,
                )
            )
    except (OSError, KeyError, TypeError, ValueError, SaveSyncError) as exc:
        raise SaveSyncError(
            f"SaveSync transaction journal is invalid; it was preserved at {path}: {exc}"
        ) from exc
    return SelectedTransaction(
        operation_id,
        path,
        tuple(views),
        _transaction_metrics(views),
        _live_touched=phase in {"applying", "promoted"},
    ), phase


def _journal_absolute_path(raw: object, label: str) -> Path:
    if not isinstance(raw, str) or not raw:
        raise ValueError(f"journal {label} must be an absolute path string")
    parsed = Path(raw)
    normalized = _absolute_path(parsed)
    if not parsed.is_absolute() or raw != str(normalized):
        raise ValueError(f"journal {label} is not a canonical absolute path")
    return normalized
