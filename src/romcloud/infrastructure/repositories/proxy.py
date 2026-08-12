"""Proxy record repository — tracks .romcloud files ROMCloud created."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from romcloud.core.models.proxy import ProxyRecord
from romcloud.infrastructure.database import Database


def _parse_dt(value: Optional[str]) -> Optional[datetime]:
    if value is None:
        return None
    return datetime.fromisoformat(value)


class ProxyRepository:
    """CRUD for :class:`~romcloud.core.models.proxy.ProxyRecord`.

    Safety contract: only files recorded here are considered ROMCloud-owned.
    Code that removes proxy files MUST check :meth:`owns_path` first.
    """

    def __init__(self, db: Database) -> None:
        self._db = db

    # ── write ─────────────────────────────────────────────────────────────────

    def save(self, record: ProxyRecord) -> None:
        """Insert or update one game's durable proxy ownership.

        A proxy path is unique across games.  This must be a true upsert on
        ``game_id`` rather than SQLite ``INSERT OR REPLACE``: REPLACE resolves
        a conflicting ``proxy_path`` by deleting the *other* game's row before
        inserting this one, silently transferring ownership and keeping the
        registration count flat.  A path-allocation bug must fail without
        destroying either ownership record.
        """
        with self._db.connect() as conn:
            conn.execute(
                """
                INSERT INTO proxy_records (game_id, proxy_path, created_at)
                VALUES (?, ?, ?)
                ON CONFLICT(game_id) DO UPDATE SET
                    proxy_path = excluded.proxy_path,
                    created_at = excluded.created_at
                """,
                (
                    record.game_id,
                    record.proxy_path,
                    record.created_at.isoformat(),
                ),
            )

    def delete(self, game_id: str) -> None:
        with self._db.connect() as conn:
            conn.execute(
                "DELETE FROM proxy_records WHERE game_id = ?", (game_id,)
            )

    # ── read ──────────────────────────────────────────────────────────────────

    def get(self, game_id: str) -> Optional[ProxyRecord]:
        with self._db.connect() as conn:
            row = conn.execute(
                "SELECT * FROM proxy_records WHERE game_id = ?", (game_id,)
            ).fetchone()
            if row is None:
                return None
            return self._row(row)

    def get_by_path(self, proxy_path: str) -> Optional[ProxyRecord]:
        with self._db.connect() as conn:
            row = conn.execute(
                "SELECT * FROM proxy_records WHERE proxy_path = ?", (proxy_path,)
            ).fetchone()
            if row is None:
                return None
            return self._row(row)

    def owns_path(self, path: str) -> bool:
        """Return True if *path* is a proxy file ROMCloud created."""
        return self.get_by_path(path) is not None

    def list_all(self) -> list[ProxyRecord]:
        with self._db.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM proxy_records ORDER BY proxy_path"
            ).fetchall()
            return [self._row(r) for r in rows]

    # ── helpers ───────────────────────────────────────────────────────────────

    def _row(self, row) -> ProxyRecord:  # type: ignore[no-untyped-def]
        return ProxyRecord(
            game_id=row["game_id"],
            proxy_path=row["proxy_path"],
            created_at=_parse_dt(row["created_at"]) or datetime.now(timezone.utc),
        )
