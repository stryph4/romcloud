"""Conflict-aware synchronization for Batocera save/state data.

The emulator-facing tree remains local. A separately configured,
writable ``<remote_data.root>/saves`` dataset is canonical across devices.
Ordinary reconciliation is three-way (local, remote, last shared state), while
the explicit upload/download operations intentionally make one selected side
authoritative after preview and confirmation.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import shutil
import tempfile
import uuid
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Callable, Optional

from romcloud.core.capabilities import Capability, CapabilityPolicy
from romcloud.core.save_containers import (
    ContainerBaseline,
    ContainerSnapshot,
    EntryReplacement,
    DomainAction,
    OpaqueContainerError,
    OpaqueReason,
    RebuildPlan,
    SaveContainerAdapter,
    baseline_from_snapshot,
    plan_container_reconcile,
    target_snapshot,
)
from romcloud.core.exceptions import (
    SaveSyncConnectivityError,
    SaveSyncError,
    SaveSyncVerificationError,
    SaveSyncWriteUnavailableError,
)
from romcloud.core.models.savesync import (
    SaveArtifact,
    SaveChangeKind,
    SaveConflictRecord,
    SaveConflictResolution,
    SaveDiff,
    SaveDiffEntry,
    SaveReconcileAction,
    SaveReconcileEntry,
    SaveReconcilePlan,
    SaveReconcileReport,
    SaveSyncRecord,
    SaveSyncState,
    SaveGroupCondition,
    SaveGroupSnapshot,
    SaveGroupState,
    SaveRemoteAvailability,
    SaveRemoteObservation,
    SaveQuickSyncResult,
)
from romcloud.core.progress import ProgressSink, emit_progress
from romcloud.core.save_selection import (
    DEFAULT_SAVE_SELECTION_POLICY,
    RPCS3_DEV_HDD0_PREFIX,
    XBOX_HDD_RELATIVE_PATH,
    XBOX_SYSTEM,
    SaveGroupDescriptor,
    SaveSelectionPolicy,
)
from romcloud.core.storage import StorageAccessResult
from romcloud.core.remote_data import RemoteDataProvider
from romcloud.infrastructure import save_tree
from romcloud.infrastructure import save_transaction
from romcloud.infrastructure.logging import get_logger
from romcloud.infrastructure import savesync_state as durable_state
from romcloud.infrastructure import savesync_journal
from romcloud.infrastructure.remote_saves import RemoteSaveStore, build_remote_save_store
from romcloud.infrastructure.save_container_registry import (
    DEFAULT_SAVE_CONTAINER_REGISTRY,
    SaveContainerRegistry,
)

log = get_logger("saves")
_RPCS3_CANONICAL_PREFIX = f"ps3/{RPCS3_DEV_HDD0_PREFIX}"
_QUICK_SYNC_HISTORY_REQUIRED = 1
_CONTAINER_MERGE_ENV = "ROMCLOUD_EXPERIMENTAL_CONTAINER_SAVESYNC"


class _ScratchDir:
    """Lazily-created, always-cleaned-up local temp dir for one locked
    SaveSync operation's provider-fetched remote reads (see
    :meth:`SaveSyncService._remote_path`). Never allocated at all when the
    remote provider has real filesystem semantics."""

    def __init__(self, base: Path) -> None:
        self._base = base
        self._path: Optional[Path] = None

    def path(self) -> Path:
        if self._path is None:
            self._base.mkdir(parents=True, exist_ok=True)
            self._path = Path(
                tempfile.mkdtemp(prefix=".romcloud-remote-read-", dir=str(self._base))
            )
        return self._path

    def cleanup(self) -> None:
        if self._path is not None:
            shutil.rmtree(self._path, ignore_errors=True)
            self._path = None


@dataclass(frozen=True)
class _DestinationView:
    root: Path
    canonical_prefix: str = ""
    mapping_id: str = ""


@dataclass(frozen=True)
class _LocalMaterializationStatus:
    """Physical presence of one durable logical save generation."""

    group_id: str
    layout_id: str
    expected: int
    existing: int
    missing: int


@dataclass
class _ContainerWork:
    handled_paths: set[str]
    desired_local: dict[str, SaveArtifact]
    desired_remote: dict[str, SaveArtifact]
    local_sources: dict[str, Path]
    remote_sources: dict[str, Path]
    baselines: dict[str, ContainerBaseline]
    expected_local: dict[str, tuple[SaveContainerAdapter, Path, ContainerSnapshot]]
    expected_remote: dict[str, tuple[SaveContainerAdapter, Path, ContainerSnapshot]]
    descriptors: dict[str, SaveGroupDescriptor]
    conflicted_container_ids: set[str]
    conflict_paths: list[str]
    preview_entries: list[SaveReconcileEntry]
    invalidated_container_ids: set[str]
    uploaded: int = 0
    downloaded: int = 0
    upload_bytes: int = 0
    download_bytes: int = 0
    unchanged: int = 0
    scratch: Optional[Path] = None

    @classmethod
    def empty(cls) -> "_ContainerWork":
        return cls(set(), {}, {}, {}, {}, {}, {}, {}, {}, set(), [], [], set())

    def cleanup(self) -> None:
        if self.scratch is not None:
            shutil.rmtree(self.scratch, ignore_errors=True)
            self.scratch = None


class SaveSyncService:
    """Synchronize explicitly selected save/state content without live partial trees."""

    def __init__(
        self,
        *,
        provider: Optional[RemoteDataProvider],
        connectivity_root: Optional[object],
        local_root: str,
        remote_root: Optional[object],
        state_path: Path,
        xbox_enabled: bool = False,
        rpcs3_installed_games_enabled: bool = False,
        ownership_policy: object = None,
        legacy_rpcs3_root: Optional[str] = None,
        mapped_local_roots: tuple[tuple[str, str, str], ...] = (),
        policy: SaveSelectionPolicy = DEFAULT_SAVE_SELECTION_POLICY,
        capability_policy: Optional[CapabilityPolicy] = None,
        remote_store: Optional[RemoteSaveStore] = None,
        container_merge_enabled: Optional[bool] = None,
        container_registry: SaveContainerRegistry = DEFAULT_SAVE_CONTAINER_REGISTRY,
    ) -> None:
        self._provider = provider
        self._connectivity_root = connectivity_root
        self._local_root = Path(local_root)
        self._remote_store = remote_store or build_remote_save_store(
            provider,
            connectivity_root=connectivity_root,
            dataset_root=remote_root,
        )
        # Compatibility for callers/tests that inspect the confirmed
        # filesystem destination. Protocol/object roots are never exposed as
        # local Paths through this attribute.
        self._remote_root = (
            self._remote_store.filesystem_transaction_root
            if self._remote_store is not None
            else None
        )
        self._state_path = Path(state_path)
        self._xbox_enabled = xbox_enabled
        # Kept as accepted constructor arguments for configuration/API
        # compatibility. Eligibility is defined solely by the positive
        # save-layout registry: ordinary local games are always included, and
        # RPCS3 installed applications are never SaveSync content.
        _ = (
            rpcs3_installed_games_enabled,
            ownership_policy,
        )
        self._legacy_rpcs3_root = (
            Path(legacy_rpcs3_root) if legacy_rpcs3_root is not None else None
        )
        mapped_views: list[_DestinationView] = []
        seen_prefixes: set[str] = set()
        for mapping_id, physical_root, canonical_prefix in mapped_local_roots:
            normalized = canonical_prefix.replace("\\", "/").strip("/")
            if (
                not mapping_id
                or not normalized
                or normalized in seen_prefixes
                or PurePosixPath(normalized).as_posix() != normalized
                or any(part in {".", ".."} for part in PurePosixPath(normalized).parts)
            ):
                raise ValueError("mapped SaveSync roots require unique canonical prefixes")
            seen_prefixes.add(normalized)
            mapped_views.append(
                _DestinationView(Path(physical_root), normalized, mapping_id)
            )
        self._mapped_local_views = tuple(mapped_views)
        self._policy = policy
        self._capabilities = capability_policy or CapabilityPolicy("smart_cache")
        self._active_read_scratch: Optional[_ScratchDir] = None
        self._container_merge_enabled = (
            container_merge_enabled
            if container_merge_enabled is not None
            else os.environ.get(_CONTAINER_MERGE_ENV, "").strip().casefold()
            in {"1", "true", "yes", "on"}
        )
        self._container_registry = container_registry

    # ── connectivity and settings ────────────────────────────────────────

    def is_remote_reachable(self) -> bool:
        return self._remote_store is not None and self._remote_store.is_readable()

    def validate_remote_storage(self) -> StorageAccessResult:
        if self._remote_store is None:
            return StorageAccessResult(
                False, False, detail="ROMCloud data storage is not configured"
            )
        return self._remote_store.validate_access()

    @property
    def is_remote_configured(self) -> bool:
        return self._remote_store is not None

    @property
    def xbox_enabled(self) -> bool:
        return self._xbox_enabled

    @property
    def rpcs3_installed_games_enabled(self) -> bool:
        return False

    def xbox_hdd_size(self) -> Optional[int]:
        path = self._local_root / XBOX_SYSTEM / XBOX_HDD_RELATIVE_PATH
        return path.stat().st_size if path.is_file() else None

    def rpcs3_installed_games_size(self) -> tuple[int, int]:
        """Compatibility status: installed applications are never inspected."""
        return (0, 0)

    def _enabled_optional_systems(self) -> frozenset[str]:
        return frozenset({XBOX_SYSTEM}) if self._xbox_enabled else frozenset()

    def _enabled_optional_groups(self) -> frozenset[str]:
        return frozenset()

    def _layout_enabled(self, layout_id: str) -> bool:
        try:
            layout = self._policy.layout(layout_id)
        except KeyError:
            return False
        return (
            not layout.requires_opt_in
            or layout.system in self._enabled_optional_systems()
        )

    def _path_enabled(self, canonical_path: str) -> bool:
        descriptor = self._policy.group_for_path(canonical_path)
        return descriptor is not None and self._layout_enabled(descriptor.layout_id)

    # ── state ─────────────────────────────────────────────────────────────

    def get_state(self) -> SaveSyncState:
        """Return locked local state, creating/migrating it durably as needed."""
        return durable_state.load_state(self._state_path)

    def _get_state_unlocked(self) -> SaveSyncState:
        """Read state while the caller owns :meth:`_operation_lock`."""
        existed = self._state_path.exists()
        state = _read_state(self._state_path)
        if not existed:
            _write_state(self._state_path, state)
        return state

    def mark_local_dirty(
        self, relative_path: str, *, changed_paths: tuple[str, ...] = ()
    ) -> SaveSyncState:
        """Persist an eligible watcher hint without performing synchronization.

        A future game lifecycle watcher can call this API while the GUI is
        closed.  Registry resolution is authoritative for whether the path may
        be marked; the hint never replaces a fresh hash scan during preview.
        """
        group = self._policy.group_for_path(relative_path)
        if group is None:
            raise SaveSyncVerificationError(
                f"Path is not in a supported SaveSync layout: {relative_path}"
            )
        if not self._layout_enabled(group.layout_id):
            raise SaveSyncVerificationError(
                f"SaveSync layout is not enabled for dirty tracking: {relative_path}"
            )
        hints = changed_paths or (relative_path,)
        for path in hints:
            hint_group = self._policy.group_for_path(path)
            if hint_group is None or hint_group.group_id != group.group_id:
                raise SaveSyncVerificationError(
                    "Dirty paths must belong to the same supported SaveSync group: "
                    f"{path}"
                )
        return durable_state.SaveSyncStateStore(self._state_path).mark_local_dirty(
            group_id=group.group_id,
            layout_id=group.layout_id,
            paths=hints,
        )

    def detect_and_mark_local_changes(
        self,
        layout_ids: frozenset[str],
        *,
        changed_since: float,
    ) -> SaveSyncState:
        """Hash audited local layouts and persist changed group hints.

        This method performs no provider or remote access. Existing baselines
        are authoritative. For a group never observed before, file mtimes are
        used only to nominate a candidate; reconciliation always rescans and
        hashes both sides before making a decision.
        """
        allowed_layouts = frozenset(
            layout_id
            for layout_id in layout_ids
            if self._policy.is_lifecycle_enabled(layout_id)
            and self._layout_enabled(layout_id)
        )
        if not allowed_layouts:
            return self.get_state()
        with self._operation_lock():
            state = self._get_state_unlocked()
            current = self._scan_local_layouts(allowed_layouts).artifacts
            baseline = self._automatic_baseline(state)
            known_groups = {group.group_id for group in state.groups}
            paths_by_group: dict[str, set[str]] = {}
            descriptors = {}
            for path in sorted(set(current) | set(baseline)):
                descriptor = self._policy.group_for_path(path)
                if descriptor is None or descriptor.layout_id not in allowed_layouts:
                    continue
                paths_by_group.setdefault(descriptor.group_id, set()).add(path)
                descriptors[descriptor.group_id] = descriptor

            next_state = state
            for group_id, group_paths in sorted(paths_by_group.items()):
                descriptor = descriptors[group_id]
                local_group = _group_manifest(current, sorted(group_paths))
                baseline_group = _group_manifest(baseline, sorted(group_paths))
                has_baseline = group_id in known_groups or bool(baseline_group)
                if has_baseline:
                    changed = not _same_manifest(local_group, baseline_group)
                else:
                    changed = any(
                        _mtime_at_or_after(
                            self._local_path(artifact.relative_path),
                            changed_since - 2.0,
                        )
                        for artifact in local_group
                    )
                if not changed:
                    continue
                hints = tuple(
                    path
                    for path in sorted(group_paths)
                    if not _same_artifact(current.get(path), baseline.get(path))
                )
                next_state = durable_state.mark_local_dirty(
                    next_state,
                    group_id=group_id,
                    layout_id=descriptor.layout_id,
                    paths=hints,
                )
            if next_state != state:
                _write_state(self._state_path, next_state)
            return next_state

    def observe_local_groups(
        self, group_ids: frozenset[str]
    ) -> dict[str, SaveArtifact]:
        """Hash only the registered local layouts containing ``group_ids``.

        This is an advisory source-stability observation for the detached
        Auto SaveSync worker. Reconciliation still performs its authoritative
        preflight and post-staging verification under the operation lock.
        No provider or remote path is accessed here.
        """
        if not group_ids:
            return {}
        state = self.get_state()
        group_layouts = {
            group.group_id: group.layout_id for group in state.groups
        }
        layout_ids = frozenset(
            layout_id
            for group_id in group_ids
            for layout_id in [group_layouts.get(group_id)]
            if layout_id is not None
            and self._policy.is_lifecycle_enabled(layout_id)
            and self._layout_enabled(layout_id)
        )
        if not layout_ids:
            return {}
        report = self._scan_local_layouts(layout_ids)
        return _manifest_for_groups(report.artifacts, group_ids, self._policy)

    def acknowledge_conflict(self, conflict_id: str) -> SaveSyncState:
        """Record Review-Later acknowledgement without resolving a conflict."""
        return durable_state.SaveSyncStateStore(
            self._state_path
        ).acknowledge_conflict(conflict_id)

    def resolve_conflict(
        self,
        conflict_id: str,
        resolution: SaveConflictResolution,
        *,
        progress: ProgressSink = None,
    ) -> SaveSyncRecord:
        """Resolve one exact conflict with a verified, targeted transaction.

        The conflict's stored local/remote snapshots are the user's reviewed
        preflight. Both sides are rescanned under the operation lock before
        staging and again before commit, so an old popup can never authorize
        replacing newer content.
        """
        if resolution not in (
            SaveConflictResolution.KEEP_LOCAL,
            SaveConflictResolution.KEEP_REMOTE,
        ):
            raise SaveSyncVerificationError(
                "Conflict resolution must select the local or remote save."
            )
        direction = (
            "upload"
            if resolution is SaveConflictResolution.KEEP_LOCAL
            else "download"
        )
        self._capabilities.require(
            Capability.SAVE_SYNC,
            "Upload Local Save" if direction == "upload" else "Download Remote Save",
        )
        if direction == "upload":
            self._require_durable_remote("Upload Local Save")
        self._require_remote()
        with self._locked_operation():
            if not self.is_remote_reachable():
                raise SaveSyncConnectivityError(
                    f"Remote save location is not reachable: {self._connectivity_root}"
                )
            self._recover()
            state = self._get_state_unlocked()
            conflict = next(
                (
                    item
                    for item in state.active_conflicts
                    if item.conflict_id == conflict_id
                ),
                None,
            )
            if conflict is None:
                raise SaveSyncVerificationError(
                    "This SaveSync conflict is no longer active."
                )
            if (
                not self._layout_enabled(conflict.layout_id)
                or not self._policy.is_lifecycle_enabled(conflict.layout_id)
            ):
                raise SaveSyncVerificationError(
                    "This save layout is manual-only and cannot be resolved by Auto SaveSync."
                )

            local, remote = self._scan_conflict_sides(conflict)
            expected_local = {
                artifact.relative_path: artifact for artifact in conflict.local.artifacts
            }
            expected_remote = {
                artifact.relative_path: artifact for artifact in conflict.remote.artifacts
            }
            if local != expected_local or remote != expected_remote:
                raise SaveSyncVerificationError(
                    "Save data changed after this conflict was detected; leave it unresolved "
                    "and run Quick Sync again."
                )
            desired, destination = (
                (local, remote) if direction == "upload" else (remote, local)
            )
            source_path = self._local_path if direction == "upload" else self._remote_path
            destination_views = (
                (_DestinationView(self._remote_transaction_root()),)
                if direction == "upload"
                else self._local_views()
            )
            emit_progress(
                progress,
                "savesync",
                "stage",
                "running",
                f"Staging conflict {direction}",
                metadata={
                    "group_id": conflict.group_id,
                    "files": len(desired),
                    "bytes": sum(item.size_bytes for item in desired.values()),
                },
            )
            operation_id = uuid.uuid4().hex
            transaction = self._prepare_selected_transaction(
                destination_views,
                current=destination,
                desired=desired,
                source_for=lambda path, _artifact: source_path(path),
                operation_id=operation_id,
            )
            try:
                staged_local, staged_remote = self._scan_conflict_sides(conflict)
                if staged_local != local or staged_remote != remote:
                    raise SaveSyncVerificationError(
                        "Save data changed while staging; no replacement was kept."
                    )
                if transaction is not None:
                    self._apply_selected_transaction(transaction, destination_views)
                final_local, final_remote = self._scan_conflict_sides(conflict)
                if final_local != desired or final_remote != desired:
                    raise SaveSyncVerificationError(
                        "Resolved save group failed post-commit verification."
                    )
            except BaseException:
                if transaction is not None:
                    transaction.rollback()
                raise

            record = SaveSyncRecord(
                revision=uuid.uuid4().hex,
                timestamp=datetime.now(timezone.utc).isoformat(),
                device_id=state.device_id,
                manifest=tuple(desired[path] for path in sorted(desired)),
            )
            try:
                self._advance_conflict_resolution_state(
                    state,
                    conflict,
                    resolution,
                    record,
                    operation_id=operation_id,
                )
            except BaseException:
                if transaction is not None:
                    transaction.rollback()
                raise
            if direction == "upload":
                mutations = self._journal_mutations_for_remote_transition(
                    before=remote,
                    after=desired,
                )
                if mutations:
                    generation = self._append_remote_journal(
                        revision=record.revision,
                        timestamp=record.timestamp,
                        mutations=mutations,
                    )
                    next_state = self._get_state_unlocked()
                    if next_state.quick_sync_ready:
                        _write_state(
                            self._state_path,
                            replace(
                                next_state,
                                quick_sync_cursor_generation=generation,
                            ),
                        )
            if transaction is not None:
                try:
                    transaction.finalize()
                except OSError:
                    log.warning(
                        "Could not clean completed conflict transaction %s",
                        operation_id,
                        exc_info=True,
                    )
            emit_progress(
                progress,
                "savesync",
                "commit",
                "success",
                "SaveSync conflict resolved",
                metadata={"group_id": conflict.group_id},
            )
            return record

    def _scan_conflict_sides(
        self, conflict: SaveConflictRecord
    ) -> tuple[dict[str, SaveArtifact], dict[str, SaveArtifact]]:
        group_ids = frozenset({conflict.group_id})
        layout_ids = frozenset({conflict.layout_id})
        local = _manifest_for_groups(
            self._scan_local_layouts(layout_ids).artifacts,
            group_ids,
            self._policy,
        )
        remote = _manifest_for_groups(
            self._scan_remote_layouts(layout_ids).artifacts,
            group_ids,
            self._policy,
        )
        return local, remote

    def _advance_conflict_resolution_state(
        self,
        state: SaveSyncState,
        conflict: SaveConflictRecord,
        resolution: SaveConflictResolution,
        record: SaveSyncRecord,
        *,
        operation_id: str,
    ) -> None:
        snapshot = SaveGroupSnapshot(
            group_id=conflict.group_id,
            layout_id=conflict.layout_id,
            artifacts=record.manifest,
            observed_at=record.timestamp,
        )
        clean_group = SaveGroupState(
            group_id=conflict.group_id,
            layout_id=conflict.layout_id,
            condition=SaveGroupCondition.CLEAN,
            baseline=snapshot,
            local_observed=snapshot,
            remote_observed=snapshot,
            verified_at=record.timestamp,
        )
        groups_by_id = {
            group.group_id: group
            for group in state.groups
            if group.group_id != conflict.group_id
        }
        groups_by_id[conflict.group_id] = clean_group
        groups = tuple(groups_by_id[group_id] for group_id in sorted(groups_by_id))
        shared = {
            artifact.relative_path: artifact
            for artifact in state.shared_manifest
            if _group_id(self._policy, artifact.relative_path) != conflict.group_id
        }
        shared.update(
            {artifact.relative_path: artifact for artifact in record.manifest}
        )
        resolved = durable_state.resolve_conflict(
            state,
            conflict.conflict_id,
            resolution=resolution,
            resolution_revision=record.revision,
            resolved_at=record.timestamp,
        )
        _write_state(
            self._state_path,
            replace(
                resolved,
                last_upload=(
                    record
                    if resolution is SaveConflictResolution.KEEP_LOCAL
                    else state.last_upload
                ),
                last_download=(
                    record
                    if resolution is SaveConflictResolution.KEEP_REMOTE
                    else state.last_download
                ),
                shared_manifest=tuple(shared[path] for path in sorted(shared)),
                groups=groups,
                active_operation=None,
                last_error=None,
                last_completed_operation_id=operation_id,
            ),
        )

    def record_remote_observation(
        self, access: StorageAccessResult
    ) -> SaveSyncState:
        """Persist a completed provider-neutral availability observation."""
        observation = SaveRemoteObservation(
            availability=(
                SaveRemoteAvailability.AVAILABLE
                if access.readable
                else SaveRemoteAvailability.UNAVAILABLE
            ),
            checked_at=datetime.now(timezone.utc).isoformat(),
            detail=access.detail,
        )
        return durable_state.SaveSyncStateStore(self._state_path).update(
            lambda state: replace(state, remote_observation=observation)
        )

    def _operation_lock(self):  # noqa: ANN202
        return durable_state.state_file_lock(self._state_path)

    def _remote_has_filesystem_semantics(self) -> bool:
        return (
            self._remote_store is not None
            and self._remote_store.capabilities.filesystem_transactions
        )

    def _remote_supports_durable_transactions(self) -> bool:
        return (
            self._remote_store is not None
            and self._remote_store.capabilities.filesystem_transactions
        )

    def _require_durable_remote(self, operation: str) -> None:
        if not self._remote_supports_durable_transactions():
            raise SaveSyncWriteUnavailableError(
                f"{operation}: the configured remote-data storage does not support "
                "ROMCloud's durable write-transaction requirements. Read-only SaveSync "
                "behavior remains available."
            )
        assert self._remote_store is not None
        access = self._remote_store.validate_access()
        if not access.readable:
            raise SaveSyncConnectivityError(
                f"{operation}: remote-data storage is not readable. {access.detail}".rstrip()
            )
        if not self._remote_store.is_writable(access):
            detail = f" {access.detail}" if access.detail else ""
            raise SaveSyncWriteUnavailableError(
                f"{operation}: remote-data storage is readable but not writable.{detail}"
            )

    def _require_filesystem_remote(self, operation: str) -> None:
        """Full/Quick Sync's remote change-journal uses a real local-style
        lock file beside the remote dataset (see
        :func:`romcloud.infrastructure.savesync_journal.journal_lock`), which
        a protocol-only provider (e.g. SFTP) cannot provide. Ordinary
        reconcile/preview/commit do not depend on this and remain available.
        """
        if (
            self._remote_store is None
            or not self._remote_store.capabilities.filesystem_journal
        ):
            raise SaveSyncWriteUnavailableError(
                f"{operation}: the configured remote-data storage does not support "
                "the filesystem semantics ROMCloud's change journal requires. Use "
                "ordinary reconciliation instead."
            )

    @contextlib.contextmanager
    def _locked_operation(self):  # noqa: ANN202
        """The common choke point for every operation that may read remote
        content: holds the cross-process operation lock and, only when the
        remote provider lacks filesystem semantics, provides a scratch
        directory for :meth:`_remote_path` to fetch into — always cleaned
        up when the operation ends, success or failure."""
        with self._operation_lock():
            scratch = _ScratchDir(self._state_path.parent)
            previous = self._active_read_scratch
            self._active_read_scratch = scratch
            try:
                yield
            finally:
                scratch.cleanup()
                self._active_read_scratch = previous

    @property
    def selection_policy(self) -> SaveSelectionPolicy:
        return self._policy

    # ── scanning and physical path mapping ───────────────────────────────

    def _uses_legacy_rpcs3(self) -> bool:
        if self._legacy_rpcs3_root is None:
            return False
        future = self._local_root / "ps3" / RPCS3_DEV_HDD0_PREFIX
        return not future.exists() and (
            self._legacy_rpcs3_root.exists() or self._legacy_rpcs3_root.parent.exists()
        )

    def _scan_primary(
        self,
        root: Path,
        policy: Optional[SaveSelectionPolicy] = None,
    ) -> save_tree.ScanReport:
        return save_tree.scan_tree_report(
            root,
            policy or self._policy,
            enabled_optional_systems=self._enabled_optional_systems(),
            enabled_optional_groups=self._enabled_optional_groups(),
        )

    def _primary_local_policy(
        self, policy: SaveSelectionPolicy
    ) -> SaveSelectionPolicy:
        """Remove mapped subtrees before local discovery enters any of them."""
        prefixes = [view.canonical_prefix for view in self._mapped_local_views]
        if self._uses_legacy_rpcs3():
            prefixes.append(_RPCS3_CANONICAL_PREFIX)
        if not prefixes:
            return policy
        layouts = []
        for layout in policy.layouts:
            layout_root = "/".join(
                part
                for part in (layout.system, layout.root_pattern.strip("/"))
                if part
            )
            if any(
                layout_root == prefix or layout_root.startswith(f"{prefix}/")
                for prefix in prefixes
            ):
                continue
            layouts.append(layout)
        return SaveSelectionPolicy(layouts=tuple(layouts))

    def _without_mapped_local_prefixes(
        self, report: save_tree.ScanReport
    ) -> save_tree.ScanReport:
        prefixes = tuple(
            f"{view.canonical_prefix}/" for view in self._mapped_local_views
        )
        if self._uses_legacy_rpcs3():
            prefixes = (*prefixes, f"{_RPCS3_CANONICAL_PREFIX}/")
        if not prefixes:
            return report
        return save_tree.ScanReport(
            {
                path: artifact
                for path, artifact in report.artifacts.items()
                if not path.startswith(prefixes)
            },
            excluded_files=report.excluded_files,
            excluded_bytes=report.excluded_bytes,
            optional_groups=report.optional_groups,
        )

    def _scan_mapped_view(
        self,
        view: _DestinationView,
        policy: SaveSelectionPolicy,
        *,
        root: Optional[Path] = None,
    ) -> save_tree.ScanReport:
        system, _, relative_prefix = view.canonical_prefix.partition("/")
        return save_tree.scan_mapped_tree_report(
            view.root if root is None else root,
            policy,
            system=system,
            relative_prefix=relative_prefix,
            enabled_optional_groups=self._enabled_optional_groups(),
        )

    def _scan_local(self) -> save_tree.ScanReport:
        primary_policy = self._primary_local_policy(self._policy)
        reports = [
            self._without_mapped_local_prefixes(
                self._scan_primary(self._local_root, primary_policy)
            )
        ]
        reports.extend(
            self._scan_mapped_view(view, self._policy)
            for view in self._mapped_local_views
        )
        if self._uses_legacy_rpcs3():
            assert self._legacy_rpcs3_root is not None
            reports.append(
                self._scan_mapped_view(
                    _DestinationView(
                        self._legacy_rpcs3_root,
                        _RPCS3_CANONICAL_PREFIX,
                        "legacy-rpcs3-dev-hdd0",
                    ),
                    self._policy,
                )
            )
        result = reports[0]
        for report in reports[1:]:
            result = save_tree.merge_scan_reports(result, report)
        return result

    def _scan_local_layouts(
        self, layout_ids: frozenset[str]
    ) -> save_tree.ScanReport:
        """Scan only explicit registry roots associated with a game session."""
        layouts = tuple(
            layout
            for layout in self._policy.layouts
            if layout.layout_id in layout_ids
        )
        if not layouts:
            return save_tree.ScanReport({})
        selected_policy = SaveSelectionPolicy(layouts=layouts)
        primary_policy = self._primary_local_policy(selected_policy)
        primary = self._without_mapped_local_prefixes(
            self._scan_primary(self._local_root, primary_policy)
        )
        reports = [primary]
        selected_systems = {layout.system for layout in layouts}
        reports.extend(
            self._scan_mapped_view(view, selected_policy)
            for view in self._mapped_local_views
            if view.canonical_prefix.partition("/")[0] in selected_systems
        )
        if self._uses_legacy_rpcs3() and "ps3" in selected_systems:
            assert self._legacy_rpcs3_root is not None
            reports.append(
                self._scan_mapped_view(
                    _DestinationView(
                        self._legacy_rpcs3_root,
                        _RPCS3_CANONICAL_PREFIX,
                        "legacy-rpcs3-dev-hdd0",
                    ),
                    selected_policy,
                )
            )
        result = reports[0]
        for report in reports[1:]:
            result = save_tree.merge_scan_reports(result, report)
        return result

    def _scan_remote(self) -> save_tree.ScanReport:
        assert self._remote_store is not None
        return self._remote_store.scan(
            self._policy,
            enabled_optional_systems=self._enabled_optional_systems(),
            enabled_optional_groups=self._enabled_optional_groups(),
        )

    def _scan_remote_layouts(
        self, layout_ids: frozenset[str]
    ) -> save_tree.ScanReport:
        assert self._remote_store is not None
        layouts = tuple(
            layout
            for layout in self._policy.layouts
            if layout.layout_id in layout_ids
        )
        if not layouts:
            return save_tree.ScanReport({})
        selected_policy = SaveSelectionPolicy(layouts=layouts)
        return self._remote_store.scan(
            selected_policy,
            enabled_optional_systems=self._enabled_optional_systems(),
            enabled_optional_groups=self._enabled_optional_groups(),
        )

    def _automatic_report(self, report: save_tree.ScanReport) -> save_tree.ScanReport:
        # The supported layout is the eligibility boundary.  Catalog
        # membership and .romcloud proxy presence never filter a valid save.
        return report

    def _scan_automatic_local(self) -> save_tree.ScanReport:
        return self._automatic_report(self._scan_local())

    def _scan_automatic_remote(self) -> save_tree.ScanReport:
        return self._automatic_report(self._scan_remote())

    def _automatic_baseline(self, state: SaveSyncState) -> dict[str, SaveArtifact]:
        return {
            path: artifact
            for path, artifact in _baseline_manifest(state).items()
            if self._path_enabled(path)
        }

    def _local_path(self, relative_path: str) -> Path:
        """Resolve a canonical save path through the local destination views.

        Discovery, selected transactions, post-write verification, and direct
        source lookups all use the same view/prefix mapping.  This is especially
        important for Batocera releases whose RPCS3 tree is outside the primary
        ``/userdata/saves`` root.
        """
        destinations: list[Path] = []
        for view in self._local_views():
            if not self._belongs_to_view(view, relative_path):
                continue
            physical = self._path_for_view(view, relative_path)
            if physical is not None:
                destinations.append(view.root / physical)
        if len(destinations) != 1:
            raise SaveSyncVerificationError(
                "Canonical SaveSync path does not resolve to exactly one local "
                f"destination: {relative_path}"
            )
        return destinations[0]

    def _local_materialization_gaps(
        self,
        state: SaveSyncState,
        *,
        excluded_layout_ids: frozenset[str],
    ) -> tuple[_LocalMaterializationStatus, ...]:
        """Find clean durable generations with absent emulator-visible files.

        This deliberately performs bounded path-existence checks rather than a
        whole save-tree scan.  A gap only nominates the owning group for normal
        authoritative reconciliation; both sides are still freshly scanned and
        hashed before any winner or transaction is selected.
        """
        gaps: list[_LocalMaterializationStatus] = []
        for group in state.groups:
            if (
                group.condition is not SaveGroupCondition.CLEAN
                or group.layout_id in excluded_layout_ids
                or not self._layout_enabled(group.layout_id)
            ):
                continue
            snapshot = group.local_observed or group.baseline
            if snapshot is None or not snapshot.artifacts:
                continue
            if any(
                (descriptor := self._policy.group_for_path(artifact.relative_path))
                is None
                or descriptor.group_id != group.group_id
                or descriptor.layout_id != group.layout_id
                for artifact in snapshot.artifacts
            ):
                # Old policy state is not authority to touch a path that the
                # current positive registry no longer maps to this exact group.
                continue
            existing = 0
            for artifact in snapshot.artifacts:
                destination = self._local_path(artifact.relative_path)
                try:
                    present = not destination.is_symlink() and destination.is_file()
                except OSError:
                    present = False
                existing += int(present)
            expected = len(snapshot.artifacts)
            if existing == expected:
                continue
            gaps.append(
                _LocalMaterializationStatus(
                    group_id=group.group_id,
                    layout_id=group.layout_id,
                    expected=expected,
                    existing=existing,
                    missing=expected - existing,
                )
            )
        return tuple(gaps)

    def _remote_path(self, relative_path: str) -> Path:
        """A local, readable :class:`Path` containing *relative_path*'s
        remote content — used only as a copy *source*, never as a write
        destination (remote writes use the remote store's commit strategy).
        When the remote
        provider has real filesystem semantics this is the live mounted
        path, unchanged from before. Otherwise (e.g. SFTP) the content is
        fetched into this locked operation's scratch directory first, so
        the existing local-only staging/verification code
        (:mod:`romcloud.infrastructure.save_tree`/``save_transaction``)
        never has to open a non-local path directly.
        """
        assert self._remote_store is not None
        assert self._active_read_scratch is not None, (
            "Reading remote SaveSync content requires an active locked operation"
        )
        local_path = self._active_read_scratch.path() / f"{uuid.uuid4().hex}.tmp"
        return self._remote_store.materialize(relative_path, local_path)

    def _local_views(self) -> tuple[_DestinationView, ...]:
        views = [_DestinationView(self._local_root)]
        views.extend(self._mapped_local_views)
        if self._uses_legacy_rpcs3():
            assert self._legacy_rpcs3_root is not None
            views.append(
                _DestinationView(
                    self._legacy_rpcs3_root,
                    _RPCS3_CANONICAL_PREFIX,
                    "legacy-rpcs3-dev-hdd0",
                )
            )
        return tuple(views)

    @property
    def _transaction_journal_path(self) -> Path:
        return self._state_path.with_name("savesync-transaction.json")

    @property
    def _remote_journal_path(self) -> Optional[Path]:
        if self._remote_store is None:
            return None
        return self._remote_store.filesystem_journal_path

    def _remote_transaction_root(self) -> Path:
        if self._remote_store is None:
            raise SaveSyncWriteUnavailableError("Remote-data storage is not configured")
        root = self._remote_store.filesystem_transaction_root
        if root is None:
            raise SaveSyncWriteUnavailableError(
                "The configured remote-data provider has no filesystem commit strategy"
            )
        return root

    def _all_destination_roots(self) -> tuple[Path, ...]:
        roots = [view.root for view in self._local_views()]
        if self._remote_store is not None:
            remote_root = self._remote_store.filesystem_transaction_root
            if remote_root is not None:
                roots.append(remote_root)
        return tuple(roots)

    def _recover(self) -> None:
        # Recovery mutates transaction artifacts and must only run while the
        # cross-process SaveSync operation lock is held.
        state = self._get_state_unlocked()
        save_transaction.recover_transaction(
            self._transaction_journal_path,
            allowed_roots=self._all_destination_roots(),
            completed_operation_id=getattr(state, "last_completed_operation_id", None),
        )
        # Compatibility recovery for transaction directories created by
        # SaveSync v1/v2.  The positive scanner and all new transactions use
        # targeted selected-content staging instead.
        for view in self._local_views():
            save_tree.recover_interrupted_commit(view.root)
        if self._remote_store is not None:
            self._remote_store.recover_filesystem_dataset()

    # ── authoritative force-operation preview ────────────────────────────

    def preview_upload(self) -> SaveDiff:
        with self._locked_operation():
            return self._preview("upload")

    def preview_download(self) -> SaveDiff:
        with self._locked_operation():
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
        state = self._get_state_unlocked()
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
                policy=self._policy,
            ),
            excluded_files=local_report.excluded_files + remote_report.excluded_files,
            excluded_bytes=local_report.excluded_bytes + remote_report.excluded_bytes,
            optional_groups=_merge_optional_groups(local_report, remote_report),
        )

    def full_sync(self, *, progress: ProgressSink = None) -> SaveReconcileReport:
        """Run authoritative reconciliation and establish Quick Sync baseline."""
        self._require_remote()
        self._require_filesystem_remote("Full Sync")
        self._require_durable_remote("Full Sync")
        self._load_remote_journal(reset_on_error=True)
        report = self.reconcile(progress=progress)
        journal = self._load_remote_journal(reset_on_error=True)
        observed_generation = int(journal["generation"])
        with self._locked_operation():
            state = self._get_state_unlocked()
            _write_state(
                self._state_path,
                replace(
                    state,
                    quick_sync_ready=True,
                    quick_sync_cursor_generation=observed_generation,
                ),
            )
        return report

    def quick_sync(
        self,
        *,
        progress: ProgressSink = None,
        is_group_active: Optional[Callable[[str], bool]] = None,
        is_layout_active: Optional[Callable[[str], bool]] = None,
        exclude_layout_ids: Optional[frozenset[str]] = None,
    ) -> SaveQuickSyncResult:
        """Journal-driven discovery optimization for authoritative reconciliation."""
        self._capabilities.require(Capability.SAVE_SYNC, "Quick SaveSync")
        self._require_remote()
        self._require_filesystem_remote("Quick SaveSync")
        self._require_durable_remote("Quick SaveSync")
        if not self.is_remote_reachable():
            raise SaveSyncConnectivityError(
                f"Remote save location is not reachable: {self._connectivity_root}"
            )

        excluded_layouts = exclude_layout_ids or frozenset()
        with self._locked_operation():
            state = self._get_state_unlocked()
            cursor = state.quick_sync_cursor_generation
            known_groups = {group.group_id for group in state.groups}
            group_layouts = {
                group.group_id: group.layout_id for group in state.groups
            }
            materialization_gaps = self._local_materialization_gaps(
                state,
                excluded_layout_ids=excluded_layouts,
            )
            materialization_groups = frozenset(
                status.group_id for status in materialization_gaps
            )
            for status in materialization_gaps:
                log.warning(
                    "SaveSync local materialization gap: logical_save_id=%s "
                    "layout=%s reconciliation_decision=inspect "
                    "expected_physical_destinations=%d "
                    "existing_physical_destinations=%d "
                    "missing_physical_destinations=%d "
                    "reason=missing-emulator-visible-data",
                    status.group_id,
                    status.layout_id,
                    status.expected,
                    status.existing,
                    status.missing,
                )
            pending_groups = frozenset(
                group.group_id
                for group in state.groups
                if group.layout_id not in excluded_layouts
                and self._layout_enabled(group.layout_id)
                and (
                    group.condition
                    in {
                        SaveGroupCondition.LOCAL_DIRTY,
                        SaveGroupCondition.REMOTE_DIRTY,
                    }
                    or bool(group.dirty_path_hints)
                )
            ).union(materialization_groups)
            if not state.quick_sync_ready or cursor is None:
                return SaveQuickSyncResult(
                    status="requires-full-sync",
                    remote_generation=cursor or 0,
                    cursor_before=cursor,
                    cursor_after=cursor,
                    reason="quick-sync-baseline-missing",
                )
            journal = self._load_remote_journal(reset_on_error=False)
            if journal is None:
                return SaveQuickSyncResult(
                    status="requires-full-sync",
                    remote_generation=cursor,
                    cursor_before=cursor,
                    cursor_after=cursor,
                    reason="journal-untrustworthy",
                )
            remote_generation = int(journal["generation"])
            generation_unchanged = remote_generation == cursor
            if generation_unchanged and not pending_groups:
                return SaveQuickSyncResult(
                    status="unchanged",
                    remote_generation=remote_generation,
                    cursor_before=cursor,
                    cursor_after=cursor,
                    reason="journal-current-local-materialized",
                )
            history = list(journal["history"])
            if generation_unchanged:
                unseen: list[dict[str, object]] = []
            else:
                first_retained = (
                    int(history[0]["generation"])
                    if history
                    else remote_generation + 1
                )
                if cursor < first_retained - _QUICK_SYNC_HISTORY_REQUIRED:
                    return SaveQuickSyncResult(
                        status="requires-full-sync",
                        remote_generation=remote_generation,
                        cursor_before=cursor,
                        cursor_after=cursor,
                        reason="journal-gap",
                    )
                unseen = [
                    entry for entry in history if int(entry["generation"]) > cursor
                ]
                if not unseen and remote_generation > cursor:
                    return SaveQuickSyncResult(
                        status="requires-full-sync",
                        remote_generation=remote_generation,
                        cursor_before=cursor,
                        cursor_after=cursor,
                        reason="journal-history-incomplete",
                    )

        if generation_unchanged:
            selected_groups: Optional[frozenset[str]] = pending_groups
            selected_layouts: Optional[frozenset[str]] = None
            reason = None
        else:
            selected_groups, selected_layouts, reason = self._quick_sync_scope(
                unseen,
                known_groups=known_groups,
                exclude_layout_ids=excluded_layouts,
            )
        if reason is not None:
            return SaveQuickSyncResult(
                status="requires-full-sync",
                remote_generation=remote_generation,
                cursor_before=cursor,
                cursor_after=cursor,
                reason=reason,
            )

        if pending_groups:
            if selected_layouts is None:
                selected_groups = frozenset(selected_groups or ()).union(
                    pending_groups
                )
            else:
                # An unknown journal group already requires layout-scoped
                # discovery. Keep the union bounded to only journal-affected
                # and durable-pending registered layouts.
                group_scope = frozenset(selected_groups or ()).union(
                    pending_groups
                )
                selected_layouts = selected_layouts.union(
                    group_layouts[group_id]
                    for group_id in group_scope
                    if group_id in group_layouts
                )
                selected_groups = None

        if selected_groups == frozenset() and selected_layouts is None:
            with self._locked_operation():
                state = self._get_state_unlocked()
                _write_state(
                    self._state_path,
                    replace(
                        state,
                        quick_sync_ready=True,
                        quick_sync_cursor_generation=remote_generation,
                    ),
                )
            return SaveQuickSyncResult(
                status="unchanged",
                remote_generation=remote_generation,
                cursor_before=cursor,
                cursor_after=remote_generation,
                processed_entries=len(unseen),
                processed_groups=(),
                reason="journal-no-eligible-changes",
            )

        report = self._reconcile(
            progress=progress,
            selected_group_ids=selected_groups,
            selected_layout_ids=selected_layouts,
            upload_only=False,
            is_group_active=is_group_active,
            is_layout_active=is_layout_active,
        )
        if report is None:
            return SaveQuickSyncResult(
                status="deferred",
                remote_generation=remote_generation,
                cursor_before=cursor,
                cursor_after=cursor,
                processed_entries=len(unseen),
                processed_groups=tuple(sorted(selected_groups or frozenset())),
                reason="active-session",
            )

        with self._locked_operation():
            state = self._get_state_unlocked()
            cursor_after = max(
                remote_generation,
                state.quick_sync_cursor_generation or remote_generation,
            )
            _write_state(
                self._state_path,
                replace(
                    state,
                    quick_sync_ready=True,
                    quick_sync_cursor_generation=cursor_after,
                ),
            )
        return SaveQuickSyncResult(
            status="reconciled",
            remote_generation=remote_generation,
            cursor_before=cursor,
            cursor_after=cursor_after,
            processed_entries=len(unseen),
            processed_groups=tuple(sorted(selected_groups or frozenset())),
            report=report,
        )

    def _load_remote_journal(self, *, reset_on_error: bool) -> Optional[dict[str, object]]:
        path = self._remote_journal_path
        if path is None:
            raise SaveSyncConnectivityError("SaveSync remote location is not configured")
        with savesync_journal.journal_lock(path):
            if not reset_on_error:
                try:
                    return savesync_journal.load(path)
                except SaveSyncError:
                    return None
            try:
                return savesync_journal.load(path)
            except SaveSyncError:
                # Repairing a missing/corrupt journal writes a fresh one —
                # only ever attempted against a durable-transaction target.
                self._require_durable_remote("Full Sync")
                return savesync_journal.load_or_reset(path)

    def _quick_sync_scope(
        self,
        entries: list[dict[str, object]],
        *,
        known_groups: set[str],
        exclude_layout_ids: frozenset[str],
    ) -> tuple[Optional[frozenset[str]], Optional[frozenset[str]], Optional[str]]:
        groups: set[str] = set()
        layouts: set[str] = set()
        for entry in entries:
            layout_id = str(entry.get("layout_id") or "").strip()
            if not layout_id:
                return None, None, "journal-entry-missing-layout"
            if layout_id in exclude_layout_ids:
                continue
            if not self._layout_enabled(layout_id):
                continue
            group_id = str(entry.get("group_id") or "").strip()
            if group_id:
                if group_id in known_groups:
                    groups.add(group_id)
                else:
                    layouts.add(layout_id)
                continue
            # Ambiguous object identity: reconcile the whole affected layout.
            layouts.add(layout_id)
        if groups:
            return frozenset(groups), (frozenset(layouts) if layouts else None), None
        if layouts:
            return None, frozenset(layouts), None
        return frozenset(), None, None

    # ── ordinary three-way remote-data reconciliation ───────────────────

    def preview_reconciliation(self) -> SaveReconcilePlan:
        with self._locked_operation():
            self._capabilities.require(
                Capability.SAVE_SYNC, "Save/state reconciliation"
            )
            self._require_remote()
            if not self.is_remote_reachable():
                raise SaveSyncConnectivityError(
                    f"Remote save location is not reachable: {self._connectivity_root}"
                )
            self._recover()
            local_report = self._scan_automatic_local()
            remote_report = self._scan_automatic_remote()
            state = self._get_state_unlocked()
            baseline = self._automatic_baseline(state)
            repair_local_group_ids = frozenset(
                status.group_id
                for status in self._local_materialization_gaps(
                    state, excluded_layout_ids=frozenset()
                )
            )
            container_work = self._prepare_container_reconciliation(
                local=dict(local_report.artifacts),
                remote=dict(remote_report.artifacts),
                state=state,
                upload_only=False,
                stage_candidates=False,
                repair_local_group_ids=repair_local_group_ids,
            )
            try:
                if container_work.handled_paths:
                    local_report = replace(
                        local_report,
                        artifacts={
                            path: artifact
                            for path, artifact in local_report.artifacts.items()
                            if path not in container_work.handled_paths
                        },
                    )
                    remote_report = replace(
                        remote_report,
                        artifacts={
                            path: artifact
                            for path, artifact in remote_report.artifacts.items()
                            if path not in container_work.handled_paths
                        },
                    )
                    baseline = {
                        path: artifact
                        for path, artifact in baseline.items()
                        if path not in container_work.handled_paths
                    }
                physical = _reconcile_plan(
                    local_report,
                    remote_report,
                    baseline,
                    policy=self._policy,
                    scope="all_eligible",
                    repair_local_group_ids=repair_local_group_ids,
                )
                return SaveReconcilePlan(
                    entries=tuple((*physical.entries, *container_work.preview_entries)),
                    excluded_files=physical.excluded_files,
                    excluded_bytes=physical.excluded_bytes,
                    optional_groups=physical.optional_groups,
                    scope=physical.scope,
                )
            finally:
                container_work.cleanup()

    def reconcile(self, *, progress: ProgressSink = None) -> SaveReconcileReport:
        """Apply all non-conflicting changes and preserve both conflict versions."""
        result = self._reconcile(progress=progress)
        assert result is not None
        return result

    def reconcile_pending_groups(
        self,
        group_ids: frozenset[str],
        *,
        is_group_active: Optional[Callable[[str], bool]] = None,
        progress: ProgressSink = None,
    ) -> Optional[SaveReconcileReport]:
        """Compatibility API for an explicit upload-only dirty-group pass.

        Automatic triggers intentionally do not call this legacy surface;
        gameStop, reconnect, and menu polling all use :meth:`quick_sync` for
        normal bidirectional three-way reconciliation.  The durable group IDs
        here remain only hints about the work to inspect, and a group which
        becomes active before promotion is deferred without state or mutation.
        """
        if not group_ids:
            return None
        return self._reconcile(
            progress=progress,
            selected_group_ids=group_ids,
            upload_only=True,
            is_group_active=is_group_active,
        )

    @staticmethod
    def _container_paths(
        manifest: dict[str, SaveArtifact], container_id: str, kind: str
    ) -> set[str]:
        if kind == "file":
            return {container_id} if container_id in manifest else set()
        prefix = f"{container_id}/"
        return {path for path in manifest if path.startswith(prefix)}

    def _remote_container_path(self, container_id: str, kind: str) -> Optional[Path]:
        if kind == "directory":
            if self._remote_root is None:
                return None
            return self._remote_root / container_id
        return self._remote_path(container_id)

    @staticmethod
    def _snapshot_content(snapshot: ContainerSnapshot) -> tuple:
        return tuple(
            (
                entry.identity,
                entry.merge_domain_id,
                entry.canonical_hash,
                entry.size_bytes,
            )
            for entry in snapshot.entries
        )

    @staticmethod
    def _candidate_manifest(
        candidate: Path, *, container_id: str, kind: str
    ) -> tuple[dict[str, SaveArtifact], dict[str, Path]]:
        if kind == "file":
            artifact = SaveArtifact(
                container_id,
                candidate.stat().st_size,
                save_tree.hash_file(candidate),
            )
            return {container_id: artifact}, {container_id: candidate}
        artifacts: dict[str, SaveArtifact] = {}
        sources: dict[str, Path] = {}
        for current, directories, filenames in os.walk(candidate, followlinks=False):
            current_path = Path(current)
            if current_path.is_symlink():
                raise SaveSyncVerificationError(
                    "Container candidate contains a substituted directory"
                )
            directories[:] = sorted(
                name
                for name in directories
                if not (current_path / name).is_symlink()
            )
            for filename in sorted(filenames):
                path = current_path / filename
                if path.is_symlink() or not path.is_file():
                    raise SaveSyncVerificationError(
                        "Container candidate contains a substituted path"
                    )
                relative = path.relative_to(candidate).as_posix()
                canonical = f"{container_id}/{relative}"
                artifacts[canonical] = SaveArtifact(
                    canonical, path.stat().st_size, save_tree.hash_file(path)
                )
                sources[canonical] = path
        return artifacts, sources

    @staticmethod
    def _replace_container_manifest(
        manifest: dict[str, SaveArtifact],
        *,
        container_id: str,
        kind: str,
        replacement: dict[str, SaveArtifact],
    ) -> dict[str, SaveArtifact]:
        paths = SaveSyncService._container_paths(manifest, container_id, kind)
        result = {path: value for path, value in manifest.items() if path not in paths}
        result.update(replacement)
        return result

    def _stage_container_target(
        self,
        work: _ContainerWork,
        *,
        adapter: SaveContainerAdapter,
        destination_path: Path,
        destination_snapshot: ContainerSnapshot,
        target: ContainerSnapshot,
        local_path: Path,
        local_snapshot: ContainerSnapshot,
        remote_path: Path,
        remote_snapshot: ContainerSnapshot,
        kind: str,
        side: str,
    ) -> None:
        if self._snapshot_content(destination_snapshot) == self._snapshot_content(target):
            expected = (adapter, destination_path, target)
            if side == "local":
                work.expected_local[target.container_id] = expected
            else:
                work.expected_remote[target.container_id] = expected
            return
        if work.scratch is None:
            self._state_path.parent.mkdir(parents=True, exist_ok=True)
            work.scratch = Path(
                tempfile.mkdtemp(
                    prefix=".romcloud-container-stage-",
                    dir=str(self._state_path.parent),
                )
            )
        stage_root = work.scratch / uuid.uuid4().hex
        stage_root.mkdir()
        candidate = stage_root / ("candidate.card" if kind == "file" else "candidate")
        current = destination_snapshot.entry_map()
        replacements: list[EntryReplacement] = []
        for entry in target.entries:
            current_entry = current.get(entry.identity)
            if current_entry is not None and (
                current_entry.canonical_hash == entry.canonical_hash
                and current_entry.merge_domain_id == entry.merge_domain_id
            ):
                continue
            if any(
                value.identity == entry.identity
                and value.canonical_hash == entry.canonical_hash
                and value.merge_domain_id == entry.merge_domain_id
                for value in local_snapshot.entries
            ):
                source_path, source_snapshot = local_path, local_snapshot
            elif any(
                value.identity == entry.identity
                and value.canonical_hash == entry.canonical_hash
                and value.merge_domain_id == entry.merge_domain_id
                for value in remote_snapshot.entries
            ):
                source_path, source_snapshot = remote_path, remote_snapshot
            else:
                raise SaveSyncVerificationError(
                    "No verified logical source exists for a container entry"
                )
            replacements.append(
                EntryReplacement(
                    entry,
                    adapter.extract_entry(source_path, source_snapshot, entry.identity),
                )
            )
        removals = tuple(
            identity for identity in current if identity not in target.entry_map()
        )
        adapter.rebuild(
            destination_path,
            RebuildPlan(
                expected=target,
                replacements=tuple(replacements),
                removals=tuple(sorted(removals)),
            ),
            candidate,
        )
        candidate_manifest, candidate_sources = self._candidate_manifest(
            candidate, container_id=target.container_id, kind=kind
        )
        if side == "local":
            work.desired_local = self._replace_container_manifest(
                work.desired_local,
                container_id=target.container_id,
                kind=kind,
                replacement=candidate_manifest,
            )
            work.local_sources.update(candidate_sources)
            work.expected_local[target.container_id] = (adapter, destination_path, target)
        else:
            work.desired_remote = self._replace_container_manifest(
                work.desired_remote,
                container_id=target.container_id,
                kind=kind,
                replacement=candidate_manifest,
            )
            work.remote_sources.update(candidate_sources)
            work.expected_remote[target.container_id] = (adapter, destination_path, target)

    def _prepare_container_reconciliation(
        self,
        *,
        local: dict[str, SaveArtifact],
        remote: dict[str, SaveArtifact],
        state: SaveSyncState,
        upload_only: bool,
        stage_candidates: bool = True,
        repair_local_group_ids: frozenset[str] = frozenset(),
    ) -> _ContainerWork:
        work = _ContainerWork.empty()
        work.desired_local = dict(local)
        work.desired_remote = dict(remote)
        if not self._container_merge_enabled or upload_only:
            return work
        baselines = {
            baseline.container_id: baseline for baseline in state.container_baselines
        }
        descriptors: dict[str, SaveGroupDescriptor] = {}
        for path in sorted(set(local).union(remote)):
            descriptor = self._policy.group_for_path(path)
            if (
                descriptor is not None
                and descriptor.container_id
                and descriptor.container_adapter_id
            ):
                descriptors.setdefault(descriptor.container_id, descriptor)

        for container_id, descriptor in descriptors.items():
            if descriptor.group_id in repair_local_group_ids:
                # Preserve the 0.9.32 materialization rule: a durable clean
                # physical generation missing from the emulator-visible tree
                # is repaired by the existing physical transaction path.
                continue
            adapter = self._container_registry.get(descriptor.container_adapter_id)
            if adapter is None:
                continue
            local_paths = self._container_paths(local, container_id, descriptor.container_kind)
            remote_paths = self._container_paths(remote, container_id, descriptor.container_kind)
            if not local_paths or not remote_paths:
                continue
            local_path = self._local_root / container_id
            remote_path = self._remote_container_path(
                container_id, descriptor.container_kind
            )
            if remote_path is None:
                continue
            checkpoint = (
                set(work.handled_paths),
                dict(work.desired_local),
                dict(work.desired_remote),
                dict(work.local_sources),
                dict(work.remote_sources),
                dict(work.baselines),
                dict(work.expected_local),
                dict(work.expected_remote),
                dict(work.descriptors),
                set(work.conflicted_container_ids),
                list(work.conflict_paths),
                list(work.preview_entries),
                set(work.invalidated_container_ids),
                work.uploaded,
                work.downloaded,
                work.upload_bytes,
                work.download_bytes,
                work.unchanged,
            )
            try:
                local_snapshot = adapter.snapshot(local_path, container_id=container_id)
                remote_snapshot = adapter.snapshot(remote_path, container_id=container_id)
                logical_plan = plan_container_reconcile(
                    local_snapshot,
                    remote_snapshot,
                    baselines.get(container_id),
                )
                local_target = target_snapshot(
                    logical_plan, local_snapshot, remote_snapshot, side="local"
                )
                remote_target = target_snapshot(
                    logical_plan, local_snapshot, remote_snapshot, side="remote"
                )
                work.handled_paths.update(local_paths)
                work.handled_paths.update(remote_paths)
                work.baselines[container_id] = logical_plan.next_baseline
                work.descriptors[container_id] = descriptor
                if logical_plan.conflicts:
                    work.conflicted_container_ids.add(container_id)
                    for conflict in logical_plan.conflicts:
                        fingerprint = hashlib.sha256(
                            conflict.merge_domain_id.encode("utf-8")
                        ).hexdigest()[:16]
                        work.conflict_paths.append(
                            f"{container_id}/logical-conflict-{fingerprint}"
                        )
                for decision in logical_plan.decisions:
                    domain_fingerprint = hashlib.sha256(
                        decision.merge_domain_id.encode("utf-8")
                    ).hexdigest()[:16]
                    logical_path = (
                        f"{container_id}/logical-domain-{domain_fingerprint}"
                    )

                    def logical_artifact(domain):
                        if domain is None:
                            return None
                        digest = hashlib.sha256()
                        for entry in domain.entries:
                            digest.update(entry.identity.encode("utf-8"))
                            digest.update(entry.canonical_hash.encode("ascii"))
                            digest.update(entry.size_bytes.to_bytes(8, "little"))
                        return SaveArtifact(
                            logical_path,
                            sum(entry.size_bytes for entry in domain.entries),
                            digest.hexdigest(),
                        )

                    action = {
                        DomainAction.USE_LOCAL: SaveReconcileAction.UPLOAD,
                        DomainAction.USE_REMOTE: SaveReconcileAction.DOWNLOAD,
                        DomainAction.CONFLICT: SaveReconcileAction.CONFLICT,
                        DomainAction.CONVERGED: SaveReconcileAction.UNCHANGED,
                    }[decision.action]
                    work.preview_entries.append(
                        SaveReconcileEntry(
                            relative_path=logical_path,
                            action=action,
                            local=logical_artifact(decision.local),
                            remote=logical_artifact(decision.remote),
                            baseline=logical_artifact(decision.baseline),
                        )
                    )
                    if decision.action is DomainAction.USE_LOCAL:
                        work.uploaded += 1
                        if decision.local is not None:
                            work.upload_bytes += sum(
                                entry.size_bytes for entry in decision.local.entries
                            )
                    elif decision.action is DomainAction.USE_REMOTE:
                        work.downloaded += 1
                        if decision.remote is not None:
                            work.download_bytes += sum(
                                entry.size_bytes for entry in decision.remote.entries
                            )
                    elif decision.action is DomainAction.CONVERGED:
                        work.unchanged += 1
                if not stage_candidates:
                    continue
                self._stage_container_target(
                    work,
                    adapter=adapter,
                    destination_path=local_path,
                    destination_snapshot=local_snapshot,
                    target=local_target,
                    local_path=local_path,
                    local_snapshot=local_snapshot,
                    remote_path=remote_path,
                    remote_snapshot=remote_snapshot,
                    kind=descriptor.container_kind,
                    side="local",
                )
                self._stage_container_target(
                    work,
                    adapter=adapter,
                    destination_path=remote_path,
                    destination_snapshot=remote_snapshot,
                    target=remote_target,
                    local_path=local_path,
                    local_snapshot=local_snapshot,
                    remote_path=remote_path,
                    remote_snapshot=remote_snapshot,
                    kind=descriptor.container_kind,
                    side="remote",
                )
            except OpaqueContainerError as exc:
                (
                    work.handled_paths,
                    work.desired_local,
                    work.desired_remote,
                    work.local_sources,
                    work.remote_sources,
                    work.baselines,
                    work.expected_local,
                    work.expected_remote,
                    work.descriptors,
                    work.conflicted_container_ids,
                    work.conflict_paths,
                    work.preview_entries,
                    work.invalidated_container_ids,
                    work.uploaded,
                    work.downloaded,
                    work.upload_bytes,
                    work.download_bytes,
                    work.unchanged,
                ) = checkpoint
                if exc.reason is OpaqueReason.ADAPTER_VERSION_MISMATCH:
                    work.invalidated_container_ids.add(container_id)
                fingerprint = hashlib.sha256(container_id.encode("utf-8")).hexdigest()[:12]
                log.info(
                    "SaveSync container fallback: adapter=%s container=%s reason=%s",
                    descriptor.container_adapter_id,
                    fingerprint,
                    exc.reason.value,
                )
                continue
        return work

    def _verify_container_targets(self, work: _ContainerWork) -> None:
        for values in (work.expected_local, work.expected_remote):
            for adapter, path, expected in values.values():
                actual = adapter.snapshot(path, container_id=expected.container_id)
                if self._snapshot_content(actual) != self._snapshot_content(expected):
                    raise SaveSyncVerificationError(
                        "Promoted container does not match its expected logical snapshot"
                    )

    def _reconcile(
        self,
        *,
        progress: ProgressSink = None,
        selected_group_ids: Optional[frozenset[str]] = None,
        selected_layout_ids: Optional[frozenset[str]] = None,
        upload_only: bool = False,
        is_group_active: Optional[Callable[[str], bool]] = None,
        is_layout_active: Optional[Callable[[str], bool]] = None,
    ) -> Optional[SaveReconcileReport]:
        with self._locked_operation():
            if selected_group_ids is not None and selected_layout_ids is not None:
                selected_layout_ids = frozenset(selected_layout_ids)
            if selected_group_ids is not None:
                selected_group_ids = frozenset(
                    group_id
                    for group_id in selected_group_ids
                    if not (is_group_active and is_group_active(group_id))
                )
                if not selected_group_ids:
                    return None
            if selected_layout_ids is not None:
                selected_layout_ids = frozenset(
                    layout_id
                    for layout_id in selected_layout_ids
                    if self._layout_enabled(layout_id)
                    and not (is_layout_active and is_layout_active(layout_id))
                )
                if not selected_layout_ids and selected_group_ids is None:
                    return None
            self._capabilities.require(Capability.SAVE_SYNC, "Save/state reconciliation")
            self._require_remote()
            if not self.is_remote_reachable():
                raise SaveSyncConnectivityError(
                    f"Remote save location is not reachable: {self._connectivity_root}"
                )
            emit_progress(
                progress,
                "savesync",
                "preflight",
                "running",
                "Comparing local and shared save/state data",
            )
            self._recover()
            state = self._get_state_unlocked()
            repair_local_group_ids = frozenset(
                status.group_id
                for status in self._local_materialization_gaps(
                    state, excluded_layout_ids=frozenset()
                )
            )
            verification_layout_ids: Optional[frozenset[str]] = None
            if selected_layout_ids is not None:
                local_report = self._scan_local_layouts(selected_layout_ids)
                remote_report = self._scan_remote_layouts(selected_layout_ids)
                local_report = self._automatic_report(local_report)
                remote_report = self._automatic_report(remote_report)
            elif selected_group_ids is not None:
                group_layout_map = {
                    group.group_id: group.layout_id for group in state.groups
                }
                scoped_layouts = frozenset(
                    layout_id
                    for group_id in selected_group_ids
                    for layout_id in [group_layout_map.get(group_id)]
                    if layout_id is not None and self._layout_enabled(layout_id)
                )
                verification_layout_ids = scoped_layouts
                if scoped_layouts:
                    local_report = self._automatic_report(
                        self._scan_local_layouts(scoped_layouts)
                    )
                    remote_report = self._automatic_report(
                        self._scan_remote_layouts(scoped_layouts)
                    )
                else:
                    local_report = save_tree.ScanReport({})
                    remote_report = save_tree.ScanReport({})
            else:
                local_report = self._scan_automatic_local()
                remote_report = self._scan_automatic_remote()
            complete_local = dict(local_report.artifacts)
            complete_remote = dict(remote_report.artifacts)
            baseline = self._automatic_baseline(state)
            if selected_group_ids is not None:
                local_report = _report_for_groups(
                    local_report, selected_group_ids, self._policy
                )
                remote_report = _report_for_groups(
                    remote_report, selected_group_ids, self._policy
                )
                baseline = _manifest_for_groups(
                    baseline, selected_group_ids, self._policy
                )
            elif selected_layout_ids is not None:
                baseline = {
                    path: artifact
                    for path, artifact in baseline.items()
                    if (
                        (descriptor := self._policy.group_for_path(path)) is not None
                        and descriptor.layout_id in selected_layout_ids
                    )
                }
            original_local = dict(local_report.artifacts)
            original_remote = dict(remote_report.artifacts)
            physical_baseline = dict(baseline)
            container_work = self._prepare_container_reconciliation(
                local=original_local,
                remote=original_remote,
                state=state,
                upload_only=upload_only,
                repair_local_group_ids=repair_local_group_ids,
            )
            if container_work.handled_paths:
                local_report = replace(
                    local_report,
                    artifacts={
                        path: artifact
                        for path, artifact in local_report.artifacts.items()
                        if path not in container_work.handled_paths
                    },
                )
                remote_report = replace(
                    remote_report,
                    artifacts={
                        path: artifact
                        for path, artifact in remote_report.artifacts.items()
                        if path not in container_work.handled_paths
                    },
                )
                baseline = {
                    path: artifact
                    for path, artifact in baseline.items()
                    if path not in container_work.handled_paths
                }
            plan = _reconcile_plan(
                local_report,
                remote_report,
                baseline,
                policy=self._policy,
                scope=(
                    "pending_dirty"
                    if selected_group_ids is not None
                    else "journal-layout"
                    if selected_layout_ids is not None
                    else "all_eligible"
                ),
                repair_local_group_ids=repair_local_group_ids,
            )
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
            local = original_local
            remote = original_remote
            desired_local = dict(container_work.desired_local)
            desired_remote = dict(container_work.desired_remote)
            for entry in plan.entries:
                if entry.action is SaveReconcileAction.UPLOAD:
                    _assign(desired_remote, entry.relative_path, entry.local)
                elif entry.action is SaveReconcileAction.DOWNLOAD and not upload_only:
                    _assign(desired_local, entry.relative_path, entry.remote)

            operation_id = uuid.uuid4().hex
            destination_views: list[_DestinationView] = []
            selected_views: list[save_transaction.SelectedView] = []
            try:
                remote_changed = desired_remote != remote
                local_changed = desired_local != local
                if remote_changed:
                    # Only this branch writes to remote-data; a plan with no
                    # uploads (pure download/no-op) must remain available
                    # regardless of the remote's durable-transaction support.
                    self._require_durable_remote("Save/state reconciliation upload")
                    remote_views = (
                        _DestinationView(self._remote_transaction_root()),
                    )
                    destination_views.extend(remote_views)
                    selected_views.extend(
                        self._selected_transaction_views(
                            remote_views,
                            current=remote,
                            desired=desired_remote,
                            source_for=lambda path, artifact: (
                                container_work.remote_sources[path]
                                if path in container_work.remote_sources
                                else self._choose_source(path, artifact, local, remote)
                            ),
                        )
                    )
                if local_changed and not upload_only:
                    local_views = self._local_views()
                    destination_views.extend(local_views)
                    selected_views.extend(
                        self._selected_transaction_views(
                            local_views,
                            current=local,
                            desired=desired_local,
                            source_for=lambda path, artifact: (
                                container_work.local_sources[path]
                                if path in container_work.local_sources
                                else self._choose_source(path, artifact, local, remote)
                            ),
                        )
                    )
                transaction = (
                    save_transaction.prepare_transaction(
                        self._transaction_journal_path,
                        selected_views,
                        operation_id=operation_id,
                    )
                    if selected_views
                    else None
                )
                if verification_layout_ids is None:
                    current_local = self._scan_automatic_local()
                    current_remote = self._scan_automatic_remote()
                else:
                    current_local = self._automatic_report(
                        self._scan_local_layouts(verification_layout_ids)
                    )
                    current_remote = self._automatic_report(
                        self._scan_remote_layouts(verification_layout_ids)
                    )
                if (
                    current_local.artifacts != complete_local
                    or current_remote.artifacts != complete_remote
                ):
                    raise SaveSyncVerificationError(
                        "Save/state data changed while staging; reconciliation was abandoned."
                    )
                if selected_group_ids is not None and is_group_active and any(
                    is_group_active(group_id) for group_id in selected_group_ids
                ):
                    if transaction is not None:
                        transaction.rollback()
                    return None
                if selected_layout_ids is not None and is_layout_active and any(
                    is_layout_active(layout_id) for layout_id in selected_layout_ids
                ):
                    if transaction is not None:
                        transaction.rollback()
                    return None
                if transaction is not None:
                    self._apply_selected_transaction(
                        transaction, tuple(destination_views)
                    )
                self._verify_container_targets(container_work)
                if verification_layout_ids is None:
                    final_local_report = self._scan_automatic_local()
                    final_remote_report = self._scan_automatic_remote()
                else:
                    final_local_report = self._automatic_report(
                        self._scan_local_layouts(verification_layout_ids)
                    )
                    final_remote_report = self._automatic_report(
                        self._scan_remote_layouts(verification_layout_ids)
                    )
                if (
                    final_local_report.artifacts
                    != (
                        _replace_selected_groups(
                            complete_local,
                            desired_local,
                            selected_group_ids,
                            self._policy,
                        )
                    )
                    or final_remote_report.artifacts
                    != (
                        _replace_selected_groups(
                            complete_remote,
                            desired_remote,
                            selected_group_ids,
                            self._policy,
                        )
                    )
                ):
                    raise SaveSyncVerificationError(
                        "Save/state data changed before reconciliation completed."
                    )
            except BaseException:
                transaction = locals().get("transaction")
                if transaction is not None:
                    transaction.rollback()
                container_work.cleanup()
                raise

            timestamp = datetime.now(timezone.utc).isoformat()
            report = SaveReconcileReport(
                revision=uuid.uuid4().hex,
                timestamp=timestamp,
                uploaded=len(plan.uploads) + container_work.uploaded,
                downloaded=(
                    0 if upload_only else len(plan.downloads) + container_work.downloaded
                ),
                conflicts=len(plan.conflicts) + len(container_work.conflict_paths),
                unchanged=len(plan.unchanged) + container_work.unchanged,
                upload_bytes=plan.upload_bytes + container_work.upload_bytes,
                download_bytes=(
                    0
                    if upload_only
                    else plan.download_bytes + container_work.download_bytes
                ),
                conflict_paths=tuple(
                    [entry.relative_path for entry in plan.conflicts]
                    + container_work.conflict_paths
                ),
                scope=plan.scope,
            )
            selected_baseline = (
                _upload_only_reconciled_baseline(plan, existing=baseline)
                if upload_only
                else _reconciled_baseline(plan, existing=baseline)
            )
            for container_id, descriptor in container_work.descriptors.items():
                paths = self._container_paths(
                    desired_local, container_id, descriptor.container_kind
                )
                if container_id in container_work.conflicted_container_ids:
                    for path in paths:
                        if path in physical_baseline:
                            selected_baseline[path] = physical_baseline[path]
                    continue
                for path in paths:
                    selected_baseline[path] = desired_local[path]
            final_local_values = tuple(
                desired_local[path] for path in sorted(desired_local)
            )
            final_remote_values = tuple(
                desired_remote[path] for path in sorted(desired_remote)
            )
            selected_baseline_values = tuple(
                selected_baseline[path] for path in sorted(selected_baseline)
            )
            refreshed_groups = {
                group.group_id: group
                for group in _group_states_from_manifests(
                    selected_baseline_values,
                    final_local_values,
                    final_remote_values,
                    self._policy,
                    observed_at=timestamp,
                )
            }
            container_conflicted_group_ids = {
                descriptor.group_id
                for container_id, descriptor in container_work.descriptors.items()
                if container_id in container_work.conflicted_container_ids
            }
            container_conflicted_group_ids.update(
                descriptor.group_id
                for entry in plan.conflicts
                for descriptor in [self._policy.group_for_path(entry.relative_path)]
                if descriptor is not None
            )
            for container_id, descriptor in container_work.descriptors.items():
                group_id = descriptor.group_id
                local_artifacts = tuple(
                    desired_local[path]
                    for path in sorted(desired_local)
                    if (
                        (value := self._policy.group_for_path(path)) is not None
                        and value.group_id == group_id
                    )
                )
                remote_artifacts = tuple(
                    desired_remote[path]
                    for path in sorted(desired_remote)
                    if (
                        (value := self._policy.group_for_path(path)) is not None
                        and value.group_id == group_id
                    )
                )
                local_snapshot = SaveGroupSnapshot(
                    group_id, descriptor.layout_id, local_artifacts, timestamp
                )
                remote_snapshot = SaveGroupSnapshot(
                    group_id, descriptor.layout_id, remote_artifacts, timestamp
                )
                previous_group = next(
                    (group for group in state.groups if group.group_id == group_id),
                    None,
                )
                conflicted = group_id in container_conflicted_group_ids
                refreshed_groups[group_id] = SaveGroupState(
                    group_id=group_id,
                    layout_id=descriptor.layout_id,
                    condition=(
                        SaveGroupCondition.CONFLICT
                        if conflicted
                        else SaveGroupCondition.CLEAN
                    ),
                    baseline=(
                        previous_group.baseline
                        if conflicted and previous_group is not None
                        else local_snapshot
                    ),
                    local_observed=local_snapshot,
                    remote_observed=remote_snapshot,
                    verified_at=timestamp,
                )
            affected_layouts = {}
            for entry in plan.entries:
                descriptor = self._policy.group_for_path(entry.relative_path)
                if descriptor is None:
                    raise SaveSyncVerificationError(
                        "Reconciliation plan contains an unsupported path: "
                        f"{entry.relative_path}"
                    )
                affected_layouts[descriptor.group_id] = descriptor.layout_id
            for descriptor in container_work.descriptors.values():
                affected_layouts[descriptor.group_id] = descriptor.layout_id
            # A watcher can mark a supported group dirty before any baseline
            # or file exists. A verified empty reconciliation clears only a
            # hint whose path still resolves to that same current registry
            # identity; obsolete group identities remain conservative.
            for group in state.groups:
                if not self._layout_enabled(group.layout_id):
                    continue
                if (
                    selected_group_ids is not None
                    and group.group_id not in selected_group_ids
                ):
                    continue
                for hint in group.dirty_path_hints:
                    descriptor = self._policy.group_for_path(hint)
                    if (
                        descriptor is not None
                        and descriptor.group_id == group.group_id
                        and descriptor.layout_id == group.layout_id
                    ):
                        affected_layouts[group.group_id] = group.layout_id
                        break

            # An enabled dirty/conflict group can be verified empty on both
            # sides even when no artifact remains to appear in the plan.
            for group_id, layout_id in affected_layouts.items():
                if group_id in refreshed_groups:
                    continue
                snapshot = SaveGroupSnapshot(
                    group_id=group_id,
                    layout_id=layout_id,
                    artifacts=(),
                    observed_at=timestamp,
                )
                refreshed_groups[group_id] = SaveGroupState(
                    group_id=group_id,
                    layout_id=layout_id,
                    condition=SaveGroupCondition.CLEAN,
                    baseline=snapshot,
                    local_observed=snapshot,
                    remote_observed=snapshot,
                    verified_at=timestamp,
                )
            affected_group_ids = frozenset(affected_layouts)
            group_states = tuple(
                sorted(
                    (
                        group
                        for group in state.groups
                        if group.group_id not in affected_group_ids
                    ),
                    key=lambda group: group.group_id,
                )
            ) + tuple(
                refreshed_groups[group_id] for group_id in sorted(refreshed_groups)
            )

            combined_baseline = {
                artifact.relative_path: artifact
                for artifact in state.shared_manifest
                if self._policy.group_for_path(artifact.relative_path) is not None
                and _group_id(self._policy, artifact.relative_path)
                not in affected_group_ids
            }
            combined_baseline.update(selected_baseline)
            baseline_values = tuple(
                combined_baseline[path] for path in sorted(combined_baseline)
            )
            next_state = replace(
                state,
                shared_manifest=baseline_values,
                last_reconcile=report,
                groups=group_states,
                active_operation=None,
                last_error=None,
                last_completed_operation_id=operation_id,
                container_baselines=tuple(
                    sorted(
                        {
                            **{
                                item.container_id: item
                                for item in state.container_baselines
                                if item.container_id
                                not in container_work.invalidated_container_ids
                            },
                            **container_work.baselines,
                        }.values(),
                        key=lambda item: item.container_id,
                    )
                ),
            )
            conflicted_group_ids = {
                group.group_id
                for group in refreshed_groups.values()
                if group.condition is SaveGroupCondition.CONFLICT
            }
            next_state = replace(
                next_state,
                conflicts=tuple(
                    replace(
                        conflict,
                        resolved_at=timestamp,
                        resolution=SaveConflictResolution.MANUAL,
                        resolution_revision=report.revision,
                    )
                    if not conflict.resolved
                    and conflict.group_id in affected_group_ids
                    and conflict.group_id not in conflicted_group_ids
                    else conflict
                    for conflict in next_state.conflicts
                ),
            )
            for group in refreshed_groups.values():
                if group.condition is not SaveGroupCondition.CONFLICT:
                    continue
                assert group.local_observed is not None
                assert group.remote_observed is not None
                next_state = durable_state.record_conflict(
                    next_state,
                    group_id=group.group_id,
                    layout_id=group.layout_id,
                    baseline=group.baseline,
                    local=group.local_observed,
                    remote=group.remote_observed,
                    observed_at=timestamp,
                )
            try:
                _write_state(self._state_path, next_state)
            except BaseException:
                if transaction is not None:
                    transaction.rollback()
                container_work.cleanup()
                raise
            if desired_remote != remote:
                mutations = self._journal_mutations_for_remote_transition(
                    before=remote,
                    after=desired_remote,
                )
                if mutations:
                    generation = self._append_remote_journal(
                        revision=report.revision,
                        timestamp=timestamp,
                        mutations=mutations,
                    )
                    if next_state.quick_sync_ready:
                        next_state = replace(
                            next_state,
                            quick_sync_cursor_generation=generation,
                        )
                        _write_state(self._state_path, next_state)
            if transaction is not None:
                try:
                    transaction.finalize()
                except OSError:
                    log.warning(
                        "Could not clean completed SaveSync transaction %s",
                        operation_id,
                        exc_info=True,
                    )
            container_work.cleanup()
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
        if diff.direction != "upload":
            raise SaveSyncVerificationError("Upload requires an upload preview.")
        self._capabilities.require(Capability.SAVE_SYNC, "Upload All Saves")
        self._require_durable_remote("Upload All Saves")
        self._require_remote()
        return self._commit_force(
            diff,
            source_scan=self._scan_local,
            source_path=self._local_path,
            destination_views=(
                _DestinationView(self._remote_transaction_root()),
            ),
            progress=progress,
        )

    def commit_download(
        self, diff: SaveDiff, *, progress: ProgressSink = None
    ) -> SaveSyncRecord:
        if diff.direction != "download":
            raise SaveSyncVerificationError("Download requires a download preview.")
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
        with self._locked_operation():
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
            local, remote = _manifests_from_diff(current_preview)
            source, destination = (
                (local, remote) if diff.direction == "upload" else (remote, local)
            )
            emit_progress(
                progress,
                "savesync",
                "stage",
                "running",
                f"Staging {diff.direction} save/state replacement",
                metadata={"files": len(source), "bytes": sum(a.size_bytes for a in source.values())},
            )
            operation_id = uuid.uuid4().hex
            transaction = self._prepare_selected_transaction(
                destination_views,
                current=destination,
                desired=source,
                source_for=lambda path, _artifact: source_path(path),
                operation_id=operation_id,
            )
            try:
                # Verify both sides after staging.  This closes the historical
                # second-rescan race and ensures the committed bytes are
                # exactly those shown in the confirmed preview.
                if self._scan_local().artifacts != local or self._scan_remote().artifacts != remote:
                    raise SaveSyncVerificationError(
                        "Save/state data changed while staging; review the operation again."
                    )
                if transaction is not None:
                    self._apply_selected_transaction(transaction, destination_views)
                if source_scan().artifacts != source:
                    raise SaveSyncVerificationError(
                        "Save/state source changed before commit completed; no replacement was kept."
                    )
            except BaseException:
                if transaction is not None:
                    transaction.rollback()
                raise

            record = SaveSyncRecord(
                revision=uuid.uuid4().hex,
                timestamp=datetime.now(timezone.utc).isoformat(),
                device_id=self._get_state_unlocked().device_id,
                manifest=tuple(source[path] for path in sorted(source)),
            )
            try:
                self._advance_force_state(
                    diff.direction,
                    record,
                    local=local,
                    remote=remote,
                    operation_id=operation_id,
                )
            except BaseException:
                if transaction is not None:
                    transaction.rollback()
                raise
            if diff.direction == "upload":
                mutations = self._journal_mutations_for_remote_transition(
                    before=remote,
                    after=source,
                )
                if mutations:
                    generation = self._append_remote_journal(
                        revision=record.revision,
                        timestamp=record.timestamp,
                        mutations=mutations,
                    )
                    state = self._get_state_unlocked()
                    if state.quick_sync_ready:
                        _write_state(
                            self._state_path,
                            replace(
                                state,
                                quick_sync_cursor_generation=generation,
                            ),
                        )
            if transaction is not None:
                try:
                    transaction.finalize()
                except OSError:
                    # The completion receipt is durable. Recovery will only
                    # clean the journal/stage; it will not roll back success.
                    log.warning(
                        "Could not clean completed SaveSync transaction %s",
                        operation_id,
                        exc_info=True,
                    )
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
        if view.root != self._local_root:
            return True
        mapped_prefixes = tuple(
            f"{mapped.canonical_prefix}/"
            for mapped in self._local_views()
            if mapped.canonical_prefix
        )
        return not canonical_path.startswith(mapped_prefixes)

    def _physical_manifest(
        self,
        view: _DestinationView,
        canonical: dict[str, SaveArtifact],
    ) -> tuple[dict[str, SaveArtifact], dict[str, str]]:
        physical: dict[str, SaveArtifact] = {}
        reverse: dict[str, str] = {}
        for canonical_path, artifact in canonical.items():
            if not self._belongs_to_view(view, canonical_path):
                continue
            relative = self._path_for_view(view, canonical_path)
            assert relative is not None
            relative_path = relative.as_posix()
            if relative_path in physical:
                raise SaveSyncVerificationError(
                    f"Duplicate SaveSync destination path: {view.root / relative}"
                )
            physical[relative_path] = SaveArtifact(
                relative_path,
                artifact.size_bytes,
                artifact.content_hash,
            )
            reverse[relative_path] = canonical_path
        return physical, reverse

    def _scan_view(self, root: Path, view: _DestinationView) -> dict[str, SaveArtifact]:
        if view.canonical_prefix:
            report = self._scan_mapped_view(view, self._policy, root=root)
        else:
            if view.root == self._local_root:
                report = self._scan_primary(
                    root, self._primary_local_policy(self._policy)
                )
                report = self._without_mapped_local_prefixes(report)
            else:
                report = self._scan_primary(root)
        physical, _ = self._physical_manifest(view, report.artifacts)
        return physical

    def _selected_transaction_views(
        self,
        views: tuple[_DestinationView, ...],
        *,
        current: dict[str, SaveArtifact],
        desired: dict[str, SaveArtifact],
        source_for: Callable[[str, SaveArtifact], Path],
    ) -> list[save_transaction.SelectedView]:
        selected_views: list[save_transaction.SelectedView] = []
        for view in views:
            view_current, _ = self._physical_manifest(view, current)
            view_desired, reverse = self._physical_manifest(view, desired)
            if view_current == view_desired:
                continue
            selected_views.append(
                save_transaction.SelectedView(
                    root=view.root,
                    current=view_current,
                    desired=view_desired,
                    source_for=lambda relative, artifact, reverse=reverse: source_for(
                        reverse[relative], desired[reverse[relative]]
                    ),
                )
            )
        return selected_views

    def _prepare_selected_transaction(
        self,
        views: tuple[_DestinationView, ...],
        *,
        current: dict[str, SaveArtifact],
        desired: dict[str, SaveArtifact],
        source_for: Callable[[str, SaveArtifact], Path],
        operation_id: str,
    ) -> Optional[save_transaction.SelectedTransaction]:
        selected_views = self._selected_transaction_views(
            views,
            current=current,
            desired=desired,
            source_for=source_for,
        )
        return (
            save_transaction.prepare_transaction(
                self._transaction_journal_path,
                selected_views,
                operation_id=operation_id,
            )
            if selected_views
            else None
        )

    def _apply_selected_transaction(
        self,
        transaction: save_transaction.SelectedTransaction,
        views: tuple[_DestinationView, ...],
    ) -> None:
        by_root = {view.root.absolute(): view for view in views}
        selected_paths = {
            view.root.absolute(): frozenset(view.verification_current)
            | frozenset(view.verification_desired)
            for view in transaction.views
        }

        def verify(root: Path, expected: dict[str, SaveArtifact]) -> None:
            view = by_root.get(root.absolute())
            if view is None:
                raise SaveSyncVerificationError(
                    f"Unexpected SaveSync transaction destination: {root}"
                )
            observed = self._scan_view(root, view)
            observed = {
                path: artifact
                for path, artifact in observed.items()
                if path in selected_paths.get(root.absolute(), frozenset())
            }
            if observed != expected:
                raise SaveSyncVerificationError(
                    f"Selected save/state tree verification failed for {root}"
                )

        save_transaction.apply_transaction(
            transaction,
            verify_current=verify,
            verify_desired=verify,
        )
        log.info(
            "SaveSync local/remote apply verified: operation_id=%s "
            "selected_transaction_path_count=%d staged_path_count=%d "
            "promoted_path_count=%d verified_path_count=%d",
            transaction.operation_id,
            transaction.metrics.changed_files,
            transaction.metrics.staged_files,
            transaction.metrics.changed_files,
            sum(len(view.verification_desired) for view in transaction.views),
        )

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

    def _advance_force_state(
        self,
        direction: str,
        record: SaveSyncRecord,
        *,
        local: dict[str, SaveArtifact],
        remote: dict[str, SaveArtifact],
        operation_id: Optional[str] = None,
    ) -> None:
        state = self._get_state_unlocked()
        timestamp = record.timestamp
        descriptors = {}
        for path in sorted(set(local) | set(remote)):
            descriptor = self._policy.group_for_path(path)
            if descriptor is None:
                raise SaveSyncVerificationError(
                    f"Confirmed SaveSync manifest contains an unsupported path: {path}"
                )
            previous = descriptors.get(descriptor.group_id)
            if previous is not None and previous.layout_id != descriptor.layout_id:
                raise SaveSyncVerificationError(
                    f"SaveSync group identity is ambiguous: {descriptor.group_id}"
                )
            descriptors[descriptor.group_id] = descriptor
        affected_group_ids = frozenset(descriptors)

        refreshed_groups = {
            group.group_id: group
            for group in _group_states_from_manifests(
                record.manifest,
                record.manifest,
                record.manifest,
                self._policy,
                observed_at=timestamp,
            )
        }
        # A force operation may deliberately delete a destination-only group.
        # Keep a verified empty snapshot so its conflict can be resolved
        # without losing the fact that both selected sides are now empty.
        for group_id, descriptor in descriptors.items():
            if group_id in refreshed_groups:
                continue
            snapshot = SaveGroupSnapshot(
                group_id=group_id,
                layout_id=descriptor.layout_id,
                artifacts=(),
                observed_at=timestamp,
            )
            refreshed_groups[group_id] = SaveGroupState(
                group_id=group_id,
                layout_id=descriptor.layout_id,
                condition=SaveGroupCondition.CLEAN,
                baseline=snapshot,
                local_observed=snapshot,
                remote_observed=snapshot,
                verified_at=timestamp,
            )

        groups = tuple(
            sorted(
                (
                    group
                    for group in state.groups
                    if group.group_id not in affected_group_ids
                ),
                key=lambda group: group.group_id,
            )
        ) + tuple(
            refreshed_groups[group_id] for group_id in sorted(refreshed_groups)
        )

        # Optional layouts that were not selected (notably disabled xemu) keep
        # their last common baseline and unresolved conflict history.
        shared_manifest = {
            artifact.relative_path: artifact
            for artifact in state.shared_manifest
            if self._policy.group_for_path(artifact.relative_path) is not None
            and _group_id(self._policy, artifact.relative_path)
            not in affected_group_ids
        }
        shared_manifest.update(
            {artifact.relative_path: artifact for artifact in record.manifest}
        )
        shared_values = tuple(shared_manifest[path] for path in sorted(shared_manifest))

        resolution = (
            SaveConflictResolution.KEEP_LOCAL
            if direction == "upload"
            else SaveConflictResolution.KEEP_REMOTE
        )
        conflicts = tuple(
            replace(
                conflict,
                resolved_at=timestamp,
                resolution=resolution,
                resolution_revision=record.revision,
            )
            if not conflict.resolved and conflict.group_id in affected_group_ids
            else conflict
            for conflict in state.conflicts
        )
        container_baselines = self._container_baselines_after_force(
            state,
            record.manifest,
            affected_paths=frozenset(set(local).union(remote)),
        )
        _write_state(
            self._state_path,
            replace(
                state,
                last_upload=(record if direction == "upload" else state.last_upload),
                last_download=(
                    record if direction == "download" else state.last_download
                ),
                shared_manifest=shared_values,
                groups=groups,
                conflicts=conflicts,
                container_baselines=container_baselines,
                active_operation=None,
                last_error=None,
                last_completed_operation_id=operation_id,
            ),
        )

    def _container_baselines_after_force(
        self,
        state: SaveSyncState,
        manifest: tuple[SaveArtifact, ...],
        *,
        affected_paths: frozenset[str],
    ) -> tuple[ContainerBaseline, ...]:
        if not self._container_merge_enabled:
            return state.container_baselines
        by_id = {item.container_id: item for item in state.container_baselines}
        descriptors = {}
        manifest_map = {artifact.relative_path: artifact for artifact in manifest}
        for path in sorted(set(affected_paths).union(manifest_map)):
            descriptor = self._policy.group_for_path(path)
            if descriptor is not None and descriptor.container_id:
                descriptors.setdefault(descriptor.container_id, descriptor)
        for container_id, descriptor in descriptors.items():
            if not self._container_paths(
                manifest_map, container_id, descriptor.container_kind
            ):
                by_id.pop(container_id, None)
                continue
            adapter = self._container_registry.get(descriptor.container_adapter_id)
            if adapter is None:
                by_id.pop(container_id, None)
                continue
            try:
                snapshot = adapter.snapshot(
                    self._local_root / container_id,
                    container_id=container_id,
                )
            except OpaqueContainerError:
                by_id.pop(container_id, None)
                continue
            by_id[container_id] = baseline_from_snapshot(snapshot)
        return tuple(by_id[key] for key in sorted(by_id))

    def _append_remote_journal(
        self,
        *,
        revision: str,
        timestamp: str,
        mutations: list[dict[str, object]],
    ) -> int:
        path = self._remote_journal_path
        if path is None:
            return 0
        state = self._get_state_unlocked()
        return savesync_journal.append_mutations(
            path,
            device_id=state.device_id,
            revision=revision,
            timestamp=timestamp,
            mutations=mutations,
        )

    def _journal_mutations_for_remote_transition(
        self,
        *,
        before: dict[str, SaveArtifact],
        after: dict[str, SaveArtifact],
    ) -> list[dict[str, object]]:
        grouped_before = _grouped_manifest(before, self._policy)
        grouped_after = _grouped_manifest(after, self._policy)
        mutations: list[dict[str, object]] = []
        for group_id in sorted(set(grouped_before) | set(grouped_after)):
            before_group = grouped_before.get(group_id, ())
            after_group = grouped_after.get(group_id, ())
            if before_group == after_group:
                continue
            descriptor = self._policy.group_for_path(
                (after_group[0].relative_path if after_group else before_group[0].relative_path)
            )
            if descriptor is None:
                continue
            operation = "update"
            if not before_group and after_group:
                operation = "create"
            elif before_group and not after_group:
                operation = "delete"
            object_id = None
            if len(after_group) == 1:
                object_id = after_group[0].relative_path
            elif len(before_group) == 1:
                object_id = before_group[0].relative_path
            mutations.append(
                {
                    "system": descriptor.system,
                    "layout_id": descriptor.layout_id,
                    "group_id": descriptor.group_id,
                    "object_id": object_id,
                    "operation": operation,
                }
            )
        return mutations


def _same_artifact(left: Optional[SaveArtifact], right: Optional[SaveArtifact]) -> bool:
    if left is None or right is None:
        return left is right
    return (
        left.relative_path == right.relative_path
        and left.size_bytes == right.size_bytes
        and left.content_hash == right.content_hash
    )


def _manifests_from_diff(
    diff: SaveDiff,
) -> tuple[dict[str, SaveArtifact], dict[str, SaveArtifact]]:
    """Recover the exact verified local/remote snapshots shown in a preview."""
    local: dict[str, SaveArtifact] = {}
    remote: dict[str, SaveArtifact] = {}
    for entry in diff.entries:
        if entry.local is not None:
            local[entry.relative_path] = entry.local
        if entry.remote is not None:
            remote[entry.relative_path] = entry.remote
    return local, remote


def _group_id(policy: SaveSelectionPolicy, path: str) -> str:
    descriptor = policy.group_for_path(path)
    if descriptor is None:
        # Baselines from an older policy can outlive layout support. Keep each
        # such path isolated and conservative until a verified force operation
        # removes it from the common manifest.
        return f"unsupported:{path}"
    return descriptor.group_id


def _group_manifest(
    manifest: dict[str, SaveArtifact], paths: list[str]
) -> tuple[SaveArtifact, ...]:
    return tuple(manifest[path] for path in paths if path in manifest)


def _grouped_manifest(
    manifest: dict[str, SaveArtifact],
    policy: SaveSelectionPolicy,
) -> dict[str, tuple[SaveArtifact, ...]]:
    grouped_paths: dict[str, list[str]] = {}
    for path in sorted(manifest):
        grouped_paths.setdefault(_group_id(policy, path), []).append(path)
    return {
        group_id: _group_manifest(manifest, paths)
        for group_id, paths in grouped_paths.items()
    }


def _same_manifest(
    left: tuple[SaveArtifact, ...], right: tuple[SaveArtifact, ...]
) -> bool:
    return left == right


def _is_incomplete_local_materialization(
    local: tuple[SaveArtifact, ...],
    remote: tuple[SaveArtifact, ...],
    baseline: tuple[SaveArtifact, ...],
) -> bool:
    """Return whether local is only missing files from a known generation.

    Missing physical paths are not a new local content generation when the
    remote still holds the last shared generation.  This is intentionally
    narrower than treating every deletion as repairable: any added or modified
    local artifact, remote change, or absent remote side keeps the ordinary
    three-way/conflict semantics.
    """
    if (
        not remote
        or not baseline
        or len(local) >= len(baseline)
        or not _same_manifest(remote, baseline)
    ):
        return False
    baseline_by_path = {
        artifact.relative_path: artifact for artifact in baseline
    }
    return all(
        _same_artifact(baseline_by_path.get(artifact.relative_path), artifact)
        for artifact in local
    )


def _group_conflict_ids(
    local: dict[str, SaveArtifact],
    remote: dict[str, SaveArtifact],
    baseline: dict[str, SaveArtifact],
    policy: SaveSelectionPolicy,
) -> frozenset[str]:
    paths = sorted(set(local) | set(remote) | set(baseline))
    groups: dict[str, list[str]] = {}
    for path in paths:
        groups.setdefault(_group_id(policy, path), []).append(path)
    conflicts = {
        group_id
        for group_id, group_paths in groups.items()
        if not _same_manifest(_group_manifest(local, group_paths), _group_manifest(remote, group_paths))
        and not _same_manifest(
            _group_manifest(local, group_paths), _group_manifest(baseline, group_paths)
        )
        and not _same_manifest(
            _group_manifest(remote, group_paths), _group_manifest(baseline, group_paths)
        )
    }
    return frozenset(conflicts)


def _group_states_from_manifests(
    baseline_values: tuple[SaveArtifact, ...],
    local_values: tuple[SaveArtifact, ...],
    remote_values: tuple[SaveArtifact, ...],
    policy: SaveSelectionPolicy,
    *,
    observed_at: str,
) -> tuple[SaveGroupState, ...]:
    baseline = {artifact.relative_path: artifact for artifact in baseline_values}
    local = {artifact.relative_path: artifact for artifact in local_values}
    remote = {artifact.relative_path: artifact for artifact in remote_values}
    grouped: dict[str, list[str]] = {}
    descriptors = {}
    for path in sorted(set(baseline) | set(local) | set(remote)):
        descriptor = policy.group_for_path(path)
        if descriptor is None:
            continue
        grouped.setdefault(descriptor.group_id, []).append(path)
        descriptors[descriptor.group_id] = descriptor
    states: list[SaveGroupState] = []
    for group_id, paths in sorted(grouped.items()):
        descriptor = descriptors[group_id]

        def snapshot(values: dict[str, SaveArtifact]) -> SaveGroupSnapshot:
            return SaveGroupSnapshot(
                group_id=group_id,
                layout_id=descriptor.layout_id,
                artifacts=tuple(values[path] for path in paths if path in values),
                observed_at=observed_at,
            )

        base_snapshot = snapshot(baseline)
        local_snapshot = snapshot(local)
        remote_snapshot = snapshot(remote)
        base_content = base_snapshot.artifacts
        local_content = local_snapshot.artifacts
        remote_content = remote_snapshot.artifacts
        if local_content == remote_content:
            condition = SaveGroupCondition.CLEAN
        elif remote_content == base_content:
            condition = SaveGroupCondition.LOCAL_DIRTY
        elif local_content == base_content:
            condition = SaveGroupCondition.REMOTE_DIRTY
        else:
            condition = SaveGroupCondition.CONFLICT
        states.append(
            SaveGroupState(
                group_id=group_id,
                layout_id=descriptor.layout_id,
                condition=condition,
                baseline=base_snapshot,
                local_observed=local_snapshot,
                remote_observed=remote_snapshot,
                verified_at=observed_at,
            )
        )
    return tuple(states)


def _assign(
    manifest: dict[str, SaveArtifact], path: str, artifact: Optional[SaveArtifact]
) -> None:
    if artifact is None:
        manifest.pop(path, None)
    else:
        manifest[path] = artifact


def _mtime_at_or_after(path: Path, threshold: float) -> bool:
    try:
        return path.stat(follow_symlinks=False).st_mtime >= threshold
    except (FileNotFoundError, OSError):
        return False


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
    policy: SaveSelectionPolicy,
) -> tuple[SaveDiffEntry, ...]:
    group_conflicts = _group_conflict_ids(new_side, old_side, baseline, policy)
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
                conflict=(
                    _group_id(policy, path) in group_conflicts
                    or _is_conflict(local, remote, baseline.get(path))
                ),
            )
        )
    return tuple(entries)


def _reconcile_plan(
    local_report: save_tree.ScanReport,
    remote_report: save_tree.ScanReport,
    baseline: dict[str, SaveArtifact],
    *,
    policy: SaveSelectionPolicy,
    scope: str = "all_eligible",
    repair_local_group_ids: frozenset[str] = frozenset(),
) -> SaveReconcilePlan:
    local = local_report.artifacts
    remote = remote_report.artifacts
    entries: list[SaveReconcileEntry] = []
    paths = sorted(set(local) | set(remote) | set(baseline))
    groups: dict[str, list[str]] = {}
    for path in paths:
        groups.setdefault(_group_id(policy, path), []).append(path)
    for group_id in sorted(groups):
        group_paths = groups[group_id]
        local_group = _group_manifest(local, group_paths)
        remote_group = _group_manifest(remote, group_paths)
        baseline_group = _group_manifest(baseline, group_paths)
        if _same_manifest(local_group, remote_group):
            group_action = SaveReconcileAction.UNCHANGED
        elif (
            group_id in repair_local_group_ids
            and _is_incomplete_local_materialization(
                local_group, remote_group, baseline_group
            )
        ):
            # A durable logical generation is not locally applied while any of
            # its required physical files are absent.  Download All has always
            # repaired this by making the remote manifest authoritative; normal
            # reconciliation now makes the same safe decision for a pure gap.
            group_action = SaveReconcileAction.DOWNLOAD
            descriptor = policy.group_for_path(
                remote_group[0].relative_path
            )
            log.warning(
                "SaveSync reconciliation repair: logical_save_id=%s layout=%s "
                "reconciliation_decision=download "
                "expected_physical_destinations=%d "
                "existing_physical_destinations=%d "
                "missing_physical_destinations=%d "
                "selected_transaction_path_count=%d "
                "reason=incomplete-local-materialization",
                group_id,
                descriptor.layout_id if descriptor is not None else "unknown",
                len(remote_group),
                len(local_group),
                len(baseline_group) - len(local_group),
                sum(
                    1
                    for artifact in remote_group
                    if artifact not in local_group
                ),
            )
        elif _same_manifest(remote_group, baseline_group):
            group_action = SaveReconcileAction.UPLOAD
        elif _same_manifest(local_group, baseline_group):
            group_action = SaveReconcileAction.DOWNLOAD
        else:
            group_action = SaveReconcileAction.CONFLICT
        for path in group_paths:
            local_artifact = local.get(path)
            remote_artifact = remote.get(path)
            base_artifact = baseline.get(path)
            entries.append(
                SaveReconcileEntry(
                    path,
                    group_action,
                    local_artifact,
                    remote_artifact,
                    base_artifact,
                )
            )
    return SaveReconcilePlan(
        tuple(sorted(entries, key=lambda entry: entry.relative_path)),
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


def _upload_only_reconciled_baseline(
    plan: SaveReconcilePlan,
    *,
    existing: Optional[dict[str, SaveArtifact]] = None,
) -> dict[str, SaveArtifact]:
    """Advance only groups proven equal or successfully uploaded.

    Remote-only changes intentionally remain remote-dirty for a later manual
    reconciliation. Conflicts retain their previous common ancestor.
    """
    baseline: dict[str, SaveArtifact] = dict(existing or {})
    for entry in plan.entries:
        if entry.action is SaveReconcileAction.UPLOAD:
            artifact = entry.local
        elif entry.action is SaveReconcileAction.UNCHANGED:
            artifact = entry.local if entry.local is not None else entry.remote
        else:
            artifact = entry.baseline
        _assign(baseline, entry.relative_path, artifact)
    return baseline


def _manifest_for_groups(
    manifest: dict[str, SaveArtifact],
    group_ids: frozenset[str],
    policy: SaveSelectionPolicy,
) -> dict[str, SaveArtifact]:
    return {
        path: artifact
        for path, artifact in manifest.items()
        if _group_id(policy, path) in group_ids
    }


def _report_for_groups(
    report: save_tree.ScanReport,
    group_ids: frozenset[str],
    policy: SaveSelectionPolicy,
) -> save_tree.ScanReport:
    return replace(
        report,
        artifacts=_manifest_for_groups(report.artifacts, group_ids, policy),
    )


def _replace_selected_groups(
    complete: dict[str, SaveArtifact],
    selected: dict[str, SaveArtifact],
    group_ids: Optional[frozenset[str]],
    policy: SaveSelectionPolicy,
) -> dict[str, SaveArtifact]:
    if group_ids is None:
        return dict(selected)
    result = {
        path: artifact
        for path, artifact in complete.items()
        if _group_id(policy, path) not in group_ids
    }
    result.update(selected)
    return result


def _merge_optional_groups(*reports: save_tree.ScanReport) -> tuple[tuple[str, int, int], ...]:
    merged: dict[str, list[int]] = {}
    for report in reports:
        for group, files, size_bytes in report.optional_groups:
            stats = merged.setdefault(group, [0, 0])
            stats[0] += files
            stats[1] += size_bytes
    return tuple((group, values[0], values[1]) for group, values in sorted(merged.items()))


# ── state persistence and migration ───────────────────────────────────────


def _read_state(path: Path) -> SaveSyncState:
    return durable_state.read_state(path)


def _write_state(path: Path, state: SaveSyncState) -> None:
    durable_state.write_state(path, state)


def _baseline_manifest(state: SaveSyncState) -> dict[str, SaveArtifact]:
    # V1 migration materializes its newest successful force manifest into the
    # v3 field. Therefore an empty tuple here is authoritative: both selected
    # sides may have been verified empty by a later reconciliation.
    return {artifact.relative_path: artifact for artifact in state.shared_manifest}
