"""Minimal Batocera lifecycle triggers for authoritative Quick SaveSync."""

from __future__ import annotations

import hashlib
import json
import os
import time
import uuid
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable, Optional, Protocol

from romcloud.core.exceptions import (
    SaveSyncError,
    SaveSyncVerificationError,
    SaveSyncWorkerBusyError,
)
from romcloud.core.models.savesync import SaveGroupCondition
from romcloud.core.progress import ProgressEvent
from romcloud.core.save_selection import SaveSelectionPolicy
from romcloud.infrastructure.logging import get_logger
from romcloud.infrastructure.diagnostics import (
    correlated_operation,
    event as diagnostic_event,
    stage_timer,
)
from romcloud.infrastructure import savesync_prompts
from romcloud.integrations.batocera import auto_savesync as batocera_auto_savesync
from romcloud.services.saves import SaveSyncService

log = get_logger("auto-savesync")
_MENU_PULL_INTERVAL_SECONDS = 300.0
_DEFAULT_STABILITY_CHECKS = 4
_DEFAULT_STAGING_RETRIES = 2
# A detached drain-pending worker never blocks a lifecycle hook, so it can
# afford to wait much longer than an interactive trigger for a busy worker
# lock to free up — still bounded, never indefinitely.
_DRAIN_PENDING_LOCK_RETRY_ATTEMPTS = 300  # ~30s at the existing 0.1s poll interval


@dataclass(frozen=True)
class _SettledObservation:
    """One completed bounded stability proof and the observation that made it.

    ``manifest`` is the exact content observation the proof was concluded from,
    ``observed_at`` the ``time.monotonic()`` reading when it was taken, and
    ``window_started_at`` when the still-unbroken run of *equal* observations
    began. A later stage can therefore reuse the bytes already read and can
    account for how much of the required quiet window is already covered by
    observation rather than sleeping through it a second time.
    """

    stable: bool
    observations: int
    manifest: dict
    observed_at: float
    window_started_at: float


@dataclass(frozen=True)
class GameSession:
    system: str
    emulator: str
    core: str
    rom: str
    started_at: float
    boot_id: str
    sync_outcome: str = "not_attempted"
    """One of: not_attempted, unsupported, skipped, synchronized, unresolved.

    Records the outcome of gameStart's best-effort targeted pre-launch sync
    attempt, if any — never a launch gate, purely informational for gameStop
    and diagnostics. gameStop always performs its own independent, fresh
    local/remote comparison regardless of this value.
    """
    sync_group_ids: tuple[str, ...] = ()
    """The save group ID(s) gameStart resolved as this game's own targeted
    pre-launch sync scope, regardless of the resulting outcome."""


def layout_ids_for_session(
    policy: SaveSelectionPolicy,
    system: str,
    emulator: str = "",
    core: str = "",
) -> frozenset[str]:
    """Delegate Batocera lifecycle targeting to the positive registry."""
    return policy.layout_ids_for_lifecycle(
        system=system,
        emulator=emulator,
        core=core,
    )


class ActiveSessionStore:
    """Crash-safe per-game marker files; no shared lock can delay game launch."""

    def __init__(self, data_root: Path) -> None:
        self._root = Path(data_root) / "savesync-sessions"

    def start(self, *, system: str, emulator: str, core: str, rom: str) -> GameSession:
        session = GameSession(
            system=system,
            emulator=emulator,
            core=core,
            rom=rom,
            started_at=time.time(),
            boot_id=_boot_id(),
        )
        self._write(session)
        return session

    def record_sync_outcome(
        self,
        *,
        system: str,
        rom: str,
        outcome: str,
        group_ids: tuple[str, ...] = (),
    ) -> None:
        """Update the already-written session marker with gameStart's
        best-effort pre-launch sync outcome. A missing/stale-boot marker
        (already retired or never written) is a silent no-op — this is pure
        diagnostics/gameStop context, never a source of truth gameStart's own
        launch-continuation behavior depends on.
        """
        target = self._path(system, rom)
        session = self._read(target)
        if session is None:
            return
        self._write(
            replace(session, sync_outcome=outcome, sync_group_ids=tuple(group_ids))
        )

    def _write(self, session: GameSession) -> None:
        self._root.mkdir(parents=True, exist_ok=True)
        target = self._path(session.system, session.rom)
        temporary = self._root / f".{target.name}.{uuid.uuid4().hex}.tmp"
        payload = json.dumps(session.__dict__, sort_keys=True, separators=(",", ":"))
        temporary.write_text(payload, encoding="utf-8")
        temporary.replace(target)

    def stop(self, *, system: str, rom: str) -> Optional[GameSession]:
        target = self._path(system, rom)
        session = self._read(target)
        try:
            target.unlink()
        except FileNotFoundError:
            pass
        return session

    def active_layout_ids(self, policy: SaveSelectionPolicy) -> frozenset[str]:
        result: set[str] = set()
        for session in self._active_sessions():
            result.update(
                layout_ids_for_session(
                    policy, session.system, session.emulator, session.core
                )
            )
        return frozenset(result)

    def has_active_session(self) -> bool:
        """Return whether any current Batocera gameplay marker exists."""
        return next(iter(self._active_sessions()), None) is not None

    def _active_sessions(self) -> tuple[GameSession, ...]:
        if not self._root.is_dir() or self._root.is_symlink():
            return ()
        try:
            entries = tuple(self._root.iterdir())
        except OSError:
            return ()
        result: list[GameSession] = []
        for path in entries:
            if path.suffix != ".json" or path.is_symlink() or not path.is_file():
                continue
            session = self._read(path)
            if session is not None:
                result.append(session)
        return tuple(result)

    def _read(self, path: Path) -> Optional[GameSession]:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            raw_group_ids = payload.get("sync_group_ids", ())
            session = GameSession(
                system=str(payload["system"]),
                emulator=str(payload.get("emulator", "")),
                core=str(payload.get("core", "")),
                rom=str(payload["rom"]),
                started_at=float(payload["started_at"]),
                boot_id=str(payload["boot_id"]),
                sync_outcome=str(payload.get("sync_outcome", "not_attempted")),
                sync_group_ids=tuple(str(value) for value in raw_group_ids),
            )
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
            return None
        return session if session.boot_id == _boot_id() else None

    def _path(self, system: str, rom: str) -> Path:
        key = hashlib.sha256(f"{system}\0{rom}".encode("utf-8")).hexdigest()
        return self._root / f"{key}.json"


class SaveSyncProgressReporterLike(Protocol):
    """Duck-typed hook for the shared lifecycle SaveSync progress popup.

    Deliberately independent of :mod:`romcloud.ui.savesync_progress` — this
    service layer never imports pygame/subprocess machinery directly, and
    every real implementation must already be fail-open (never raise).
    """

    def stage(
        self,
        text: str,
        *,
        current: Optional[int] = None,
        total: Optional[int] = None,
    ) -> None: ...

    def close(self, ok: bool, message: Optional[str] = None) -> None: ...


class _NullSaveSyncProgress:
    """Default no-op progress reporter — every other trigger (periodic
    menu tick, remote reconnect, drain-pending) never shows a popup."""

    def stage(
        self,
        text: str,
        *,
        current: Optional[int] = None,
        total: Optional[int] = None,
    ) -> None:  # noqa: ARG002
        return None

    def close(self, ok: bool, message: Optional[str] = None) -> None:  # noqa: ARG002
        return None


class _SafeProgress:
    """Defensive wrapper guaranteeing a broken/misbehaving progress reporter
    can never affect the real SaveSync outcome. Even though every real
    implementation is documented as fail-open already, gameStop's
    correctness must not depend on that promise being upheld correctly —
    any exception raised by ``stage``/``close`` is caught and logged here,
    never propagated into the SaveSync control flow above it."""

    def __init__(self, inner: SaveSyncProgressReporterLike) -> None:
        self._inner = inner
        self._last_stage: Optional[tuple[str, Optional[int], Optional[int]]] = None

    def stage(
        self,
        text: str,
        *,
        current: Optional[int] = None,
        total: Optional[int] = None,
    ) -> None:
        stage = (text, current, total)
        if stage == self._last_stage:
            return
        self._last_stage = stage
        try:
            if current is None or total is None:
                self._inner.stage(text)
            else:
                self._inner.stage(text, current=current, total=total)
        except TypeError:
            # Compatibility with older/custom presentation-only reporters.
            # They still receive truthful phase text; only the optional byte
            # fields are omitted.
            try:
                self._inner.stage(text)
            except Exception:  # noqa: BLE001 - UI failures never affect SaveSync
                log.warning("SaveSync progress popup stage update failed", exc_info=True)
        except Exception:  # noqa: BLE001 - UI failures must never affect SaveSync
            log.warning("SaveSync progress popup stage update failed", exc_info=True)

    def close(self, ok: bool, message: Optional[str] = None) -> None:
        try:
            self._inner.close(ok, message)
        except Exception:  # noqa: BLE001 - UI failures must never affect SaveSync
            log.warning("SaveSync progress popup close failed", exc_info=True)


def _lifecycle_progress_sink(
    progress: SaveSyncProgressReporterLike,
) -> Callable[[ProgressEvent], None]:
    """Translate service events into the shared lifecycle popup phases.

    Reconciliation exposes trustworthy aggregate byte totals, but not a
    trustworthy incremental transfer callback. Both lifecycle edges therefore
    use the same indeterminate phase presentation instead of fabricating a
    percentage.
    """

    def report(event: ProgressEvent) -> None:
        byte_progress = (
            event.current,
            event.total,
        ) if event.current is not None and event.total is not None else (None, None)
        if event.stage == "preflight" and event.status == "running":
            progress.stage(
                "Comparing save versions…",
                current=byte_progress[0],
                total=byte_progress[1],
            )
            return
        if event.stage == "preflight":
            metadata = event.metadata or {}
            uploads = int(metadata.get("uploads", 0) or 0)
            downloads = int(metadata.get("downloads", 0) or 0)
            if uploads and downloads:
                progress.stage("Uploading and downloading saves…")
            elif uploads:
                progress.stage("Uploading save…")
            elif downloads:
                progress.stage("Downloading save…")
            elif int(metadata.get("conflicts", 0) or 0):
                progress.stage("Save conflict found.")
            return
        if event.stage == "verify" and event.status == "running":
            progress.stage(
                "Verifying save…",
                current=byte_progress[0],
                total=byte_progress[1],
            )

    return report


class AutoSaveSyncCoordinator:
    """Coalesce automatic triggers into one bounded, serialized worker."""

    def __init__(
        self,
        service: SaveSyncService,
        *,
        data_root: Path,
        enabled: bool,
        policy: Optional[SaveSelectionPolicy] = None,
        quiet_seconds: float = 1.0,
        stability_checks: int = _DEFAULT_STABILITY_CHECKS,
        staging_retries: int = _DEFAULT_STAGING_RETRIES,
        enabled_check: Optional[Callable[[], bool]] = None,
    ) -> None:
        self._service = service
        self._data_root = Path(data_root)
        self._policy = policy or (service.selection_policy if enabled else None)
        self._sessions = ActiveSessionStore(self._data_root)
        # ``quiet_seconds`` is retained as a compatibility-facing name, but is
        # now the interval between concrete content observations rather than a
        # blind post-exit delay.
        self._stability_interval = max(0.0, quiet_seconds)
        self._stability_checks = max(1, stability_checks)
        self._staging_retries = max(0, staging_retries)
        self._enabled = enabled
        self._enabled_check = enabled_check
        self._menu_state_path = self._data_root / "savesync-menu-pull.json"

    @correlated_operation("gameStart", subsystem="savesync", source="Auto gameStart")
    def game_start(
        self,
        *,
        system: str,
        emulator: str,
        core: str,
        rom: str,
        progress: Optional[SaveSyncProgressReporterLike] = None,
    ) -> tuple[str, ...]:
        """Record the lifecycle marker, then best-effort pre-launch sync.

        SaveSync must never hold the game hostage: the marker is written
        first (pure local bookkeeping), and every step after that which
        could touch the remote is wrapped so that any failure is recorded
        as an outcome on the session marker and swallowed here \u2014 gameStart
        always returns normally so the caller launches the game regardless.
        """
        if not self._enabled:
            return ()
        with stage_timer("lifecycle-session-record"):
            session = self._sessions.start(
                system=system, emulator=emulator, core=core, rom=rom
            )
        session_path = self._sessions._path(system, rom)
        log.info(
            "gameStart session recorded: system=%s emulator=%s core=%s rom=%s "
            "session_id=%s session_path=%s started_at=%.6f boot_id=%s",
            system,
            emulator,
            core,
            rom,
            session_path.stem,
            session_path,
            session.started_at,
            session.boot_id,
        )
        diagnostic_event(
            "savesync",
            "session.created",
            "gameStart session recorded",
            metadata={
                "raw_system": system,
                "emulator": emulator,
                "core": core,
                "rom": rom,
                "session_id": session_path.stem,
                "session_path": str(session_path),
                "started_at": session.started_at,
                "boot_id": session.boot_id,
            },
        )
        return self._game_start_sync(
            system=system,
            emulator=emulator,
            core=core,
            rom=rom,
            progress=progress,
        )

    def game_start_eligible(
        self, *, system: str, emulator: str, core: str, rom: str
    ) -> bool:
        """Return whether gameStart has a locally provable safe target."""
        if not self._enabled:
            return False
        with stage_timer("lifecycle-target-resolution"):
            layout_ids = layout_ids_for_session(self._policy, system, emulator, core)
        if not layout_ids:
            return False
        try:
            return bool(self._resolve_game_start_targets(layout_ids, rom))
        except Exception:  # noqa: BLE001 - resolution failure remains fail-open
            return False

    def _resolve_game_start_targets(
        self, layout_ids: frozenset[str], rom: str
    ) -> dict[str, str]:
        """Map each resolved layout to its provably safe pre-launch target.

        A shared/container layout never gets a per-ROM guess: it only gets
        ``shared_container_group_id``'s structural single-container
        invariant. Every other layout only gets ``group_id_for_rom``'s
        ROM-name-derived group. A layout resolving to neither is simply
        absent from the result — callers must skip it, never widen to a
        broad scan.
        """
        group_layout_map: dict[str, str] = {}
        for layout_id in sorted(layout_ids):
            layout = self._policy.layout(layout_id)
            if layout.shared or layout.container_adapter_id:
                group_id = self._policy.shared_container_group_id(layout_id)
            else:
                group_id = self._policy.group_id_for_rom(layout_id, rom)
            if group_id is not None:
                group_layout_map[group_id] = layout_id
        return group_layout_map

    def _game_start_sync(
        self,
        *,
        system: str,
        emulator: str,
        core: str,
        rom: str,
        progress: Optional[SaveSyncProgressReporterLike] = None,
    ) -> tuple[str, ...]:
        layout_ids = layout_ids_for_session(self._policy, system, emulator, core)
        if not layout_ids:
            log.info(
                "gameStart pre-launch sync skipped: system=%s emulator=%s core=%s "
                "reason=unsupported-system",
                system,
                emulator,
                core,
            )
            self._sessions.record_sync_outcome(
                system=system, rom=rom, outcome="unsupported"
            )
            return ()

        try:
            with stage_timer("lifecycle-target-resolution"):
                group_layout_map = self._resolve_game_start_targets(layout_ids, rom)
        except Exception:  # noqa: BLE001 - gameStart must never block a launch
            log.warning(
                "gameStart target resolution failed; continuing launch: "
                "system=%s emulator=%s core=%s rom=%s layout_ids=%s",
                system,
                emulator,
                core,
                rom,
                ",".join(sorted(layout_ids)),
                exc_info=True,
            )
            diagnostic_event(
                "savesync",
                "session.sync_unresolved",
                "gameStart target resolution failed; launch continuing",
                level="WARNING",
                metadata={
                    "raw_system": system,
                    "emulator": emulator,
                    "core": core,
                    "rom": rom,
                    "layout_ids": sorted(layout_ids),
                },
            )
            self._sessions.record_sync_outcome(
                system=system, rom=rom, outcome="unresolved"
            )
            return ()

        if not group_layout_map:
            # Neither a provable per-ROM target (group_id_for_rom) nor a
            # structurally-guaranteed single shared container
            # (shared_container_group_id) exists for any resolved layout.
            # Never guess or widen to a broad scan here — skip pre-launch
            # sync for this launch and continue, leaving full reconciliation
            # to gameStop as before.
            log.info(
                "gameStart pre-launch sync skipped: system=%s emulator=%s core=%s "
                "rom=%s layout_ids=%s reason=no-safe-target",
                system,
                emulator,
                core,
                rom,
                ",".join(sorted(layout_ids)),
            )
            diagnostic_event(
                "savesync",
                "session.sync_skipped",
                "gameStart pre-launch sync skipped: no safe target",
                metadata={
                    "raw_system": system,
                    "emulator": emulator,
                    "core": core,
                    "rom": rom,
                    "layout_ids": sorted(layout_ids),
                },
            )
            self._sessions.record_sync_outcome(
                system=system, rom=rom, outcome="skipped"
            )
            return ()

        target_group_ids = tuple(sorted(group_layout_map))
        progress = _SafeProgress(
            progress if progress is not None else _NullSaveSyncProgress()
        )
        progress.stage("Checking save…")
        progress.stage("Checking remote state…")
        progress.stage("Comparing save versions…")
        try:
            result = self._service.targeted_game_start_sync(
                group_layout_map,
                progress=_lifecycle_progress_sink(progress),
            )
        except Exception:  # noqa: BLE001 - gameStart must never block a launch
            log.warning(
                "gameStart pre-launch sync attempt failed; continuing launch: "
                "system=%s emulator=%s core=%s rom=%s group_ids=%s",
                system,
                emulator,
                core,
                rom,
                ",".join(target_group_ids),
                exc_info=True,
            )
            diagnostic_event(
                "savesync",
                "session.sync_unresolved",
                "gameStart pre-launch sync failed; launch continuing",
                level="WARNING",
                metadata={
                    "raw_system": system,
                    "emulator": emulator,
                    "core": core,
                    "rom": rom,
                    "group_ids": list(target_group_ids),
                },
            )
            self._sessions.record_sync_outcome(
                system=system,
                rom=rom,
                outcome="unresolved",
                group_ids=target_group_ids,
            )
            progress.close(
                False,
                "Save sync unavailable.\nLaunching with your local save.",
            )
            return ()

        conflict_ids: tuple[str, ...] = ()
        if result.status == "unresolved" and result.reason == "conflict":
            try:
                conflict_ids = tuple(
                    sorted(
                        conflict.conflict_id
                        for conflict in self._service.get_state().active_conflicts
                        if conflict.group_id in target_group_ids
                    )
                )
                if conflict_ids:
                    savesync_prompts.enqueue(self._data_root, conflict_ids)
                    log.info(
                        "Persisted %d gameStart conflict prompt(s): queue=%s ids=%s",
                        len(conflict_ids),
                        savesync_prompts.queue_path(self._data_root),
                        ",".join(conflict_ids),
                    )
            except Exception:  # noqa: BLE001 - launch remains fail-open
                log.warning(
                    "Could not persist gameStart conflict prompt queue; "
                    "conflict evidence remains in SaveSync state",
                    exc_info=True,
                )

        log.info(
            "gameStart pre-launch sync outcome: system=%s emulator=%s core=%s "
            "rom=%s group_ids=%s status=%s reason=%s",
            system,
            emulator,
            core,
            rom,
            ",".join(target_group_ids),
            result.status,
            result.reason,
        )
        diagnostic_event(
            "savesync",
            "session.sync_completed",
            "gameStart pre-launch sync completed",
            metadata={
                "raw_system": system,
                "emulator": emulator,
                "core": core,
                "rom": rom,
                "group_ids": list(target_group_ids),
                "status": result.status,
                "reason": result.reason,
            },
        )
        self._sessions.record_sync_outcome(
            system=system,
            rom=rom,
            outcome=result.status,
            group_ids=target_group_ids,
        )
        if conflict_ids:
            progress.close(True, "Save conflict found.")
        elif result.status == "unresolved":
            progress.close(
                False,
                "Save sync needs attention.\nLaunching with your local save.",
            )
        else:
            progress.close(True, "Save is current.")
        return conflict_ids

    def game_stop_eligible(self, *, system: str, emulator: str, core: str) -> bool:
        """Return whether a stop owns a code-supported automatic layout."""
        return bool(layout_ids_for_session(self._policy, system, emulator, core))

    @correlated_operation(
        "Auto Quick Sync", subsystem="savesync", source="Auto gameStop"
    )
    def game_stop(
        self,
        *,
        system: str,
        emulator: str,
        core: str,
        rom: str,
        progress: Optional[SaveSyncProgressReporterLike] = None,
    ) -> tuple[str, ...]:
        if not self._enabled:
            log.info("gameStop conflict check skipped: Auto SaveSync disabled")
            return ()
        layout_ids = layout_ids_for_session(self._policy, system, emulator, core)
        canonical_systems = tuple(
            sorted({self._policy.layout(value).system for value in layout_ids})
        )
        diagnostic_event(
            "savesync",
            "lifecycle.resolved",
            "gameStop lifecycle identity resolved",
            metadata={
                "raw_system": system,
                "normalized_system": system.strip().casefold(),
                "emulator": emulator,
                "core": core,
                "rom": rom,
                "matched_layout_ids": sorted(layout_ids),
                "canonical_systems": canonical_systems,
                "reason": "eligible" if layout_ids else "ineligible",
            },
        )
        if not self.game_stop_eligible(
            system=system, emulator=emulator, core=core
        ):
            # A lifecycle marker is bookkeeping rather than SaveSync work and
            # must still be retired for every observed gameStop. Eligibility
            # is decided before popup, observation, worker, provider, or state
            # access so an unrelated application exit is a total sync no-op.
            self._sessions.stop(system=system, rom=rom)
            log.info(
                "gameStop SaveSync skipped: system=%s normalized_system=%s "
                "emulator=%s core=%s resolved_layout_ids=%s "
                "canonical_systems=%s reason=no-supported-layout",
                system,
                system.strip().casefold() or "none",
                emulator,
                core,
                ",".join(sorted(layout_ids)) or "none",
                ",".join(
                    sorted(
                        {
                            self._policy.layout(value).system
                            for value in layout_ids
                        }
                    )
                )
                or "none",
            )
            return ()
        # The progress popup is purely observational: it never owns or gates
        # any SaveSync decision below, and its failures are never allowed to
        # propagate (see NullSaveSyncProgress / SaveSyncProgressReporter).
        progress = _SafeProgress(progress if progress is not None else _NullSaveSyncProgress())
        progress.stage("Checking save changes…")
        log.info(
            "gameStop received: system=%s emulator=%s core=%s rom=%s",
            system,
            emulator,
            core,
            rom,
        )
        log.info(
            "gameStop SaveSync resolution: raw_system=%s normalized_system=%s "
            "matched_layout_ids=%s canonical_systems=%s",
            system,
            system.strip().casefold() or "none",
            ",".join(sorted(layout_ids)),
            ",".join(canonical_systems),
        )
        log.info(
            "gameStop conflict check started: system=%s emulator=%s core=%s",
            system,
            emulator,
            core,
        )
        try:
            with self._service.observation_scope():
                return self._game_stop_locked(
                    system=system,
                    emulator=emulator,
                    core=core,
                    rom=rom,
                    progress=progress,
                    layout_ids=layout_ids,
                )
        except Exception:
            progress.close(
                False,
                "Save sync failed.\nYour local save has been preserved.",
            )
            raise

    def _game_stop_locked(
        self,
        *,
        system: str,
        emulator: str,
        core: str,
        rom: str,
        progress: SaveSyncProgressReporterLike,
        layout_ids: frozenset[str],
    ) -> tuple[str, ...]:
        settled: Optional[_SettledObservation] = None
        stopped_at = time.time()
        with stage_timer("scope"):
            session = self._sessions.stop(system=system, rom=rom)
        log.info(
            "gameStop SaveSync scope: system=%s emulator=%s core=%s "
            "layout_count=%d layout_ids=%s session_record=%s",
            system,
            emulator,
            core,
            len(layout_ids),
            ",".join(sorted(layout_ids)) or "none",
            "present" if session is not None else "missing",
        )
        diagnostic_event(
            "savesync",
            "session.stopped",
            "gameStop session marker retired",
            metadata={
                "raw_system": system,
                "emulator": emulator,
                "core": core,
                "rom": rom,
                "session_id": self._sessions._path(system, rom).stem,
                "session_path": str(self._sessions._path(system, rom)),
                "session_record": "present" if session is not None else "missing",
                "session_started_at": (
                    session.started_at if session is not None else None
                ),
                "game_stop_at": stopped_at,
                "session_duration_seconds": (
                    max(0.0, stopped_at - session.started_at)
                    if session is not None
                    else None
                ),
                "layout_ids": sorted(layout_ids),
            },
        )
        if layout_ids:
            scope = tuple(
                {
                    "layout_id": layout_id,
                    "canonical_system": self._policy.layout(layout_id).system,
                    "root_pattern": self._policy.layout(layout_id).root_pattern,
                }
                for layout_id in sorted(layout_ids)
            )
            log.info(
                "gameStop targeted observation scope: layout_count=%d scope=%s",
                len(scope),
                json.dumps(scope, sort_keys=True, separators=(",", ":")),
            )
            diagnostic_event(
                "savesync",
                "local_observation.scope",
                "gameStop targeted local observation scope resolved",
                metadata={
                    "scope": scope,
                    "layout_ids": sorted(layout_ids),
                    "count": len(scope),
                },
            )
            progress.stage("Waiting for save data to settle…")
            log.info(
                "gameStop waiting for save stability: layout_ids=%s "
                "bounded_checks=%d interval=%.2fs",
                ",".join(sorted(layout_ids)),
                self._stability_checks,
                self._stability_interval,
            )
            with stage_timer("stability") as timing:
                settled = self._settle(
                    lambda: self._service.observe_local_layouts(layout_ids)
                )
                timing["observations"] = settled.observations
                timing["result"] = "stable" if settled.stable else "unstable"
            if settled.stable:
                log.info(
                    "gameStop save stability achieved: layout_ids=%s "
                    "observations=%d",
                    ",".join(sorted(layout_ids)),
                    settled.observations,
                )
            else:
                log.warning(
                    "gameStop save stability timeout: layout_ids=%s "
                    "bounded_checks=%d; local discovery skipped this pass to "
                    "avoid classifying an in-flight write",
                    ",".join(sorted(layout_ids)),
                    self._stability_checks,
                )
                log.warning(
                    "Auto SaveSync final result: trigger=game stop status=deferred "
                    "reason=local-data-unstable-pre-discovery "
                    "durable_dirty_state_retained=true"
                )
                raise SaveSyncError(
                    "Auto SaveSync save data did not stabilize before discovery; "
                    "pending local work was retained."
                )
            changed_since = (
                session.started_at if session is not None else time.time() - 5.0
            )
            log.info(
                "gameStop local discovery started: layout_ids=%s",
                ",".join(sorted(layout_ids)),
            )
            with stage_timer("discovery"):
                state_before = self._service.get_state()
                dirty_before = _local_dirty_group_ids(state_before)
                ownership_groups = _ownership_groups(
                    settled.manifest, self._policy
                )
                state_after = self._service.detect_and_mark_local_changes(
                    layout_ids,
                    changed_since=changed_since,
                    observed=settled.manifest,
                )
                dirty_after = _local_dirty_group_ids(state_after)
                log.info(
                    "gameStop dirty-state commit: ownership_groups=%s "
                    "dirty_before=%s dirty_after=%s newly_dirty=%s "
                    "state_commit=%s",
                    ",".join(ownership_groups) or "none",
                    ",".join(dirty_before) or "none",
                    ",".join(dirty_after) or "none",
                    ",".join(sorted(set(dirty_after) - set(dirty_before))) or "none",
                    "updated" if state_after != state_before else "unchanged",
                )
                diagnostic_event(
                    "savesync",
                    "dirty_state.committed",
                    "gameStop dirty-state classification committed",
                    metadata={
                        "layout_ids": sorted(layout_ids),
                        "ownership_groups": ownership_groups,
                        "dirty_before": dirty_before,
                        "dirty_after": dirty_after,
                        "newly_dirty": sorted(
                            set(dirty_after) - set(dirty_before)
                        ),
                        "state_commit": (
                            "updated" if state_after != state_before else "unchanged"
                        ),
                    },
                )
        if self._sessions.has_active_session():
            log.info(
                "Auto SaveSync final result: trigger=game stop status=deferred "
                "reason=another-session-active durable_dirty_state_retained=true"
            )
            progress.close(True, "Save sync deferred.")
            return ()
        progress.stage("Preparing save sync…")
        conflict_ids = self._run_quick_sync(
            trigger="game stop",
            wait_for_handoff=True,
            collect_new_conflicts=True,
            require_completion=True,
            progress=progress,
            settled=settled,
        )
        log.info(
            "gameStop conflict check complete: new_conflicts=%d ids=%s",
            len(conflict_ids),
            ",".join(conflict_ids) if conflict_ids else "none",
        )
        # ``stage`` above already set the truthful final phrase ("No save
        # changes detected."/"Save sync complete."); ``close`` here only
        # starts the auto-dismiss countdown without overwriting it.
        progress.close(True)
        return conflict_ids

    @correlated_operation(
        "Auto Quick Sync", subsystem="savesync", source="remote reconnect"
    )
    def remote_reconnect(self) -> None:
        """Run one eligible Quick Sync after a detached reconnect edge."""
        if not self._enabled:
            return
        state = self._service.get_state()
        if (
            not state.quick_sync_ready
            or state.quick_sync_cursor_generation is None
        ):
            return
        self._run_quick_sync(trigger="remote-data reconnect")

    @correlated_operation(
        "Auto Quick Sync", subsystem="savesync", source="periodic Auto"
    )
    def menu_tick(self, *, force: bool = False) -> None:
        if not self._enabled:
            return
        if not force and not self._menu_pull_due():
            return
        self._run_quick_sync(trigger="periodic menu")

    def _run_quick_sync(
        self,
        *,
        trigger: str,
        wait_for_handoff: bool = False,
        collect_new_conflicts: bool = False,
        require_completion: bool = False,
        lock_retry_attempts: Optional[int] = None,
        progress: Optional[SaveSyncProgressReporterLike] = None,
        settled: Optional[_SettledObservation] = None,
    ) -> tuple[str, ...]:
        """Serialize every automatic trigger through ``SaveSyncService.quick_sync``.

        Local-dirty groups receive the existing bounded settling observations
        before Quick Sync.  Quick Sync itself remains the sole authority for
        journal scoping and three-way upload/download/conflict decisions.

        *settled* carries a stability proof a caller already completed for the
        same local content (gameStop's pre-discovery settle). It only ever
        seeds the first of the two observations still required here — never
        replaces the proof.
        """
        lock = _AutoWorkerLock(self._data_root / ".savesync-auto.lock")
        new_conflict_ids: set[str] = set()
        attempts = lock_retry_attempts or (6 if wait_for_handoff else 1)
        with stage_timer("worker-lock", metadata={"trigger": trigger}) as timing:
            acquired = False
            used = 0
            for attempt in range(attempts):
                used = attempt + 1
                if lock.acquire():
                    acquired = True
                    break
                if attempt == attempts - 1:
                    break
                # A just-finishing leader may have completed its final durable
                # state read while gameStop was recording new work.
                time.sleep(0.1)
            timing["attempts"] = used
            timing["result"] = "acquired" if acquired else "busy"
        if not acquired:
            log.warning(
                "Auto SaveSync final result: trigger=%s status=deferred "
                "reason=worker-busy",
                trigger,
            )
            diagnostic_event(
                "savesync", "worker.busy", "Auto SaveSync worker is busy",
                level="WARNING",
                metadata={
                    "trigger": trigger, "status": "deferred",
                    "reason": "worker-busy", "worker_state": "busy",
                },
            )
            if require_completion:
                raise SaveSyncWorkerBusyError(
                    "Auto SaveSync could not acquire its worker lock "
                    "because another Quick Sync was still running; "
                    "pending local work was retained and a follow-up "
                    "sync will be scheduled."
                )
            return ()
        log.info("Auto SaveSync worker lock acquired: trigger=%s", trigger)
        try:
            if self._sessions.has_active_session():
                log.info(
                    "Auto SaveSync final result: trigger=%s status=deferred "
                    "reason=active-session",
                    trigger,
                )
                if require_completion:
                    raise SaveSyncError(
                        "Auto SaveSync found another active game session; "
                        "pending local work was retained."
                    )
                return ()
            for _ in range(32):
                state = self._service.get_state()
                group_layouts = {
                    group.group_id: group.layout_id for group in state.groups
                }
                pending = frozenset(
                    group.group_id
                    for group in state.groups
                    if self._policy.is_lifecycle_enabled(group.layout_id)
                    and (
                        group.condition is SaveGroupCondition.LOCAL_DIRTY
                        or bool(group.dirty_path_hints)
                    )
                )
                log.info(
                    "Auto SaveSync pass: trigger=%s quick_ready=%s cursor=%s "
                    "tracked_groups=%d pending_local_groups=%d",
                    trigger,
                    state.quick_sync_ready,
                    (
                        state.quick_sync_cursor_generation
                        if state.quick_sync_cursor_generation is not None
                        else "none"
                    ),
                    len(state.groups),
                    len(pending),
                )
                if pending and not self._wait_until_stable(pending, seed=settled):
                    log.warning(
                        "Auto SaveSync deferred: local save data did not "
                        "stabilize after %d bounded checks; durable dirty "
                        "state retained",
                        self._stability_checks,
                    )
                    log.warning(
                        "Auto SaveSync final result: trigger=%s status=deferred "
                        "reason=local-data-unstable pending_local_groups=%d",
                        trigger,
                        len(pending),
                    )
                    if require_completion:
                        raise SaveSyncError(
                            "Auto SaveSync local save data did not stabilize; "
                            "pending local work was retained."
                        )
                    return ()

                def is_group_active(group_id: str) -> bool:
                    return group_layouts.get(
                        group_id
                    ) in self._sessions.active_layout_ids(self._policy)

                def is_layout_active(layout_id: str) -> bool:
                    return layout_id in self._sessions.active_layout_ids(
                        self._policy
                    )

                result = None
                conflicts_before = frozenset(
                    conflict.conflict_id for conflict in state.active_conflicts
                )
                if collect_new_conflicts:
                    log.info(
                        "gameStop Quick Sync preflight conflicts=%d ids=%s",
                        len(conflicts_before),
                        ",".join(sorted(conflicts_before)) or "none",
                    )
                log.info(
                    "Auto SaveSync quick sync started: trigger=%s "
                    "pending_local_groups=%d",
                    trigger,
                    len(pending),
                )
                try:
                    for staging_attempt in range(self._staging_retries + 1):
                        try:
                            with stage_timer("quick-sync"):
                                result = self._service.quick_sync(
                                    progress=(
                                        _lifecycle_progress_sink(progress)
                                        if progress is not None
                                        else None
                                    ),
                                    is_group_active=is_group_active,
                                    is_layout_active=is_layout_active,
                                    exclude_layout_ids=(
                                        self._policy.lifecycle_disabled_layout_ids()
                                    ),
                                )
                            break
                        except SaveSyncVerificationError:
                            if staging_attempt >= self._staging_retries:
                                raise
                            log.warning(
                                "Auto SaveSync detected save data changing during "
                                "staging; waiting for stability before retry %d/%d",
                                staging_attempt + 1,
                                self._staging_retries,
                            )
                            retry_pending = self._pending_local_groups()
                            if retry_pending and not self._wait_until_stable(
                                retry_pending
                            ):
                                log.warning(
                                    "Auto SaveSync deferred: local save data remained "
                                    "unstable; durable dirty state retained"
                                )
                                if require_completion:
                                    raise SaveSyncError(
                                        "Auto SaveSync local save data remained "
                                        "unstable; pending local work was retained."
                                    )
                                return ()
                except Exception:  # noqa: BLE001 - detached work is best-effort
                    log.warning(
                        "Auto SaveSync %s Quick Sync deferred", trigger, exc_info=True
                    )
                    log.warning(
                        "Auto SaveSync final result: trigger=%s status=failed "
                        "reason=exception",
                        trigger,
                    )
                    if require_completion:
                        raise
                    return ()
                if result is None:
                    log.warning(
                        "Auto SaveSync final result: trigger=%s status=failed "
                        "reason=missing-quick-sync-result",
                        trigger,
                    )
                    if require_completion:
                        raise SaveSyncError(
                            "Auto SaveSync ended without a Quick Sync result; "
                            "pending local work was retained."
                        )
                    return self._still_active_conflicts(
                        new_conflict_ids, enqueue=collect_new_conflicts
                    )
                if progress is not None:
                    if result.status == "unchanged":
                        progress.stage("No save changes detected.")
                    elif result.report is not None and result.report.conflicts:
                        progress.stage("Save conflict found.")
                    else:
                        progress.stage("Save sync complete.")
                log.info(
                    "Auto SaveSync Quick Sync result: trigger=%s status=%s "
                    "reason=%s remote_generation=%d cursor_before=%s "
                    "cursor_after=%s processed_entries=%d processed_groups=%d "
                    "uploaded=%d downloaded=%d conflicts=%d",
                    trigger,
                    result.status,
                    result.reason or "none",
                    result.remote_generation,
                    result.cursor_before if result.cursor_before is not None else "none",
                    result.cursor_after if result.cursor_after is not None else "none",
                    result.processed_entries,
                    len(result.processed_groups),
                    result.report.uploaded if result.report is not None else 0,
                    result.report.downloaded if result.report is not None else 0,
                    result.report.conflicts if result.report is not None else 0,
                )
                diagnostic_event(
                    "savesync", "operation.result", "Auto Quick Sync completed",
                    metadata={
                        "trigger": trigger, "status": result.status,
                        "reason": result.reason,
                        "generation": result.remote_generation,
                        "cursor_before": result.cursor_before,
                        "cursor_after": result.cursor_after,
                        "processed_entries": result.processed_entries,
                        "processed_groups": result.processed_groups,
                        "uploaded": result.report.uploaded if result.report else 0,
                        "downloaded": result.report.downloaded if result.report else 0,
                        "conflicts": result.report.conflicts if result.report else 0,
                        "unchanged": result.report.unchanged if result.report else 0,
                    },
                )
                if result.status in {"deferred", "requires-full-sync"}:
                    log.warning(
                        "Auto SaveSync final result: trigger=%s status=%s reason=%s",
                        trigger,
                        result.status,
                        result.reason or "none",
                    )
                    if require_completion:
                        raise SaveSyncError(
                            "Auto SaveSync Quick Sync did not complete "
                            f"({result.status}: {result.reason or 'no reason'}); "
                            "pending local work was retained."
                        )
                    return self._still_active_conflicts(
                        new_conflict_ids, enqueue=collect_new_conflicts
                    )
                if collect_new_conflicts:
                    conflicts_after = frozenset(
                        conflict.conflict_id
                        for conflict in self._service.get_state().active_conflicts
                    )
                    new_conflict_ids.update(conflicts_after - conflicts_before)
                    log.info(
                        "gameStop Quick Sync result=%s conflicts_after=%d new_ids=%s",
                        result.status,
                        len(conflicts_after),
                        ",".join(sorted(conflicts_after - conflicts_before))
                        or "none",
                    )
                after = self._pending_local_groups()
                if after == pending and after and require_completion:
                    log.warning(
                        "Auto SaveSync final result: trigger=%s status=failed "
                        "reason=pending-local-work-no-progress "
                        "remaining_pending_local_groups=%d",
                        trigger,
                        len(after),
                    )
                    raise SaveSyncError(
                        "Auto SaveSync Quick Sync made no progress while local work "
                        "remained pending."
                    )
                if not after or after == pending:
                    self._write_menu_pull_timestamp(time.time())
                    log.info(
                        "Auto SaveSync final result: trigger=%s status=%s "
                        "remaining_pending_local_groups=%d",
                        trigger,
                        result.status,
                        len(after),
                    )
                    return self._still_active_conflicts(
                        new_conflict_ids, enqueue=collect_new_conflicts
                    )
        finally:
            lock.release()
        if require_completion:
            raise SaveSyncError(
                "Auto SaveSync did not drain pending local work within its bounded "
                "passes; pending local work was retained."
            )
        return self._still_active_conflicts(
            new_conflict_ids, enqueue=collect_new_conflicts
        )

    def _still_active_conflicts(
        self, conflict_ids: set[str], *, enqueue: bool = False
    ) -> tuple[str, ...]:
        if not conflict_ids:
            return ()
        active = {
            conflict.conflict_id for conflict in self._service.get_state().active_conflicts
        }
        result = tuple(sorted(conflict_ids & active))
        if enqueue and result:
            try:
                savesync_prompts.enqueue(self._data_root, result)
            except Exception:
                log.error(
                    "Could not persist gameStop conflict prompt queue: queue=%s ids=%s",
                    savesync_prompts.queue_path(self._data_root),
                    ",".join(result),
                    exc_info=True,
                )
                raise
            log.info(
                "Persisted %d gameStop conflict prompt(s): queue=%s ids=%s",
                len(result),
                savesync_prompts.queue_path(self._data_root),
                ",".join(result),
            )
        return result

    def menu_loop(self) -> None:
        if not self._menu_loop_enabled():
            return
        loop_lock = _AutoWorkerLock(self._data_root / ".savesync-menu-loop.lock")
        if not loop_lock.acquire():
            return
        try:
            batocera_auto_savesync.record_menu_loop_pid(self._data_root)
            log.info("Auto SaveSync periodic menu loop started")
            self.menu_tick(force=True)
            while True:
                for _ in range(int(_MENU_PULL_INTERVAL_SECONDS)):
                    time.sleep(1.0)
                if not self._menu_loop_enabled():
                    log.info(
                        "Auto SaveSync periodic menu loop stopped: Auto Sync disabled"
                    )
                    return
                self.menu_tick(force=False)
        finally:
            batocera_auto_savesync.clear_menu_loop_pid(self._data_root)
            loop_lock.release()

    def _menu_loop_enabled(self) -> bool:
        if not self._enabled:
            return False
        if self._enabled_check is None:
            return True
        try:
            return bool(self._enabled_check())
        except Exception:  # noqa: BLE001 - fail closed for resident work
            log.warning(
                "Auto SaveSync periodic menu loop stopped: configuration "
                "could not be refreshed",
                exc_info=True,
            )
            return False

    def _menu_pull_due(self) -> bool:
        last = self._read_menu_pull_timestamp()
        if last is None:
            return True
        return (time.time() - last) >= _MENU_PULL_INTERVAL_SECONDS

    def _read_menu_pull_timestamp(self) -> Optional[float]:
        try:
            payload = json.loads(self._menu_state_path.read_text(encoding="utf-8"))
            value = payload.get("last_pull")
            return float(value)
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return None

    def _write_menu_pull_timestamp(self, value: float) -> None:
        self._menu_state_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self._menu_state_path.with_name(
            f".{self._menu_state_path.name}.{uuid.uuid4().hex}.tmp"
        )
        temporary.write_text(json.dumps({"last_pull": value}), encoding="utf-8")
        temporary.replace(self._menu_state_path)

    @correlated_operation(
        "Auto Quick Sync", subsystem="savesync", source="drain-pending"
    )
    def drain_pending(self) -> None:
        """Guaranteed follow-up sync after a busy worker released its lock.

        This is the coalescing target for a gameStop (or other trigger) that
        durably recorded local-dirty state but could not itself acquire the
        worker lock because another Manual/Auto Quick Sync was still
        running. It waits considerably longer than an interactive trigger
        may (still bounded, never indefinitely) since it always runs
        detached and never blocks a lifecycle hook.
        """
        if not self._enabled:
            return
        if not self._pending_local_groups():
            return
        self._run_quick_sync(
            trigger="pending work",
            wait_for_handoff=True,
            lock_retry_attempts=_DRAIN_PENDING_LOCK_RETRY_ATTEMPTS,
        )

    def _pending_local_groups(self) -> frozenset[str]:
        state = self._service.get_state()
        return frozenset(
            group.group_id
            for group in state.groups
            if self._policy.is_lifecycle_enabled(group.layout_id)
            and (
                group.condition is SaveGroupCondition.LOCAL_DIRTY
                or bool(group.dirty_path_hints)
            )
        )

    def _wait_until_stable(
        self,
        group_ids: frozenset[str],
        *,
        seed: Optional[_SettledObservation] = None,
    ) -> bool:
        """Require two equal local hash/size observations within a bound.

        When *seed* is a proof taken over a wider local scope moments ago, its
        manifest is narrowed to *group_ids* and used as the first of the two
        required observations. A seed that does not cover these exact groups
        simply fails the equality comparison, which costs one extra
        observation and can never report stability that was not observed.
        """
        seeded: Optional[_SettledObservation] = None
        if seed is not None and seed.stable:
            seeded = replace(
                seed,
                manifest=self._service.groups_within(seed.manifest, group_ids),
            )
        with stage_timer("stability-preflight") as timing:
            result = self._settle(
                lambda: self._service.observe_local_groups(group_ids), seed=seeded
            )
            timing["observations"] = result.observations
            timing["result"] = "stable" if result.stable else "unstable"
        return result.stable

    def _settle(
        self,
        observe: Callable[[], dict],
        *,
        seed: Optional["_SettledObservation"] = None,
    ) -> "_SettledObservation":
        """Bounded proof that local save content has stopped changing.

        Stability is proven exactly as before: local content must be observed
        *unchanged* across a quiet window of at least
        ``self._stability_interval``. Without that proof a single upfront scan
        racing an emulator/core's still-in-flight save write can observe stale,
        baseline-matching bytes and permanently classify a real change as
        unchanged — nothing downstream re-scans a group that was never marked
        dirty, so the change would silently never sync.

        What changed is only the bookkeeping of that window. It is measured
        from the first observation of the current unbroken run of equal
        observations, so time already spent *reading* the tree counts towards
        it instead of being slept through again — a multi-second scan of a
        large PS2 save tree covers the whole window on its own. For the same
        reason a caller may hand over a proof it just completed as *seed*: its
        observation and window are real measurements, so one fresh matching
        observation extends that same unbroken quiet run rather than starting
        a new one. Any mismatch immediately restarts the window.
        """
        unavailable = object()
        previous: object = seed.manifest if seed is not None else unavailable
        window_started_at = seed.window_started_at if seed is not None else 0.0
        latest_at = seed.observed_at if seed is not None else 0.0
        observations = 0
        settle_started_at = time.monotonic()
        for attempt in range(1, self._stability_checks + 2):
            if previous is not unavailable and self._stability_interval:
                remaining = self._stability_interval - (
                    time.monotonic() - window_started_at
                )
                if remaining > 0:
                    time.sleep(remaining)
            try:
                current = observe()
            except OSError as exc:
                # An emulator may atomically replace a save between discovery
                # and hashing. Treat that bounded observation as unstable.
                previous = unavailable
                diagnostic_event(
                    "savesync",
                    "local_observation.failed",
                    "Scoped local save observation failed",
                    level="WARNING",
                    metadata={
                        "attempt": attempt,
                        "elapsed_ms": int(
                            (time.monotonic() - settle_started_at) * 1000
                        ),
                        "error_type": type(exc).__name__,
                    },
                )
                continue
            observations += 1
            latest_at = time.monotonic()
            matches_previous = previous is not unavailable and current == previous
            details = self._describe_observation(current)
            ownership_groups = _ownership_groups(current, self._policy)
            log.info(
                "Scoped local save observation: attempt=%d monotonic_ns=%d "
                "elapsed_ms=%d artifacts=%d ownership_groups=%s "
                "matches_previous=%s files_truncated=%d files=%s",
                attempt,
                time.monotonic_ns(),
                int((latest_at - settle_started_at) * 1000),
                len(current),
                ",".join(ownership_groups) or "none",
                matches_previous,
                max(0, len(current) - len(details)),
                json.dumps(details, sort_keys=True, separators=(",", ":")),
            )
            diagnostic_event(
                "savesync",
                "local_observation.completed",
                "Scoped local save observation completed",
                metadata={
                    "attempt": attempt,
                    "monotonic_ns": time.monotonic_ns(),
                    "elapsed_ms": int((latest_at - settle_started_at) * 1000),
                    "artifact_count": len(current),
                    "ownership_groups": ownership_groups,
                    "matches_previous": matches_previous,
                    "files_truncated": max(0, len(current) - len(details)),
                    "files": details,
                },
            )
            if matches_previous:
                return _SettledObservation(
                    True, observations, current, latest_at, window_started_at
                )
            previous = current
            window_started_at = latest_at
        return _SettledObservation(
            False,
            observations,
            {} if previous is unavailable else previous,  # type: ignore[arg-type]
            latest_at,
            window_started_at,
        )

    def _describe_observation(
        self, observed: dict
    ) -> tuple[dict[str, object], ...]:
        describe = getattr(self._service, "describe_local_observation", None)
        if callable(describe):
            try:
                return describe(observed)
            except Exception:  # noqa: BLE001 - diagnostics must be fail-open
                log.warning(
                    "Could not describe scoped local observation", exc_info=True
                )
        return tuple(
            {
                "canonical_path": path,
                "size_bytes": getattr(artifact, "size_bytes", None),
                "content_hash": getattr(artifact, "content_hash", None),
                "mtime_ns": None,
                "physical_path": "unavailable",
            }
            for path, artifact in sorted(observed.items())[:100]
        )


def _ownership_groups(
    observed: dict, policy: SaveSelectionPolicy
) -> tuple[str, ...]:
    return tuple(
        sorted(
            {
                descriptor.group_id
                for path in observed
                if (descriptor := policy.group_for_path(path)) is not None
            }
        )
    )


def _local_dirty_group_ids(state) -> tuple[str, ...]:  # noqa: ANN001
    return tuple(
        sorted(
            group.group_id
            for group in state.groups
            if group.condition is SaveGroupCondition.LOCAL_DIRTY
            or bool(group.dirty_path_hints)
        )
    )


class _AutoWorkerLock:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._handle = None

    def acquire(self) -> bool:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        handle = self._path.open("a+b")
        try:
            if os.name == "nt":
                import msvcrt

                handle.seek(0)
                if handle.tell() == handle.seek(0, os.SEEK_END):
                    handle.write(b"\0")
                    handle.flush()
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, BlockingIOError):
            handle.close()
            return False
        self._handle = handle
        return True

    def release(self) -> None:
        if self._handle is None:
            return
        try:
            if os.name == "nt":
                import msvcrt

                self._handle.seek(0)
                msvcrt.locking(self._handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
        finally:
            self._handle.close()
            self._handle = None


def _boot_id() -> str:
    try:
        return Path("/proc/sys/kernel/random/boot_id").read_text(encoding="ascii").strip()
    except OSError:
        return "unknown-boot"
