"""Cheap, server-side catalog queries for the browser library manager."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from romcloud.core.models.cache import CacheEntry, CacheStatus
from romcloud.infrastructure.database import Database


@dataclass(frozen=True)
class LibraryRow:
    game_id: str
    system: str
    title: str
    filename: str
    source_size_bytes: Optional[int]
    entry: Optional[CacheEntry]
    membership_resolved: bool


@dataclass(frozen=True)
class LibraryPage:
    rows: list[LibraryRow]
    total: int
    page: int
    page_size: int


class LibraryBrowserRepository:
    """Read model that never loads dependency descriptors or full assets."""

    _SORTS = {
        "title": "g.title COLLATE NOCASE ASC, g.id ASC",
        "title_desc": "g.title COLLATE NOCASE DESC, g.id ASC",
        "newest": "g.added_at DESC, g.title COLLATE NOCASE ASC",
        "recent": "g.last_played DESC, g.title COLLATE NOCASE ASC",
        "size": "COALESCE(c.size_bytes, a.size_bytes, 0) DESC, g.title COLLATE NOCASE ASC",
    }

    def __init__(self, db: Database) -> None:
        self._db = db

    def systems(self, *, device_only: bool = False) -> list[dict[str, object]]:
        with self._db.connect() as conn:
            rows = conn.execute(
                f"""
                SELECT g.system,
                       COUNT(*) AS total,
                       SUM(CASE WHEN c.game_id IS NOT NULL THEN 1 ELSE 0 END) AS local
                FROM games AS g
                LEFT JOIN cache_entries AS c ON c.game_id = g.id
                WHERE g.is_eligible = 1
                  {"AND c.game_id IS NOT NULL" if device_only else ""}
                GROUP BY g.system
                ORDER BY g.system COLLATE NOCASE
                """
            ).fetchall()
        return [
            {"system": row["system"], "total": int(row["total"]), "local": int(row["local"] or 0)}
            for row in rows
        ]

    def browse(
        self,
        *,
        system: Optional[str],
        scope: str,
        search: str = "",
        state: str = "all",
        sort: str = "title",
        page: int = 1,
        page_size: int = 50,
    ) -> LibraryPage:
        page = max(1, int(page))
        page_size = max(1, min(200, int(page_size)))
        clauses = ["g.is_eligible = 1"]
        params: list[object] = []
        if system:
            clauses.append("g.system = ?")
            params.append(system)
        if scope == "device":
            clauses.append("c.game_id IS NOT NULL")
        if search.strip():
            clauses.append("g.title LIKE ? ESCAPE '\\' COLLATE NOCASE")
            params.append(f"%{_escape_like(search.strip())}%")
        state_clause = {
            "remote_only": "c.game_id IS NULL",
            "cached": "c.status = 'complete'",
            "pinned": "c.is_pinned = 1",
            "incomplete": "c.game_id IS NOT NULL AND c.status IN ('incomplete', 'failed')",
            "transferring": "c.status = 'transferring'",
        }.get(state)
        if state_clause:
            clauses.append(state_clause)
        where = " AND ".join(clauses)
        order = self._SORTS.get(sort, self._SORTS["title"])
        asset_join = """
            LEFT JOIN game_assets AS a ON a.rowid = (
                SELECT pa.rowid FROM game_assets AS pa
                WHERE pa.game_id = g.id
                ORDER BY pa.is_primary DESC, pa.filename COLLATE NOCASE
                LIMIT 1
            )
        """
        with self._db.connect() as conn:
            total = int(
                conn.execute(
                    f"SELECT COUNT(*) FROM games AS g "
                    f"LEFT JOIN cache_entries AS c ON c.game_id = g.id WHERE {where}",
                    params,
                ).fetchone()[0]
            )
            rows = conn.execute(
                f"""
                SELECT g.id, g.system, g.title,
                       COALESCE(a.filename, '') AS filename,
                       a.size_bytes AS source_size_bytes,
                       c.cache_path, c.status, c.cached_at, c.last_accessed,
                       c.size_bytes AS cache_size_bytes, c.is_pinned,
                       c.membership_resolved
                FROM games AS g
                LEFT JOIN cache_entries AS c ON c.game_id = g.id
                {asset_join}
                WHERE {where}
                ORDER BY {order}
                LIMIT ? OFFSET ?
                """,
                [*params, page_size, (page - 1) * page_size],
            ).fetchall()
        return LibraryPage(
            rows=[self._to_row(row) for row in rows],
            total=total,
            page=page,
            page_size=page_size,
        )

    @staticmethod
    def _to_row(row) -> LibraryRow:  # type: ignore[no-untyped-def]
        entry = None
        if row["status"] is not None:
            entry = CacheEntry(
                game_id=row["id"],
                cache_path=row["cache_path"],
                status=CacheStatus(row["status"]),
                cached_at=datetime.fromisoformat(row["cached_at"]),
                last_accessed=datetime.fromisoformat(row["last_accessed"]),
                size_bytes=int(row["cache_size_bytes"]),
                is_pinned=bool(row["is_pinned"]),
            )
        return LibraryRow(
            game_id=row["id"],
            system=row["system"],
            title=row["title"],
            filename=row["filename"],
            source_size_bytes=row["source_size_bytes"],
            entry=entry,
            membership_resolved=bool(row["membership_resolved"]),
        )


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
