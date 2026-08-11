"""Conflict-aware synchronization for Batocera save/state data.

The emulator-facing tree remains local. In NAS mode a separately configured,
writable ``<remote_data.root>/saves`` dataset is canonical across devices.
Ordinary reconciliation is three-way (local, remote, last shared state), while
the explicit upload/download operations intentionally make one selected side
authoritative after preview and confirmation.
"""

from __future__ import annotations

import fcntl
import json
import shutil
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from romcloud.core.capabilities import Capability, CapabilityPolicy
from romcloud.core.exceptions import SaveSyncConnectivityError, SaveSyncVerificationError
from romcloud.core.models.savesync import (
    SaveArtifact,
    SaveChangeKind,
    SaveDiff,
    SaveDiffEntry,
    SaveReconcileAction,
    SaveReconcileEntry,
    SaveReconcilePlan,
    SaveReconcileReport,
    SaveSyncRecord,
    SaveSyncState,
)
from romcloud.core.progress import ProgressSink, emit_progress
from romcloud.core.save_ownership import (
    ALLOW_NO_AUTOMATIC_SAVES,
    ManagedSaveOwnershipPolicy,
)
from romcloud.core.save_selection import (
    DEFAULT_SAVE_SELECTION_POLICY,
    RPCS3_DEV_HDD0_PREFIX,
    RPCS3_INSTALLED_GAMES_GROUP,
    XBOX_HDD_RELATIVE_PATH,
    XBOX_SYSTEM,
    SaveSelectionPolicy,
)
from romcloud.core.storage import StorageProvider
from romcloud.infrastructure import save_tree
from romcloud.infrastructure.atomic_file import atomic_write_text
from romcloud.infrastructure.logging import get_logger
from romcloud.infrastructure.providers.local import StorageAccessResult

log = get_logger("saves")
_RPCS3_CANONICAL_PREFIX = f"ps3/{RPCS3_DEV_HDD0_PREFIX}"


@dataclass(frozen=True)
class _DestinationView:
    root: Path
    canonical_prefix: str = ""


class SaveSyncService:
    """Synchronize explicitly selected save/state content without live partial trees."""

    def __init__(
        self,
        *,
        provider: Optional[StorageProvider],
        connectivity_root: Optional[str],
        local_root: str,
        remote_root: Optional[str],
        state_path: Path,
        xbox_enabled: bool = False,
        rpcs3_installed_games_enabled: bool = False,
        include_local_games: bool = False,
        ownership_policy: ManagedSaveOwnershipPolicy = ALLOW_NO_AUTOMATIC_SAVES,
        legacy_rpcs3_root: Optional[str] = None,
        policy: SaveSelectionPolicy = DEFAULT_SAVE_SELECTION_POLICY,
        capability_policy: Optional[CapabilityPolicy] = None,
    ) -> None:
        self._provider = provider
        self._connectivity_root = connectivity_root
        self._local_root = Path(local_root)
        self._remote_root = Path(remote_root) if remote_root is not None else None
        self._state_path = Path(state_path)
        self._xbox_enabled = xbox_enabled
        self._rpcs3_installed_games_enabled = rpcs3_installed_games_enabled
        self._include_local_games = include_local_games
        self._ownership = ownership_policy
        self._legacy_rpcs3_root = (
            Path(legacy_rpcs3_root) if legacy_rpcs3_root is not None else None
        )
        self._policy = policy
        self._capabilities = capability_policy or CapabilityPolicy("smart_cache")

    # ── connectivity and settings ────────────────────────────────────────

    def is_remote_reachable(self) -> bool:
        return (
            self._provider is not None
            and self._connectivity_root is not None
            and self._provider.is_reachable(self._connectivity_root)
        )

    def validate_remote_storage(self) -> StorageAccessResult:
        if self._provider is None or self._connectivity_root is None:
            return StorageAccessResult(
                False, False, detail="ROMCloud data storage is not configured"
            )
        validate = getattr(self._provider, "validate_access", None)
        if validate is None:
            reachable = self._provider.is_reachable(self._connectivity_root)
            return StorageAccessResult(
                reachable,
                reachable,
                write_verified=reachable,
                cleanup_verified=reachable,
                detail="" if reachable else "storage location is not writable",
            )
        return validate(self._connectivity_root)

    @property
    def is_remote_configured(self) -> bool:
        return self._provider is not None and self._remote_root is not None

    @property
    def xbox_enabled(self) -> bool:
        return self._xbox_enabled

    @property
    def rpcs3_installed_games_enabled(self) -> bool:
        return self._rpcs3_installed_games_enabled

    @property
    def include_local_games(self) -> bool:
        return self._include_local_games

    def xbox_hdd_size(self) -> Optional[int]:
        path = self._local_root / XBOX_SYSTEM / XBOX_HDD_RELATIVE_PATH
        return path.stat().st_size if path.is_file() else None

    def rpcs3_installed_games_size(self) -> tuple[int, int]:
        """Return local installed-title file count and bytes without hashing."""
        future = self._local_root / "ps3" / RPCS3_DEV_HDD0_PREFIX / "game"
        legacy = (
            self._legacy_rpcs3_root / "game"
            if self._legacy_rpcs3_root is not None
            else None
        )
        root = future if future.is_dir() else legacy
        if root is None or not root.is_dir():
            return (0, 0)
        files = [path for path in root.rglob("*") if path.is_file()]
        return len(files), sum(path.stat().st_size for path in files)

    def _enabled_optional_systems(self) -> frozenset[str]:
        return frozenset({XBOX_SYSTEM}) if self._xbox_enabled else frozenset()

    def _enabled_optional_groups(self) -> frozenset[str]:
        return (
            frozenset({RPCS3_INSTALLED_GAMES_GROUP})
            if self._rpcs3_installed_games_enabled
            else frozenset()
        )

    # ── state ─────────────────────────────────────────────────────────────

    def get_state(self) -> SaveSyncState:
        state = _read_state(self._state_path)
        if not self._state_path.exists():
            _write_state(self._state_path, state)
        return state

    @contextmanager
    def _operation_lock(self):  # noqa: ANN202
        lock_path = self._state_path.with_name(".savesync.lock")
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with lock_path.open("a+", encoding="utf-8") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    # ── scanning and physical path mapping ───────────────────────────────

    def _uses_legacy_rpcs3(self) -> bool:
        if self._legacy_rpcs3_root is None:
            return False
        future = self._local_root / "ps3" / RPCS3_DEV_HDD0_PREFIX
        return not future.exists() and (
            self._legacy_rpcs3_root.exists() or self._legacy_rpcs3_root.parent.exists()
        )

    def _scan_primary(self, root: Path) -> save_tree.ScanReport:
        return save_tree.scan_tree_report(
            root,
            self._policy,
            enabled_optional_systems=self._enabled_optional_systems(),
            enabled_optional_groups=self._enabled_optional_groups(),
        )

    def _scan_local(self) -> save_tree.ScanReport:
        primary = self._scan_primary(self._local_root)
        if not self._uses_legacy_rpcs3():
            return primary
        assert self._legacy_rpcs3_root is not None
        legacy = save_tree.scan_mapped_tree_report(
            self._legacy_rpcs3_root,
            self._policy,
            system="ps3",
            relative_prefix=RPCS3_DEV_HDD0_PREFIX,
            enabled_optional_groups=self._enabled_optional_groups(),
        )
        return save_tree.merge_scan_reports(primary, legacy)

    def _scan_remote(self) -> save_tree.ScanReport:
        assert self._remote_root is not None
        return self._scan_primary(self._remote_root)

    def _automatic_report(self, report: save_tree.ScanReport) -> save_tree.ScanReport:
        if self._include_local_games:
            return report
        included = {
            path: artifact
            for path, artifact in report.artifacts.items()
            if self._ownership.is_managed_path(path)
        }
        omitted = [
            artifact
            for path, artifact in report.artifacts.items()
            if path not in included
        ]
        return save_tree.ScanReport(
            included,
            excluded_files=report.excluded_files + len(omitted),
            excluded_bytes=(
                report.excluded_bytes + sum(artifact.size_bytes for artifact in omitted)
            ),
            optional_groups=report.optional_groups,
        )

    def _scan_automatic_local(self) -> save_tree.ScanReport:
        return self._automatic_report(self._scan_local())

    def _scan_automatic_remote(self) -> save_tree.ScanReport:
        return self._automatic_report(self._scan_remote())

    def _automatic_baseline(self, state: SaveSyncState) -> dict[str, SaveArtifact]:
        baseline = _baseline_manifest(state)
        if self._include_local_games:
            return baseline
        return {
            path: artifact
            for path, artifact in baseline.items()
            if self._ownership.is_managed_path(path)
        }

    def _local_path(self, relative_path: str) -> Path:
        if self._uses_legacy_rpcs3() and relative_path.startswith(
            f"{_RPCS3_CANONICAL_PREFIX}/"
        ):
            assert self._legacy_rpcs3_root is not None
            suffix = relative_path.removeprefix(f"{_RPCS3_CANONICAL_PREFIX}/")
            return self._legacy_rpcs3_root / suffix
        return self._local_root / relative_path

    def _remote_path(self, relative_path: str) -> Path:
        assert self._remote_root is not None
        return self._remote_root / relative_path

    def _local_views(self) -> tuple[_DestinationView, ...]:
        views = [_DestinationView(self._local_root)]
        if self._uses_legacy_rpcs3():
            assert self._legacy_rpcs3_root is not None
            views.append(_DestinationView(self._legacy_rpcs3_root, _RPCS3_CANONICAL_PREFIX))
        return tuple(views)

    def _recover(self) -> None:
        for view in self._local_views():
            save_tree.recover_interrupted_commit(view.root)
        if self._remote_root is not None:
            save_tree.recover_interrupted_commit(self._remote_root)

    # ── authoritative force-operation preview ────────────────────────────

    def preview_upload(self) -> SaveDiff:
        return self._preview("upload")

    def preview_download(self) -> SaveDiff:
        return self._preview("download")

    def _preview(self, direction: str) -> SaveDiff:
        self._capabilities.require(Capability.SAVE_SYNC, f"SaveSync {direction}")
        self._require_remote()
        if not self.is_remote_reachable():
            raise SaveSyncConnectivityError(
                f"Remote save location is not reachable: {self._connectivity_root}"
            )
        self._recover()
        local_report = self._scan_local()
        remote_report = self._scan_remote()
        state = self.get_state()
        baseline = _baseline_manifest(state)
        new_side, old_side = (
            (local_report.artifacts, remote_report.artifacts)
            if direction == "upload"
            else (remote_report.artifacts, local_report.artifacts)
        )
        return SaveDiff(
            direction=direction,
            entries=_diff_entries(
                new_side,
                old_side,
                direction=direction,
                baseline=baseline,
            ),
            excluded_files=local_report.excluded_files + remote_report.excluded_files,
            excluded_bytes=local_report.excluded_bytes + remote_report.excluded_bytes,
            optional_groups=_merge_optional_groups(local_report, remote_report),
        )

    # ── ordinary three-way NAS reconciliation ────────────────────────────

    def preview_reconciliation(self) -> SaveReconcilePlan:
        self._capabilities.require(Capability.SAVE_SYNC, "Save/state reconciliation")
        self._require_remote()
        if not self.is_remote_reachable():
            raise SaveSyncConnectivityError(
                f"Remote save location is not reachable: {self._connectivity_root}"
            )
        self._recover()
        local_report = self._scan_automatic_local()
        remote_report = self._scan_automatic_remote()
        state = self.get_state()
        return _reconcile_plan(
            local_report,
            remote_report,
            self._automatic_baseline(state),
            scope=("all_eligible" if self._include_local_games else "managed_games"),
        )

    def reconcile(self, *, progress: ProgressSink = None) -> SaveReconcileReport:
        """Apply all non-conflicting changes and preserve both conflict versions."""
        with self._operation_lock():
            emit_progress(
                progress,
                "savesync",
                "preflight",
                "running",
                "Comparing local and shared save/state data",
            )
            plan = self.preview_reconciliation()
            emit_progress(
                progress,
                "savesync",
                "preflight",
                "warning" if plan.conflicts else "success",
                (
                    f"{len(plan.conflicts)} save conflict(s) preserved"
                    if plan.conflicts
                    else "Save/state preflight complete"
                ),
                metadata=plan.to_dict(),
            )
            local = self._scan_automatic_local().artifacts
            remote = self._scan_automatic_remote().artifacts
            desired_local = dict(local)
            desired_remote = dict(remote)
            for entry in plan.entries:
                if entry.action is SaveReconcileAction.UPLOAD:
                    _assign(desired_remote, entry.relative_path, entry.local)
                elif entry.action is SaveReconcileAction.DOWNLOAD:
                    _assign(desired_local, entry.relative_path, entry.remote)

            staged: list[
                tuple[_DestinationView, Path, dict[str, SaveArtifact], bool]
            ] = []
            try:
                if plan.uploads:
                    assert self._remote_root is not None
                    staged.extend(
                        self._stage_views(
                            (_DestinationView(self._remote_root),),
                            current=remote,
                            desired=desired_remote,
                            source_for=lambda path, artifact: self._choose_source(
                                path, artifact, local, remote
                            ),
                            automatic=True,
                        )
                    )
                if plan.downloads:
                    staged.extend(
                        self._stage_views(
                            self._local_views(),
                            current=local,
                            desired=desired_local,
                            source_for=lambda path, artifact: self._choose_source(
                                path, artifact, local, remote
                            ),
                            automatic=True,
                        )
                    )
                self._promote_all(staged)
            except BaseException:
                for _, staging, _, _ in staged:
                    shutil.rmtree(staging, ignore_errors=True)
                raise

            timestamp = datetime.now(timezone.utc).isoformat()
            report = SaveReconcileReport(
                revision=uuid.uuid4().hex,
                timestamp=timestamp,
                uploaded=len(plan.uploads),
                downloaded=len(plan.downloads),
                conflicts=len(plan.conflicts),
                unchanged=len(plan.unchanged),
                upload_bytes=plan.upload_bytes,
                download_bytes=plan.download_bytes,
                conflict_paths=tuple(entry.relative_path for entry in plan.conflicts),
                scope=plan.scope,
            )
            state = self.get_state()
            baseline = _reconciled_baseline(
                plan, existing=_baseline_manifest(state)
            )
            _write_state(
                self._state_path,
                SaveSyncState(
                    device_id=state.device_id,
                    last_upload=state.last_upload,
                    last_download=state.last_download,
                    shared_manifest=tuple(baseline[path] for path in sorted(baseline)),
                    last_reconcile=report,
                ),
            )
            emit_progress(
                progress,
                "savesync",
                "reconcile",
                "success",
                "Shared save/state reconciliation complete",
                metadata=report.to_dict(),
            )
            return report

    # ── deliberate force upload/download ─────────────────────────────────

    def commit_upload(
        self, diff: SaveDiff, *, progress: ProgressSink = None
    ) -> SaveSyncRecord:
        self._capabilities.require(Capability.SAVE_SYNC, "Upload All Saves")
        self._require_remote()
        assert self._remote_root is not None
        return self._commit_force(
            diff,
            source_scan=self._scan_local,
            source_path=self._local_path,
            destination_views=(_DestinationView(self._remote_root),),
            progress=progress,
        )

    def commit_download(
        self, diff: SaveDiff, *, progress: ProgressSink = None
    ) -> SaveSyncRecord:
        self._capabilities.require(Capability.SAVE_SYNC, "Download All Saves")
        self._require_remote()
        return self._commit_force(
            diff,
            source_scan=self._scan_remote,
            source_path=self._remote_path,
            destination_views=self._local_views(),
            progress=progress,
        )

    def _commit_force(
        self,
        diff: SaveDiff,
        *,
        source_scan: Callable[[], save_tree.ScanReport],
        source_path: Callable[[str], Path],
        destination_views: tuple[_DestinationView, ...],
        progress: ProgressSink,
    ) -> SaveSyncRecord:
        with self._operation_lock():
            if not self.is_remote_reachable():
                raise SaveSyncConnectivityError(
                    f"Remote save location is not reachable: {self._connectivity_root}"
                )
            # Recompute after taking the lock. A stale or forged preview can
            # never authorize replacing content that the user did not see.
            current_preview = self._preview(diff.direction)
            if current_preview.entries != diff.entries:
                raise SaveSyncVerificationError(
                    "Save/state data changed after the preview; review the operation again."
                )
            source = source_scan().artifacts
            destination = (
                self._scan_remote().artifacts
                if diff.direction == "upload"
                else self._scan_local().artifacts
            )
            emit_progress(
                progress,
                "savesync",
                "stage",
                "running",
                f"Staging {diff.direction} save/state replacement",
                metadata={"files": len(source), "bytes": sum(a.size_bytes for a in source.values())},
            )
            staged = self._stage_views(
                destination_views,
                current=destination,
                desired=source,
                source_for=lambda path, _artifact: source_path(path),
            )
            try:
                self._promote_all(staged)
            except BaseException:
                for _, staging, _, _ in staged:
                    shutil.rmtree(staging, ignore_errors=True)
                raise

            record = SaveSyncRecord(
                revision=uuid.uuid4().hex,
                timestamp=datetime.now(timezone.utc).isoformat(),
                device_id=self.get_state().device_id,
                manifest=tuple(source[path] for path in sorted(source)),
            )
            self._advance_force_state(diff.direction, record)
            emit_progress(
                progress,
                "savesync",
                "commit",
                "success",
                f"SaveSync {diff.direction} complete",
                metadata={"artifact_count": record.artifact_count, "bytes": record.total_bytes},
            )
            log.info(
                "SaveSync force %s committed: %d artifact(s), revision %s",
                diff.direction,
                record.artifact_count,
                record.revision,
            )
            return record

    def _require_remote(self) -> None:
        if not self.is_remote_configured:
            raise SaveSyncConnectivityError(
                "ROMCloud remote data storage is not configured; configure a writable "
                "destination before using SaveSync."
            )

    # ── transactional staging helpers ────────────────────────────────────

    @staticmethod
    def _path_for_view(view: _DestinationView, canonical_path: str) -> Optional[Path]:
        if view.canonical_prefix:
            prefix = f"{view.canonical_prefix}/"
            if not canonical_path.startswith(prefix):
                return None
            return Path(canonical_path.removeprefix(prefix))
        return Path(canonical_path)

    def _belongs_to_view(self, view: _DestinationView, canonical_path: str) -> bool:
        if view.canonical_prefix:
            return canonical_path.startswith(f"{view.canonical_prefix}/")
        return not (
            self._uses_legacy_rpcs3()
            and view.root == self._local_root
            and canonical_path.startswith(f"{_RPCS3_CANONICAL_PREFIX}/")
        )

    def _stage_views(
        self,
        views: tuple[_DestinationView, ...],
        *,
        current: dict[str, SaveArtifact],
        desired: dict[str, SaveArtifact],
        source_for: Callable[[str, SaveArtifact], Path],
        automatic: bool = False,
    ) -> list[tuple[_DestinationView, Path, dict[str, SaveArtifact], bool]]:
        staged: list[
            tuple[_DestinationView, Path, dict[str, SaveArtifact], bool]
        ] = []
        try:
            for view in views:
                view_current = {
                    path: artifact
                    for path, artifact in current.items()
                    if self._belongs_to_view(view, path)
                }
                view_desired = {
                    path: artifact
                    for path, artifact in desired.items()
                    if self._belongs_to_view(view, path)
                }
                if not view_current and not view_desired and not view.root.exists():
                    continue
                save_tree.recover_interrupted_commit(view.root)
                staging = save_tree.new_staging_dir(view.root)
                save_tree.clone_tree(view.root, staging)
                for canonical_path in sorted(set(view_current) | set(view_desired)):
                    relative = self._path_for_view(view, canonical_path)
                    assert relative is not None
                    staged_path = staging / relative
                    artifact = view_desired.get(canonical_path)
                    if artifact is None:
                        staged_path.unlink(missing_ok=True)
                        continue
                    existing = view_current.get(canonical_path)
                    unchanged_source = (
                        view.root / relative
                        if existing is not None and _same_artifact(existing, artifact)
                        else None
                    )
                    save_tree.materialize(
                        staged_path,
                        fresh_source=source_for(canonical_path, artifact),
                        unchanged_source=unchanged_source,
                    )
                self._verify_view(
                    staging, view, view_desired, automatic=automatic
                )
                staged.append((view, staging, view_desired, automatic))
            return staged
        except BaseException:
            for _, staging, _, _ in staged:
                shutil.rmtree(staging, ignore_errors=True)
            raise

    def _verify_view(
        self,
        staged_root: Path,
        view: _DestinationView,
        expected: dict[str, SaveArtifact],
        *,
        automatic: bool = False,
    ) -> None:
        if view.canonical_prefix:
            report = save_tree.scan_mapped_tree_report(
                staged_root,
                self._policy,
                system="ps3",
                relative_prefix=RPCS3_DEV_HDD0_PREFIX,
                enabled_optional_groups=self._enabled_optional_groups(),
            )
        else:
            report = self._scan_primary(staged_root)
            if self._uses_legacy_rpcs3() and view.root == self._local_root:
                report = save_tree.ScanReport(
                    {
                        path: artifact
                        for path, artifact in report.artifacts.items()
                        if not path.startswith(f"{_RPCS3_CANONICAL_PREFIX}/")
                    }
                )
        if automatic:
            report = self._automatic_report(report)
        if report.artifacts != expected:
            raise SaveSyncVerificationError(
                f"Staged save/state tree verification failed for {view.root}"
            )

    def _promote_all(
        self,
        staged: list[
            tuple[_DestinationView, Path, dict[str, SaveArtifact], bool]
        ],
    ) -> None:
        promotions: list[save_tree.DirectoryPromotion] = []
        try:
            for view, staging, expected, automatic in staged:
                promotion = save_tree.atomic_replace_dir(staging, view.root)
                promotions.append(promotion)
                self._verify_view(
                    view.root, view, expected, automatic=automatic
                )
        except BaseException:
            for promotion in reversed(promotions):
                save_tree.rollback_promotion(promotion)
            raise

    def _choose_source(
        self,
        relative_path: str,
        desired: SaveArtifact,
        local: dict[str, SaveArtifact],
        remote: dict[str, SaveArtifact],
    ) -> Path:
        if _same_artifact(local.get(relative_path), desired):
            return self._local_path(relative_path)
        if _same_artifact(remote.get(relative_path), desired):
            return self._remote_path(relative_path)
        raise SaveSyncVerificationError(
            f"No verified source exists for {relative_path}"
        )

    def _advance_force_state(self, direction: str, record: SaveSyncRecord) -> None:
        state = self.get_state()
        kwargs = {
            "device_id": state.device_id,
            "last_upload": record if direction == "upload" else state.last_upload,
            "last_download": record if direction == "download" else state.last_download,
            "shared_manifest": record.manifest,
            "last_reconcile": state.last_reconcile,
        }
        _write_state(self._state_path, SaveSyncState(**kwargs))


def _same_artifact(left: Optional[SaveArtifact], right: Optional[SaveArtifact]) -> bool:
    if left is None or right is None:
        return left is right
    return (
        left.relative_path == right.relative_path
        and left.size_bytes == right.size_bytes
        and left.content_hash == right.content_hash
    )


def _assign(
    manifest: dict[str, SaveArtifact], path: str, artifact: Optional[SaveArtifact]
) -> None:
    if artifact is None:
        manifest.pop(path, None)
    else:
        manifest[path] = artifact


def _is_conflict(
    local: Optional[SaveArtifact],
    remote: Optional[SaveArtifact],
    baseline: Optional[SaveArtifact],
) -> bool:
    if _same_artifact(local, remote):
        return False
    return not _same_artifact(local, baseline) and not _same_artifact(remote, baseline)


def _diff_entries(
    new_side: dict[str, SaveArtifact],
    old_side: dict[str, SaveArtifact],
    *,
    direction: str,
    baseline: dict[str, SaveArtifact],
) -> tuple[SaveDiffEntry, ...]:
    entries: list[SaveDiffEntry] = []
    for path in sorted(set(new_side) | set(old_side)):
        new_artifact = new_side.get(path)
        old_artifact = old_side.get(path)
        local, remote = (
            (new_artifact, old_artifact)
            if direction == "upload"
            else (old_artifact, new_artifact)
        )
        if new_artifact is not None and old_artifact is None:
            change = SaveChangeKind.ADDED
        elif new_artifact is None and old_artifact is not None:
            change = SaveChangeKind.REMOVED
        elif not _same_artifact(new_artifact, old_artifact):
            change = SaveChangeKind.CHANGED
        else:
            change = SaveChangeKind.UNCHANGED
        entries.append(
            SaveDiffEntry(
                relative_path=path,
                change=change,
                local=local,
                remote=remote,
                conflict=_is_conflict(local, remote, baseline.get(path)),
            )
        )
    return tuple(entries)


def _reconcile_plan(
    local_report: save_tree.ScanReport,
    remote_report: save_tree.ScanReport,
    baseline: dict[str, SaveArtifact],
    *,
    scope: str = "managed_games",
) -> SaveReconcilePlan:
    local = local_report.artifacts
    remote = remote_report.artifacts
    entries: list[SaveReconcileEntry] = []
    for path in sorted(set(local) | set(remote) | set(baseline)):
        local_artifact = local.get(path)
        remote_artifact = remote.get(path)
        base_artifact = baseline.get(path)
        if _same_artifact(local_artifact, remote_artifact):
            action = SaveReconcileAction.UNCHANGED
        elif _same_artifact(remote_artifact, base_artifact):
            action = SaveReconcileAction.UPLOAD
        elif _same_artifact(local_artifact, base_artifact):
            action = SaveReconcileAction.DOWNLOAD
        else:
            action = SaveReconcileAction.CONFLICT
        entries.append(
            SaveReconcileEntry(
                path, action, local_artifact, remote_artifact, base_artifact
            )
        )
    return SaveReconcilePlan(
        tuple(entries),
        excluded_files=local_report.excluded_files + remote_report.excluded_files,
        excluded_bytes=local_report.excluded_bytes + remote_report.excluded_bytes,
        optional_groups=_merge_optional_groups(local_report, remote_report),
        scope=scope,
    )


def _reconciled_baseline(
    plan: SaveReconcilePlan,
    *,
    existing: Optional[dict[str, SaveArtifact]] = None,
) -> dict[str, SaveArtifact]:
    baseline: dict[str, SaveArtifact] = dict(existing or {})
    for entry in plan.entries:
        if entry.action is SaveReconcileAction.CONFLICT:
            artifact = entry.baseline
        elif entry.action is SaveReconcileAction.UPLOAD:
            artifact = entry.local
        else:
            artifact = entry.remote if entry.remote is not None else entry.local
        _assign(baseline, entry.relative_path, artifact)
    return baseline


def _merge_optional_groups(*reports: save_tree.ScanReport) -> tuple[tuple[str, int, int], ...]:
    merged: dict[str, list[int]] = {}
    for report in reports:
        for group, files, size_bytes in report.optional_groups:
            stats = merged.setdefault(group, [0, 0])
            stats[0] += files
            stats[1] += size_bytes
    return tuple((group, values[0], values[1]) for group, values in sorted(merged.items()))


# ── state persistence and migration ───────────────────────────────────────


def _record_from_dict(payload: Optional[dict]) -> Optional[SaveSyncRecord]:
    if payload is None:
        return None
    try:
        return SaveSyncRecord(
            revision=payload["revision"],
            timestamp=payload["timestamp"],
            device_id=payload["device_id"],
            manifest=tuple(_artifact_from_dict(item) for item in payload.get("manifest", [])),
        )
    except (KeyError, TypeError, ValueError):
        return None


def _artifact_from_dict(payload: dict) -> SaveArtifact:
    return SaveArtifact(
        relative_path=payload["relative_path"],
        size_bytes=int(payload["size_bytes"]),
        content_hash=payload["content_hash"],
    )


def _artifact_dict(artifact: SaveArtifact) -> dict:
    return {
        "relative_path": artifact.relative_path,
        "size_bytes": artifact.size_bytes,
        "content_hash": artifact.content_hash,
    }


def _report_from_dict(payload: Optional[dict]) -> Optional[SaveReconcileReport]:
    if payload is None:
        return None
    try:
        return SaveReconcileReport(
            revision=payload["revision"],
            timestamp=payload["timestamp"],
            uploaded=int(payload.get("uploaded", 0)),
            downloaded=int(payload.get("downloaded", 0)),
            conflicts=int(payload.get("conflicts", 0)),
            unchanged=int(payload.get("unchanged", 0)),
            upload_bytes=int(payload.get("upload_bytes", 0)),
            download_bytes=int(payload.get("download_bytes", 0)),
            conflict_paths=tuple(str(path) for path in payload.get("conflict_paths", [])),
            scope=str(payload.get("scope", "managed_games")),
        )
    except (KeyError, TypeError, ValueError):
        return None


def _read_state(path: Path) -> SaveSyncState:
    if not path.exists():
        return SaveSyncState(device_id=uuid.uuid4().hex)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return SaveSyncState(device_id=uuid.uuid4().hex)
    try:
        shared = tuple(_artifact_from_dict(item) for item in data.get("shared_manifest", []))
    except (KeyError, TypeError, ValueError):
        shared = ()
    return SaveSyncState(
        device_id=data.get("device_id") or uuid.uuid4().hex,
        last_upload=_record_from_dict(data.get("last_upload")),
        last_download=_record_from_dict(data.get("last_download")),
        shared_manifest=shared,
        last_reconcile=_report_from_dict(data.get("last_reconcile")),
    )


def _record_dict(record: Optional[SaveSyncRecord]) -> Optional[dict]:
    if record is None:
        return None
    return {
        "revision": record.revision,
        "timestamp": record.timestamp,
        "device_id": record.device_id,
        "manifest": [_artifact_dict(artifact) for artifact in record.manifest],
    }


def _write_state(path: Path, state: SaveSyncState) -> None:
    payload = {
        "version": 2,
        "device_id": state.device_id,
        "last_upload": _record_dict(state.last_upload),
        "last_download": _record_dict(state.last_download),
        "shared_manifest": [_artifact_dict(artifact) for artifact in state.shared_manifest],
        "last_reconcile": state.last_reconcile.to_dict() if state.last_reconcile else None,
    }
    atomic_write_text(path, json.dumps(payload, indent=2) + "\n")


def _baseline_manifest(state: SaveSyncState) -> dict[str, SaveArtifact]:
    if state.shared_manifest:
        return {artifact.relative_path: artifact for artifact in state.shared_manifest}
    # SaveSync v1 did not persist a dedicated common ancestor. A successful
    # force operation made both selected sides identical, so the newest legacy
    # record is a safe migration baseline.
    records = [record for record in (state.last_upload, state.last_download) if record]
    if not records:
        return {}
    newest = max(records, key=lambda record: record.timestamp)
    return {artifact.relative_path: artifact for artifact in newest.manifest}
