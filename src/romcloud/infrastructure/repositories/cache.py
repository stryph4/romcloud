"""Cache repository — persistence for :class:`~romcloud.core.models.cache.CacheEntry`."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional

from romcloud.core.dependency_resolvers import DESCRIPTOR_EXTENSIONS
from romcloud.core.models.cache import CacheEntry, CacheMember, CacheStatus
from romcloud.core.models.game import GameAsset
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
                INSERT INTO cache_entries
                    (game_id, cache_path, status, cached_at, last_accessed,
                     size_bytes, is_pinned)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(game_id) DO UPDATE SET
                    cache_path = excluded.cache_path,
                    status = excluded.status,
                    cached_at = excluded.cached_at,
                    last_accessed = excluded.last_accessed,
                    size_bytes = excluded.size_bytes,
                    is_pinned = excluded.is_pinned
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
            if entry.status is CacheStatus.COMPLETE:
                self._seed_legacy_membership(conn, entry)

    @staticmethod
    def _seed_legacy_membership(conn, entry: CacheEntry) -> None:  # type: ignore[no-untyped-def]
        if conn.execute(
            "SELECT 1 FROM cache_members WHERE game_id = ? LIMIT 1",
            (entry.game_id,),
        ).fetchone() is not None:
            return
        assets = conn.execute(
            """
            SELECT relative_path, filename, size_bytes, is_primary
            FROM game_assets WHERE game_id = ?
            ORDER BY is_primary DESC, filename
            """,
            (entry.game_id,),
        ).fetchall()
        if not assets:
            conn.execute(
                "UPDATE cache_entries SET status = ? WHERE game_id = ?",
                (CacheStatus.INCOMPLETE.value, entry.game_id),
            )
            return
        for asset in assets:
            size = asset["size_bytes"]
            if size is None and len(assets) == 1:
                size = entry.size_bytes
            conn.execute(
                """
                INSERT INTO cache_members
                    (game_id, relative_path, expected_size, size_bytes, is_primary)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    entry.game_id,
                    asset["relative_path"],
                    asset["size_bytes"],
                    int(size or 0),
                    asset["is_primary"],
                ),
            )
        primary = next((asset for asset in assets if asset["is_primary"]), assets[0])
        resolved = Path(primary["filename"]).suffix.lower() not in DESCRIPTOR_EXTENSIONS
        conn.execute(
            "UPDATE cache_entries SET membership_resolved = ? WHERE game_id = ?",
            (1 if resolved else 0, entry.game_id),
        )
        if not resolved:
            conn.execute(
                "UPDATE cache_entries SET status = ? WHERE game_id = ?",
                (CacheStatus.INCOMPLETE.value, entry.game_id),
            )

    def replace_membership(
        self,
        game_id: str,
        assets: list[GameAsset],
        actual_sizes: dict[str, int],
    ) -> None:
        """Atomically persist the resolved ownership snapshot for one game."""
        with self._db.connect() as conn:
            conn.execute("DELETE FROM cache_members WHERE game_id = ?", (game_id,))
            conn.executemany(
                """
                INSERT INTO cache_members
                    (game_id, relative_path, expected_size, size_bytes, is_primary)
                VALUES (?, ?, ?, ?, ?)
                """,
                [
                    (
                        game_id,
                        asset.relative_path,
                        asset.size_bytes,
                        int(actual_sizes.get(asset.relative_path, 0)),
                        1 if asset.is_primary else 0,
                    )
                    for asset in assets
                ],
            )
            conn.execute(
                "UPDATE cache_entries SET membership_resolved = 1 WHERE game_id = ?",
                (game_id,),
            )

    def update_member_sizes(self, game_id: str, sizes: dict[str, int]) -> None:
        with self._db.connect() as conn:
            conn.executemany(
                """
                UPDATE cache_members SET size_bytes = ?
                WHERE game_id = ? AND relative_path = ?
                """,
                [(size, game_id, path) for path, size in sizes.items()],
            )

    def membership_resolved(self, game_id: str) -> bool:
        with self._db.connect() as conn:
            row = conn.execute(
                "SELECT membership_resolved FROM cache_entries WHERE game_id = ?",
                (game_id,),
            ).fetchone()
            return bool(row[0]) if row is not None else False

    def list_members(self, game_id: str) -> list[CacheMember]:
        with self._db.connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM cache_members WHERE game_id = ?
                ORDER BY is_primary DESC, relative_path
                """,
                (game_id,),
            ).fetchall()
            return [self._row_to_member(row) for row in rows]

    def list_resolved_memberships(self) -> dict[str, list[CacheMember]]:
        """Bulk-load authoritative membership snapshots for cache validation."""
        with self._db.connect() as conn:
            rows = conn.execute(
                """
                SELECT m.* FROM cache_members AS m
                JOIN cache_entries AS e ON e.game_id = m.game_id
                WHERE e.membership_resolved = 1
                ORDER BY m.game_id, m.is_primary DESC, m.relative_path
                """
            ).fetchall()
        memberships: dict[str, list[CacheMember]] = defaultdict(list)
        for row in rows:
            member = self._row_to_member(row)
            memberships[member.game_id].append(member)
        return dict(memberships)

    def owner_count(self, relative_path: str) -> int:
        with self._db.connect() as conn:
            return int(
                conn.execute(
                    "SELECT COUNT(*) FROM cache_members WHERE relative_path = ?",
                    (relative_path,),
                ).fetchone()[0]
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
            members = conn.execute(
                "SELECT relative_path FROM cache_members WHERE game_id = ?",
                (game_id,),
            ).fetchall()
            if len(members) == 1:
                conn.execute(
                    "UPDATE cache_members SET size_bytes = ? WHERE game_id = ?",
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
        """Return non-transferring, unpinned entries in LRU order."""
        with self._db.connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM cache_entries
                WHERE status != ? AND is_pinned = 0
                ORDER BY last_accessed ASC
                """,
                (CacheStatus.TRANSFERRING.value,),
            ).fetchall()
            return [self._row_to_entry(r) for r in rows]

    def total_size(self) -> int:
        """Return physical membership bytes, counting shared paths once."""
        with self._db.connect() as conn:
            result = conn.execute(
                """
                SELECT COALESCE(SUM(size_bytes), 0) FROM (
                    SELECT relative_path, MAX(size_bytes) AS size_bytes
                    FROM cache_members GROUP BY relative_path
                )
                """
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

    @staticmethod
    def _row_to_member(row) -> CacheMember:  # type: ignore[no-untyped-def]
        return CacheMember(
            game_id=row["game_id"],
            relative_path=row["relative_path"],
            expected_size=row["expected_size"],
            size_bytes=row["size_bytes"],
            is_primary=bool(row["is_primary"]),
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
