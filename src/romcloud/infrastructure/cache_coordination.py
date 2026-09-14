"""Cross-process physical cache locking and future-growth reservations."""

from __future__ import annotations

import fcntl
import hashlib
import os
import shutil
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Iterable, Iterator, Optional

from romcloud.core.exceptions import InsufficientSpaceError
from romcloud.core.cache_paths import resolve_cache_path
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
    # Persisting every transfer chunk is excessive on Batocera flash media.
    # Lag is deliberately conservative: the durable row temporarily reserves
    # more future growth than needed, never less.
    _UPDATE_GRANULARITY = 256 * 1024 * 1024

    def __init__(
        self,
        db: Database,
        coordinator: "CacheStorageCoordinator",
        reservation_id: str,
        handle: object,
        reserved_bytes: int,
    ) -> None:
        self._db = db
        self._coordinator = coordinator
        self.id = reservation_id
        self._handle = handle
        self._reserved = reserved_bytes
        self._last_persisted = reserved_bytes
        self._released = False

    @property
    def reserved_bytes(self) -> int:
        return self._reserved

    def consume_growth(self, bytes_written: int) -> None:
        """Convert promised future growth into already-present bytes.

        Call only after the bytes are visible in staging. Until the bounded
        durable update occurs the row over-reserves, which is safe.
        """
        self._reserved = max(0, self._reserved - max(0, int(bytes_written)))
        if (
            self._last_persisted - self._reserved < self._UPDATE_GRANULARITY
            and self._reserved != 0
        ):
            return
        self._persist_reserved()

    def protect_removal(self, bytes_to_remove: int) -> None:
        """Reserve replacement growth before staged bytes disappear."""
        amount = max(0, int(bytes_to_remove))
        if amount == 0:
            return
        self._reserved += amount
        # An increase must be durable before the corresponding filesystem
        # removal; otherwise another admission could observe a quota hole.
        self._persist_reserved()

    def _persist_reserved(self) -> None:
        with self._coordinator.admission_lock():
            with self._db.connect() as conn:
                conn.execute(
                    "UPDATE cache_reservations SET reserved_bytes=?, updated_at=? WHERE id=?",
                    (self._reserved, _utc_now(), self.id),
                )
        self._last_persisted = self._reserved

    def finalize(self, callback: Callable[[], None]) -> None:
        """Atomically swap reservation visibility for final DB accounting."""
        with self._coordinator.admission_lock():
            callback()

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
    def admission_lock(self) -> Iterator[None]:
        path = self.lock_root / "cache-admission.lock"
        with path.open("a+b") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def staging_bytes(self) -> int:
        root = self.cache_root / ".partial"
        staged = (
            sum(
                path.stat().st_size
                for path in root.rglob("*")
                if path.is_file() and not path.is_symlink()
            )
            if root.is_dir()
            else 0
        )
        # A crash can leave validated staging members already promoted out of
        # .partial while their manifest still awaits the final DB transaction.
        # Count those paths conservatively so stale-reservation reclamation
        # cannot make their physical quota ownership disappear.
        return (
            staged
            + self._unfinalized_promotion_bytes()
            + self._replacement_backup_bytes()
        )

    def _replacement_backup_bytes(self) -> int:
        """Count crash remnants from atomic directory replacement."""
        total = 0
        partial_root = self.cache_root / ".partial"
        for path in self.cache_root.rglob("*.romcloud-replaced"):
            try:
                path.relative_to(partial_root)
            except ValueError:
                pass
            else:
                continue
            if path.is_file() and not path.is_symlink():
                total += path.stat().st_size
            elif path.is_dir() and not path.is_symlink():
                total += sum(
                    item.stat().st_size
                    for item in path.rglob("*")
                    if item.is_file() and not item.is_symlink()
                )
        return total

    def _unfinalized_promotion_bytes(self) -> int:
        with self.db.connect() as conn:
            rows = conn.execute(
                "SELECT relative_path, system FROM cache_staging_assets"
            ).fetchall()
        total = 0
        for row in rows:
            path = resolve_cache_path(
                self.cache_root, row["system"], row["relative_path"]
            )
            if path.is_file() and not path.is_symlink():
                total += path.stat().st_size
            elif path.is_dir() and not path.is_symlink():
                total += sum(
                    item.stat().st_size
                    for item in path.rglob("*")
                    if item.is_file() and not item.is_symlink()
                )
        return total

    def snapshot(self) -> dict[str, int]:
        return {
            "final_bytes": self.cache_repo.total_size(),
            "staging_bytes": self.staging_bytes(),
            "reserved_growth": self._active_reserved(),
            "free_bytes": shutil.disk_usage(self.cache_root).free,
        }

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
            with self.admission_lock():
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
        return ReservationLease(self.db, self, reservation_id, handle, requested)

    def fits_current(self, requested: int) -> bool:
        """Evaluate using the caller's already-held admission lock."""
        return self._fits(max(0, int(requested)))

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
