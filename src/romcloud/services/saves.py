"""SaveSync v1 — manual, directional, whole-dataset synchronization of
game-progress save data.

Design (see the SaveSync v1 spec):

- No automatic sync, no launch/exit hooks, no bidirectional
  reconciliation, no per-game assumptions.
- Upload and download are symmetric, one-shot, confirmed operations:
  preview a full diff, stage the new dataset without touching the
  existing one, verify the staged copy, then atomically swap it into
  place. State only advances after that verified commit.
- The "remote" SaveSync dataset uses a dedicated read-write mount of the
  same SMB share as the read-only ROM catalog mount. Local/USB deployments
  keep using their ordinary filesystem root. Both remain plain filesystem
  paths so staging and directory renames stay on one filesystem.
- Both the CLI (``romcloud saves ...``) and the graphical UI (via
  ``romcloud uidata savesync-*``) call this same service — neither
  duplicates selection, diffing, or commit logic.
"""

from __future__ import annotations

import json
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from romcloud.core.exceptions import SaveSyncConnectivityError, SaveSyncVerificationError
from romcloud.core.models.savesync import (
    SaveArtifact,
    SaveChangeKind,
    SaveDiff,
    SaveDiffEntry,
    SaveSyncRecord,
    SaveSyncState,
)
from romcloud.core.save_selection import (
    DEFAULT_SAVE_SELECTION_POLICY,
    XBOX_HDD_RELATIVE_PATH,
    XBOX_SYSTEM,
    SaveSelectionPolicy,
)
from romcloud.core.storage import StorageProvider
from romcloud.infrastructure import save_tree
from romcloud.infrastructure.atomic_file import atomic_write_text
from romcloud.infrastructure.logging import get_logger

log = get_logger("saves")


class SaveSyncService:
    """Manages SaveSync v1 upload/download of game-progress save data."""

    def __init__(
        self,
        *,
        provider: StorageProvider,
        connectivity_root: str,
        local_root: str,
        remote_root: str,
        state_path: Path,
        xbox_enabled: bool = False,
        policy: SaveSelectionPolicy = DEFAULT_SAVE_SELECTION_POLICY,
    ) -> None:
        self._provider = provider
        self._connectivity_root = connectivity_root
        self._local_root = Path(local_root)
        self._remote_root = Path(remote_root)
        self._state_path = Path(state_path)
        self._xbox_enabled = xbox_enabled
        self._policy = policy

    # ── connectivity ──────────────────────────────────────────────────────

    def is_remote_reachable(self) -> bool:
        return self._provider.is_reachable(self._connectivity_root)

    # ── xbox opt-in (disabled by default; see SaveSelectionPolicy) ───────

    @property
    def xbox_enabled(self) -> bool:
        return self._xbox_enabled

    def xbox_hdd_size(self) -> Optional[int]:
        """Current size of the local xemu virtual hard drive, or ``None``
        if it doesn't exist — shown before enabling/uploading it."""
        path = self._local_root / XBOX_SYSTEM / XBOX_HDD_RELATIVE_PATH
        if not path.is_file():
            return None
        return path.stat().st_size

    def _enabled_optional_systems(self) -> frozenset[str]:
        return frozenset({XBOX_SYSTEM}) if self._xbox_enabled else frozenset()

    # ── state ─────────────────────────────────────────────────────────────

    def get_state(self) -> SaveSyncState:
        state = _read_state(self._state_path)
        if not self._state_path.exists():
            # Persist the freshly generated device_id immediately so it is
            # stable across every subsequent read, not regenerated per call.
            _write_state(self._state_path, state)
        return state

    # ── preview (save-content read-only; may recover transaction debris) ─

    def preview_upload(self) -> SaveDiff:
        return self._preview("upload")

    def preview_download(self) -> SaveDiff:
        return self._preview("download")

    def _preview(self, direction: str) -> SaveDiff:
        if not self.is_remote_reachable():
            raise SaveSyncConnectivityError(
                f"Remote save location is not reachable: {self._connectivity_root}"
            )
        # A killed process may have stopped between the two directory renames
        # used when replacing an existing dataset. Restore the last complete
        # dataset (or remove abandoned staging) before calculating a diff.
        save_tree.recover_interrupted_commit(self._remote_root)
        save_tree.recover_interrupted_commit(self._local_root)
        enabled_optional = self._enabled_optional_systems()
        local = save_tree.scan_tree(self._local_root, self._policy, enabled_optional_systems=enabled_optional)
        remote = save_tree.scan_tree(self._remote_root, self._policy, enabled_optional_systems=enabled_optional)
        new_side, old_side = (local, remote) if direction == "upload" else (remote, local)
        return SaveDiff(direction=direction, entries=_diff_entries(new_side, old_side, direction=direction))

    # ── commit (stage → verify → atomic swap → advance state) ───────────

    def commit_upload(self, diff: SaveDiff) -> SaveSyncRecord:
        return self._commit(diff, source_root=self._local_root, dest_root=self._remote_root)

    def commit_download(self, diff: SaveDiff) -> SaveSyncRecord:
        return self._commit(diff, source_root=self._remote_root, dest_root=self._local_root)

    def _commit(self, diff: SaveDiff, *, source_root: Path, dest_root: Path) -> SaveSyncRecord:
        if not self.is_remote_reachable():
            raise SaveSyncConnectivityError(
                f"Remote save location is not reachable: {self._connectivity_root}"
            )

        save_tree.recover_interrupted_commit(dest_root)
        staging = save_tree.new_staging_dir(dest_root)
        try:
            manifest = self._stage(diff, source_root=source_root, old_dest_root=dest_root, staging=staging)
            self._verify_staged(staging, manifest)
            save_tree.atomic_replace_dir(staging, dest_root)
        except BaseException:
            # Anything that goes wrong before the swap only ever touched
            # the throwaway staging directory — dest_root is untouched.
            shutil.rmtree(staging, ignore_errors=True)
            raise

        record = SaveSyncRecord(
            revision=uuid.uuid4().hex,
            timestamp=datetime.now(timezone.utc).isoformat(),
            device_id=self.get_state().device_id,
            manifest=tuple(manifest),
        )
        self._advance_state(diff.direction, record)
        log.info(
            "SaveSync %s committed: %d artifact(s), revision %s",
            diff.direction, record.artifact_count, record.revision,
        )
        return record

    def _stage(
        self, diff: SaveDiff, *, source_root: Path, old_dest_root: Path, staging: Path
    ) -> list[SaveArtifact]:
        manifest: list[SaveArtifact] = []
        for entry in diff.entries:
            if entry.change == SaveChangeKind.REMOVED:
                continue  # simply absent from the new dataset
            artifact = entry.local if diff.direction == "upload" else entry.remote
            assert artifact is not None
            unchanged_source = (
                old_dest_root / artifact.relative_path
                if entry.change == SaveChangeKind.UNCHANGED
                else None
            )
            save_tree.materialize(
                staging / artifact.relative_path,
                fresh_source=source_root / artifact.relative_path,
                unchanged_source=unchanged_source,
            )
            manifest.append(artifact)
        return manifest

    def _verify_staged(self, staging: Path, manifest: list[SaveArtifact]) -> None:
        for artifact in manifest:
            staged_path = staging / artifact.relative_path
            if not staged_path.is_file():
                raise SaveSyncVerificationError(f"Missing after staging: {artifact.relative_path}")
            actual_size = staged_path.stat().st_size
            if actual_size != artifact.size_bytes:
                raise SaveSyncVerificationError(
                    f"Size mismatch after staging for {artifact.relative_path}: "
                    f"expected {artifact.size_bytes}, got {actual_size}"
                )
            if save_tree.hash_file(staged_path) != artifact.content_hash:
                raise SaveSyncVerificationError(
                    f"Content changed since preview: {artifact.relative_path}"
                )

    def _advance_state(self, direction: str, record: SaveSyncRecord) -> None:
        state = self.get_state()
        if direction == "upload":
            state = SaveSyncState(device_id=state.device_id, last_upload=record, last_download=state.last_download)
        else:
            state = SaveSyncState(device_id=state.device_id, last_upload=state.last_upload, last_download=record)
        _write_state(self._state_path, state)


def _diff_entries(new_side: dict, old_side: dict, *, direction: str) -> tuple[SaveDiffEntry, ...]:
    entries: list[SaveDiffEntry] = []
    for path in sorted(set(new_side) | set(old_side)):
        new_artifact = new_side.get(path)
        old_artifact = old_side.get(path)
        local, remote = (new_artifact, old_artifact) if direction == "upload" else (old_artifact, new_artifact)

        if new_artifact is not None and old_artifact is None:
            change = SaveChangeKind.ADDED
        elif new_artifact is None and old_artifact is not None:
            change = SaveChangeKind.REMOVED
        elif (
            new_artifact.content_hash != old_artifact.content_hash
            or new_artifact.size_bytes != old_artifact.size_bytes
        ):
            change = SaveChangeKind.CHANGED
        else:
            change = SaveChangeKind.UNCHANGED

        entries.append(SaveDiffEntry(relative_path=path, change=change, local=local, remote=remote))
    return tuple(entries)


# ── state persistence ─────────────────────────────────────────────────────


def _read_state(path: Path) -> SaveSyncState:
    if not path.exists():
        return SaveSyncState(device_id=uuid.uuid4().hex)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return SaveSyncState(device_id=uuid.uuid4().hex)

    def record(payload: Optional[dict]) -> Optional[SaveSyncRecord]:
        if payload is None:
            return None
        try:
            return SaveSyncRecord(
                revision=payload["revision"],
                timestamp=payload["timestamp"],
                device_id=payload["device_id"],
                manifest=tuple(
                    SaveArtifact(
                        relative_path=a["relative_path"],
                        size_bytes=int(a["size_bytes"]),
                        content_hash=a["content_hash"],
                    )
                    for a in payload.get("manifest", [])
                ),
            )
        except (KeyError, TypeError, ValueError):
            return None

    return SaveSyncState(
        device_id=data.get("device_id") or uuid.uuid4().hex,
        last_upload=record(data.get("last_upload")),
        last_download=record(data.get("last_download")),
    )


def _write_state(path: Path, state: SaveSyncState) -> None:
    def record_dict(record: Optional[SaveSyncRecord]) -> Optional[dict]:
        if record is None:
            return None
        return {
            "revision": record.revision,
            "timestamp": record.timestamp,
            "device_id": record.device_id,
            "manifest": [
                {"relative_path": a.relative_path, "size_bytes": a.size_bytes, "content_hash": a.content_hash}
                for a in record.manifest
            ],
        }

    payload = {
        "device_id": state.device_id,
        "last_upload": record_dict(state.last_upload),
        "last_download": record_dict(state.last_download),
    }
    atomic_write_text(path, json.dumps(payload, indent=2) + "\n")
