"""Cache repository — persistence for :class:`~romcloud.core.models.cache.CacheEntry`."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional

from romcloud.core.models.cache import CacheEntry, CacheStatus
from romcloud.infrastructure.database import Database


# Exact historical default shipped before ROMCloud's runtime paths were grouped
# below /userdata/romcloud.  Do not add guessed locations here: reconciliation
# is intentionally limited to roots known to have been persisted by ROMCloud.
LEGACY_CACHE_ROOTS: tuple[Path, ...] = (Path("/userdata/romcloud-cache"),)


@dataclass(frozen=True)
class CachePathReconciliation:
    migrated: int = 0
    missing: int = 0


def _parse_dt(value: Optional[str]) -> Optional[datetime]:
    if value is None:
        return None
    return datetime.fromisoformat(value)


def _fmt_dt(dt: datetime) -> str:
    return dt.isoformat()


class CacheRepository:
    """CRUD operations for :class:`~romcloud.core.models.cache.CacheEntry`."""

    def __init__(self, db: Database) -> None:
        self._db = db

    # ── write ─────────────────────────────────────────────────────────────────

    def save(self, entry: CacheEntry) -> None:
        with self._db.connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO cache_entries
                    (game_id, cache_path, status, cached_at, last_accessed,
                     size_bytes, is_pinned)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    entry.game_id,
                    entry.cache_path,
                    entry.status.value,
                    _fmt_dt(entry.cached_at),
                    _fmt_dt(entry.last_accessed),
                    entry.size_bytes,
                    1 if entry.is_pinned else 0,
                ),
            )

    def update_status(self, game_id: str, status: CacheStatus) -> None:
        with self._db.connect() as conn:
            conn.execute(
                "UPDATE cache_entries SET status = ? WHERE game_id = ?",
                (status.value, game_id),
            )

    def update_cache_path(self, game_id: str, cache_path: str) -> None:
        with self._db.connect() as conn:
            conn.execute(
                "UPDATE cache_entries SET cache_path = ? WHERE game_id = ?",
                (cache_path, game_id),
            )

    def reconcile_legacy_cache_paths(
        self,
        cache_root: Path | str,
        *,
        legacy_roots: Iterable[Path | str] = LEGACY_CACHE_ROOTS,
    ) -> CachePathReconciliation:
        """Rebase verified legacy absolute paths into *cache_root*.

        Only ``cache_path`` is updated, and only when the corresponding path
        already exists below the configured cache root. Cache bytes and all
        other persisted entry state are deliberately untouched.
        """
        configured_root = Path(cache_root)
        roots = tuple(Path(root) for root in legacy_roots)
        migrated = 0
        missing = 0

        with self._db.connect() as conn:
            rows = conn.execute(
                "SELECT game_id, cache_path FROM cache_entries"
            ).fetchall()
            updates: list[tuple[str, str, str]] = []
            for row in rows:
                recorded = Path(row["cache_path"])
                if _is_within(recorded, configured_root):
                    continue

                relative = _relative_to_legacy_root(recorded, roots)
                if relative is None:
                    continue
                rebased = configured_root / relative
                if not rebased.exists():
                    missing += 1
                    continue
                updates.append((str(rebased), row["game_id"], row["cache_path"]))

            if updates:
                before = conn.total_changes
                conn.executemany(
                    """
                    UPDATE cache_entries SET cache_path = ?
                    WHERE game_id = ? AND cache_path = ?
                    """,
                    updates,
                )
                migrated = conn.total_changes - before

        return CachePathReconciliation(migrated=migrated, missing=missing)

    def update_size(self, game_id: str, size_bytes: int) -> None:
        with self._db.connect() as conn:
            conn.execute(
                "UPDATE cache_entries SET size_bytes = ? WHERE game_id = ?",
                (size_bytes, game_id),
            )

    def update_last_accessed(self, game_id: str, dt: datetime) -> None:
        with self._db.connect() as conn:
            conn.execute(
                "UPDATE cache_entries SET last_accessed = ? WHERE game_id = ?",
                (_fmt_dt(dt), game_id),
            )

    def set_pinned(self, game_id: str, pinned: bool) -> None:
        with self._db.connect() as conn:
            conn.execute(
                "UPDATE cache_entries SET is_pinned = ? WHERE game_id = ?",
                (1 if pinned else 0, game_id),
            )

    def delete(self, game_id: str) -> None:
        with self._db.connect() as conn:
            conn.execute(
                "DELETE FROM cache_entries WHERE game_id = ?", (game_id,)
            )

    # ── read ──────────────────────────────────────────────────────────────────

    def get(self, game_id: str) -> Optional[CacheEntry]:
        with self._db.connect() as conn:
            row = conn.execute(
                "SELECT * FROM cache_entries WHERE game_id = ?", (game_id,)
            ).fetchone()
            if row is None:
                return None
            return self._row_to_entry(row)

    def list_all(self) -> list[CacheEntry]:
        with self._db.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM cache_entries ORDER BY last_accessed DESC"
            ).fetchall()
            return [self._row_to_entry(r) for r in rows]

    def list_complete(self) -> list[CacheEntry]:
        with self._db.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM cache_entries WHERE status = ? ORDER BY last_accessed DESC",
                (CacheStatus.COMPLETE.value,),
            ).fetchall()
            return [self._row_to_entry(r) for r in rows]

    def list_evictable_lru(self) -> list[CacheEntry]:
        """Return complete, unpinned entries ordered oldest-accessed first."""
        with self._db.connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM cache_entries
                WHERE status = ? AND is_pinned = 0
                ORDER BY last_accessed ASC
                """,
                (CacheStatus.COMPLETE.value,),
            ).fetchall()
            return [self._row_to_entry(r) for r in rows]

    def total_size(self) -> int:
        """Return the sum of size_bytes for all complete cache entries."""
        with self._db.connect() as conn:
            result = conn.execute(
                "SELECT COALESCE(SUM(size_bytes), 0) FROM cache_entries WHERE status = ?",
                (CacheStatus.COMPLETE.value,),
            ).fetchone()
            return int(result[0])

    def count(self) -> int:
        with self._db.connect() as conn:
            return conn.execute("SELECT COUNT(*) FROM cache_entries").fetchone()[0]

    # ── helpers ───────────────────────────────────────────────────────────────

    def _row_to_entry(self, row) -> CacheEntry:  # type: ignore[no-untyped-def]
        return CacheEntry(
            game_id=row["game_id"],
            cache_path=row["cache_path"],
            status=CacheStatus(row["status"]),
            cached_at=_parse_dt(row["cached_at"]) or datetime.now(timezone.utc),
            last_accessed=_parse_dt(row["last_accessed"]) or datetime.now(timezone.utc),
            size_bytes=row["size_bytes"],
            is_pinned=bool(row["is_pinned"]),
        )


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _relative_to_legacy_root(
    path: Path, legacy_roots: tuple[Path, ...]
) -> Optional[Path]:
    for root in legacy_roots:
        try:
            relative = path.relative_to(root)
        except ValueError:
            continue
        if relative.parts and ".." not in relative.parts:
            return relative
    return None
