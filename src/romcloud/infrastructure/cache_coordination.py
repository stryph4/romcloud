"""Cross-process physical cache locking and future-growth reservations."""

from __future__ import annotations

import fcntl
import hashlib
import os
import shutil
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Iterable, Iterator, Optional

from romcloud.core.exceptions import InsufficientSpaceError
from romcloud.infrastructure.database import Database
from romcloud.infrastructure.repositories.cache import CacheRepository


class LockUnavailable(RuntimeError):
    pass


class FileLockLease:
    def __init__(self, handles: list[object]) -> None:
        self._handles = handles

    def release(self) -> None:
        while self._handles:
            handle = self._handles.pop()
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)  # type: ignore[attr-defined]
            finally:
                handle.close()  # type: ignore[attr-defined]

    def __enter__(self) -> "FileLockLease":
        return self

    def __exit__(self, *_args: object) -> None:
        self.release()

    def __del__(self) -> None:
        self.release()


class AssetLockManager:
    def __init__(self, lock_root: str | Path) -> None:
        self.root = Path(lock_root) / "assets"
        self.root.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def identity(path: str | Path) -> str:
        canonical = os.path.normcase(os.path.abspath(os.path.realpath(path)))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def acquire(
        self,
        paths: Iterable[str | Path],
        *,
        blocking: bool,
    ) -> FileLockLease:
        identities = sorted({self.identity(path) for path in paths})
        handles: list[object] = []
        flags = fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB)
        try:
            for identity in identities:
                handle = (self.root / f"{identity}.lock").open("a+b")
                try:
                    fcntl.flock(handle.fileno(), flags)
                except BlockingIOError as exc:
                    handle.close()
                    raise LockUnavailable("A required cache asset is already transferring") from exc
                handles.append(handle)
        except Exception:
            FileLockLease(handles).release()
            raise
        return FileLockLease(handles)


class ReservationLease:
    _UPDATE_GRANULARITY = 8 * 1024 * 1024

    def __init__(
        self,
        db: Database,
        reservation_id: str,
        handle: object,
        reserved_bytes: int,
    ) -> None:
        self._db = db
        self.id = reservation_id
        self._handle = handle
        self._reserved = reserved_bytes
        self._last_persisted = reserved_bytes
        self._released = False

    def shrink_to(self, reserved_bytes: int) -> None:
        value = max(0, min(self._reserved, int(reserved_bytes)))
        self._reserved = value
        if self._last_persisted - value < self._UPDATE_GRANULARITY and value != 0:
            return
        with self._db.connect() as conn:
            conn.execute(
                "UPDATE cache_reservations SET reserved_bytes=?, updated_at=? WHERE id=?",
                (value, _utc_now(), self.id),
            )
        self._last_persisted = value

    def release(self) -> None:
        if self._released:
            return
        self._released = True
        with self._db.connect() as conn:
            conn.execute("DELETE FROM cache_reservations WHERE id=?", (self.id,))
        try:
            fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)  # type: ignore[attr-defined]
        finally:
            self._handle.close()  # type: ignore[attr-defined]

    def __enter__(self) -> "ReservationLease":
        return self

    def __exit__(self, *_args: object) -> None:
        self.release()

    def __del__(self) -> None:
        try:
            self.release()
        except Exception:
            pass


class CacheStorageCoordinator:
    """Coordinates short admission decisions without serializing transfers."""

    def __init__(
        self,
        *,
        db: Database,
        cache_repo: CacheRepository,
        cache_root: str | Path,
        lock_root: str | Path,
        max_size_bytes: int,
        min_free_bytes: int,
    ) -> None:
        self.db = db
        self.cache_repo = cache_repo
        self.cache_root = Path(cache_root)
        self.lock_root = Path(lock_root)
        self.lock_root.mkdir(parents=True, exist_ok=True)
        self.assets = AssetLockManager(self.lock_root)
        self.max_size_bytes = max_size_bytes
        self.min_free_bytes = min_free_bytes

    @contextmanager
    def _admission_lock(self) -> Iterator[None]:
        path = self.lock_root / "cache-admission.lock"
        with path.open("a+b") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def staging_bytes(self) -> int:
        root = self.cache_root / ".partial"
        if not root.is_dir():
            return 0
        return sum(
            path.stat().st_size
            for path in root.rglob("*")
            if path.is_file() and not path.is_symlink()
        )

    def admit(
        self,
        *,
        requested_growth: int,
        game_id: Optional[str],
        owner_kind: str,
        owner_instance_id: str,
        download_item_id: Optional[str] = None,
        evict: Optional[Callable[[int], object]] = None,
    ) -> ReservationLease:
        requested = max(0, int(requested_growth))
        reservation_id = uuid.uuid4().hex
        reservation_dir = self.lock_root / "reservations"
        reservation_dir.mkdir(parents=True, exist_ok=True)
        handle = (reservation_dir / f"{reservation_id}.lock").open("a+b")
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            with self._admission_lock():
                self._reclaim_stale_locked()
                if not self._fits(requested) and evict is not None:
                    evict(requested)
                if not self._fits(requested):
                    final = self.cache_repo.total_size()
                    staging = self.staging_bytes()
                    active = self._active_reserved()
                    free = shutil.disk_usage(self.cache_root).free
                    raise InsufficientSpaceError(
                        "Not enough storage for download future growth: "
                        f"need {requested / 1024**3:.1f} GB; final cache "
                        f"{final / 1024**3:.1f} GB, retained partials "
                        f"{staging / 1024**3:.1f} GB, active reservations "
                        f"{active / 1024**3:.1f} GB, filesystem free "
                        f"{free / 1024**3:.1f} GB"
                    )
                now = _utc_now()
                with self.db.connect() as conn:
                    conn.execute(
                        """
                        INSERT INTO cache_reservations
                            (id,download_item_id,game_id,owner_kind,owner_instance_id,
                             reserved_bytes,created_at,updated_at)
                        VALUES (?,?,?,?,?,?,?,?)
                        """,
                        (reservation_id, download_item_id, game_id, owner_kind,
                         owner_instance_id, requested, now, now),
                    )
        except Exception:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            handle.close()
            raise
        return ReservationLease(self.db, reservation_id, handle, requested)

    def _fits(self, requested: int) -> bool:
        final = self.cache_repo.total_size()
        staging = self.staging_bytes()
        reserved = self._active_reserved()
        free = shutil.disk_usage(self.cache_root).free
        return (
            final + staging + reserved + requested <= self.max_size_bytes
            and free - reserved - requested >= self.min_free_bytes
        )

    def _active_reserved(self) -> int:
        with self.db.connect() as conn:
            return int(
                conn.execute(
                    "SELECT COALESCE(SUM(reserved_bytes),0) FROM cache_reservations"
                ).fetchone()[0]
            )

    def _reclaim_stale_locked(self) -> int:
        with self.db.connect() as conn:
            rows = conn.execute("SELECT id FROM cache_reservations").fetchall()
        reclaimed: list[str] = []
        for row in rows:
            path = self.lock_root / "reservations" / f"{row['id']}.lock"
            handle = path.open("a+b")
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                handle.close()
                continue
            reclaimed.append(row["id"])
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            handle.close()
        if reclaimed:
            with self.db.connect() as conn:
                conn.executemany(
                    "DELETE FROM cache_reservations WHERE id=?",
                    [(item,) for item in reclaimed],
                )
        return len(reclaimed)


def _utc_now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()
