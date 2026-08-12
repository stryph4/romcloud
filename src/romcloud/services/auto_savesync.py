"""Minimal Batocera game-lifecycle trigger for targeted SaveSync work."""

from __future__ import annotations

import hashlib
import json
import os
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from romcloud.core.models.savesync import SaveGroupCondition
from romcloud.core.save_selection import SaveSelectionPolicy
from romcloud.infrastructure.logging import get_logger
from romcloud.services.saves import SaveSyncService

log = get_logger("auto-savesync")

_DOLPHIN_LAYOUTS = {
    "gamecube": frozenset(
        {"dolphin-gc-memory-card-images", "dolphin-gc-gci-saves"}
    ),
    "wii": frozenset({"dolphin-wii-title-saves"}),
}
_SYSTEM_ALIASES = {
    "ps2": frozenset({"ps2", "pcsx2"}),
    "psp": frozenset({"psp", "ppsspp"}),
    "switch": frozenset({"switch", "yuzu"}),
}


@dataclass(frozen=True)
class GameSession:
    system: str
    emulator: str
    core: str
    rom: str
    started_at: float
    boot_id: str


def layout_ids_for_session(
    policy: SaveSelectionPolicy, system: str, emulator: str = ""
) -> frozenset[str]:
    """Map Batocera lifecycle identity to existing registry layouts only."""
    system_key = system.strip().lower()
    if system_key == "xbox":
        return frozenset()
    if system_key in _DOLPHIN_LAYOUTS:
        candidates = _DOLPHIN_LAYOUTS[system_key]
        return frozenset(
            layout.layout_id for layout in policy.layouts if layout.layout_id in candidates
        )
    systems = set(_SYSTEM_ALIASES.get(system_key, frozenset({system_key})))
    emulator_key = emulator.strip().lower()
    if "duckstation" in emulator_key:
        systems.add("duckstation")
    return frozenset(
        layout.layout_id
        for layout in policy.layouts
        if layout.system in systems and layout.layout_id != "xemu-hdd"
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
        if not self._root.is_dir() or self._root.is_symlink():
            return frozenset()
        result: set[str] = set()
        try:
            entries = tuple(self._root.iterdir())
        except OSError:
            return frozenset()
        for path in entries:
            if path.suffix != ".json" or path.is_symlink() or not path.is_file():
                continue
            session = self._read(path)
            if session is not None:
                result.update(
                    layout_ids_for_session(policy, session.system, session.emulator)
                )
        return frozenset(result)

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
    ) -> None:
        self._service = service
        self._data_root = Path(data_root)
        self._policy = policy or (service.selection_policy if enabled else None)
        self._sessions = ActiveSessionStore(self._data_root)
        self._quiet_seconds = max(0.0, quiet_seconds)
        self._enabled = enabled

    def game_start(self, *, system: str, emulator: str, core: str, rom: str) -> None:
        if not self._enabled:
            return
        self._sessions.start(system=system, emulator=emulator, core=core, rom=rom)

    def game_stop(self, *, system: str, emulator: str, core: str, rom: str) -> None:
        if not self._enabled:
            return
        session = self._sessions.stop(system=system, rom=rom)
        if self._quiet_seconds:
            time.sleep(self._quiet_seconds)
        layout_ids = layout_ids_for_session(self._policy, system, emulator)
        if not layout_ids:
            return
        changed_since = session.started_at if session is not None else time.time() - 5.0
        self._service.detect_and_mark_local_changes(
            layout_ids, changed_since=changed_since
        )
        self.drain_pending()

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
                    if group.layout_id != "xemu-hdd"
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
                    report = self._service.reconcile_pending_groups(
                        pending, is_group_active=is_active
                    )
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
                    if group.layout_id != "xemu-hdd"
                    and (
                        group.condition is SaveGroupCondition.LOCAL_DIRTY
                        or bool(group.dirty_path_hints)
                    )
                )
                if after == before:
                    return
        finally:
            lock.release()


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
