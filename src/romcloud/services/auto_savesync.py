"""Minimal Batocera game-lifecycle trigger for targeted SaveSync work."""

from __future__ import annotations

import hashlib
import json
import os
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from romcloud.core.exceptions import SaveSyncVerificationError
from romcloud.core.models.savesync import SaveGroupCondition
from romcloud.core.save_selection import SaveSelectionPolicy
from romcloud.infrastructure.logging import get_logger
from romcloud.services.saves import SaveSyncService

log = get_logger("auto-savesync")
_MENU_PULL_INTERVAL_SECONDS = 300.0
_DEFAULT_STABILITY_CHECKS = 4
_DEFAULT_STAGING_RETRIES = 2


@dataclass(frozen=True)
class GameSession:
    system: str
    emulator: str
    core: str
    rom: str
    started_at: float
    boot_id: str


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
        self._root.mkdir(parents=True, exist_ok=True)
        target = self._path(system, rom)
        temporary = self._root / f".{target.name}.{uuid.uuid4().hex}.tmp"
        payload = json.dumps(session.__dict__, sort_keys=True, separators=(",", ":"))
        temporary.write_text(payload, encoding="utf-8")
        temporary.replace(target)
        return session

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
            session = GameSession(
                system=str(payload["system"]),
                emulator=str(payload.get("emulator", "")),
                core=str(payload.get("core", "")),
                rom=str(payload["rom"]),
                started_at=float(payload["started_at"]),
                boot_id=str(payload["boot_id"]),
            )
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
            return None
        return session if session.boot_id == _boot_id() else None

    def _path(self, system: str, rom: str) -> Path:
        key = hashlib.sha256(f"{system}\0{rom}".encode("utf-8")).hexdigest()
        return self._root / f"{key}.json"


class AutoSaveSyncCoordinator:
    """Coalesce game exits into one bounded, serialized background worker."""

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

    def game_start(self, *, system: str, emulator: str, core: str, rom: str) -> None:
        if not self._enabled:
            return
        self._sessions.start(system=system, emulator=emulator, core=core, rom=rom)

    def game_stop(self, *, system: str, emulator: str, core: str, rom: str) -> None:
        if not self._enabled:
            return
        session = self._sessions.stop(system=system, rom=rom)
        layout_ids = layout_ids_for_session(self._policy, system, emulator, core)
        if not layout_ids:
            return
        changed_since = session.started_at if session is not None else time.time() - 5.0
        self._service.detect_and_mark_local_changes(
            layout_ids, changed_since=changed_since
        )
        self.drain_pending()
        self.menu_tick(force=True)

    def menu_tick(self, *, force: bool = False) -> None:
        if not self._enabled:
            return
        lock = _AutoWorkerLock(self._data_root / ".savesync-auto.lock")
        if not lock.acquire():
            return
        try:
            if self._sessions.has_active_session():
                return
            if not force and not self._menu_pull_due():
                return

            def is_group_active(group_id: str) -> bool:
                layouts = {group.group_id: group.layout_id for group in self._service.get_state().groups}
                return layouts.get(group_id) in self._sessions.active_layout_ids(self._policy)

            def is_layout_active(layout_id: str) -> bool:
                return layout_id in self._sessions.active_layout_ids(self._policy)

            try:
                self._service.quick_sync(
                    is_group_active=is_group_active,
                    is_layout_active=is_layout_active,
                    exclude_layout_ids=self._policy.lifecycle_disabled_layout_ids(),
                )
            except Exception:  # noqa: BLE001 - menu pull is best-effort
                log.warning("Periodic SaveSync quick pull deferred", exc_info=True)
                return
            self._write_menu_pull_timestamp(time.time())
        finally:
            lock.release()

    def menu_loop(self) -> None:
        if not self._menu_loop_enabled():
            return
        loop_lock = _AutoWorkerLock(self._data_root / ".savesync-menu-loop.lock")
        if not loop_lock.acquire():
            return
        try:
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

    def drain_pending(self) -> None:
        if not self._enabled:
            return
        lock = _AutoWorkerLock(self._data_root / ".savesync-auto.lock")
        for attempt in range(6):
            if lock.acquire():
                break
            # A just-finishing leader may have completed its final durable
            # state read while this event was being recorded. A short bounded
            # handoff retry closes that race without provider polling.
            if attempt == 5:
                return
            time.sleep(0.1)
        try:
            # Re-read durable state after every pass so dirty groups recorded
            # while a manual/background operation was running are handled next.
            for _ in range(32):
                state = self._service.get_state()
                group_layouts = {group.group_id: group.layout_id for group in state.groups}
                pending = frozenset(
                    group.group_id
                    for group in state.groups
                    if self._policy.is_lifecycle_enabled(group.layout_id)
                    and (
                        group.condition is SaveGroupCondition.LOCAL_DIRTY
                        or bool(group.dirty_path_hints)
                    )
                )
                active_layouts = self._sessions.active_layout_ids(self._policy)
                pending = frozenset(
                    group_id
                    for group_id in pending
                    if group_layouts.get(group_id) not in active_layouts
                )
                if not pending:
                    return

                def is_active(group_id: str) -> bool:
                    return group_layouts.get(group_id) in self._sessions.active_layout_ids(
                        self._policy
                    )

                before = pending
                try:
                    if not self._wait_until_stable(pending):
                        log.warning(
                            "Auto SaveSync deferred: local save data did not "
                            "stabilize after %d bounded checks; durable dirty "
                            "state retained",
                            self._stability_checks,
                        )
                        return
                    report = None
                    for staging_attempt in range(self._staging_retries + 1):
                        try:
                            report = self._service.reconcile_pending_groups(
                                pending, is_group_active=is_active
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
                            if not self._wait_until_stable(pending):
                                log.warning(
                                    "Auto SaveSync deferred: local save data remained "
                                    "unstable; durable dirty state retained"
                                )
                                return
                except Exception:  # noqa: BLE001 - background work stays quiet
                    log.warning(
                        "Auto SaveSync pass deferred; durable dirty state retained",
                        exc_info=True,
                    )
                    return
                if report is None:
                    return
                after_state = self._service.get_state()
                after = frozenset(
                    group.group_id
                    for group in after_state.groups
                    if self._policy.is_lifecycle_enabled(group.layout_id)
                    and (
                        group.condition is SaveGroupCondition.LOCAL_DIRTY
                        or bool(group.dirty_path_hints)
                    )
                )
                if after == before:
                    return
        finally:
            lock.release()

    def _wait_until_stable(self, group_ids: frozenset[str]) -> bool:
        """Require two equal local hash/size observations within a bound."""
        unavailable = object()
        previous: object = unavailable
        for observation in range(self._stability_checks + 1):
            try:
                current = self._service.observe_local_groups(group_ids)
            except OSError:
                # An emulator may atomically replace a save between discovery
                # and hashing. Treat that bounded observation as unstable.
                previous = unavailable
            else:
                if previous is not unavailable and current == previous:
                    return True
                previous = current
            if observation < self._stability_checks and self._stability_interval:
                time.sleep(self._stability_interval)
        return False


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
