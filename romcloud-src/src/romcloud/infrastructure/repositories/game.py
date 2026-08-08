"""Game repository — persistence for :class:`~romcloud.core.models.game.Game`."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Optional

from romcloud.core.models.game import Game, GameAsset
from romcloud.infrastructure.database import Database


def _parse_dt(value: Optional[str]) -> Optional[datetime]:
    if value is None:
        return None
    return datetime.fromisoformat(value)


def _fmt_dt(dt: Optional[datetime]) -> Optional[str]:
    if dt is None:
        return None
    return dt.isoformat()


class GameRepository:
    """CRUD operations for :class:`~romcloud.core.models.game.Game`."""

    def __init__(self, db: Database) -> None:
        self._db = db

    # ── write ─────────────────────────────────────────────────────────────────

    def save(self, game: Game) -> None:
        """Insert or replace a game and all its assets."""
        with self._db.connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO games
                    (id, system, title, source_provider, source_root, last_played, added_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    game.id,
                    game.system,
                    game.title,
                    game.source_provider,
                    game.source_root,
                    _fmt_dt(game.last_played),
                    _fmt_dt(game.added_at),
                ),
            )
            # Delete existing assets then re-insert.
            conn.execute("DELETE FROM game_assets WHERE game_id = ?", (game.id,))
            for asset in game.assets:
                conn.execute(
                    """
                    INSERT INTO game_assets
                        (id, game_id, relative_path, filename, size_bytes, is_primary)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        str(uuid.uuid4()),
                        game.id,
                        asset.relative_path,
                        asset.filename,
                        asset.size_bytes,
                        1 if asset.is_primary else 0,
                    ),
                )

    def update_last_played(self, game_id: str, dt: datetime) -> None:
        with self._db.connect() as conn:
            conn.execute(
                "UPDATE games SET last_played = ? WHERE id = ?",
                (_fmt_dt(dt), game_id),
            )

    def delete(self, game_id: str) -> None:
        with self._db.connect() as conn:
            conn.execute("DELETE FROM games WHERE id = ?", (game_id,))

    # ── read ──────────────────────────────────────────────────────────────────

    def get(self, game_id: str) -> Optional[Game]:
        with self._db.connect() as conn:
            row = conn.execute(
                "SELECT * FROM games WHERE id = ?", (game_id,)
            ).fetchone()
            if row is None:
                return None
            return self._row_to_game(conn, row)

    def find_by_system(self, system: str) -> list[Game]:
        with self._db.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM games WHERE system = ? ORDER BY title",
                (system,),
            ).fetchall()
            return [self._row_to_game(conn, r) for r in rows]

    def find_by_source_path(
        self,
        source_provider: str,
        source_root: str,
        relative_path: str,
    ) -> Optional[Game]:
        """Find a game by its primary asset's provider/root/path combination."""
        with self._db.connect() as conn:
            row = conn.execute(
                """
                SELECT g.* FROM games g
                JOIN game_assets a ON a.game_id = g.id
                WHERE g.source_provider = ?
                  AND g.source_root     = ?
                  AND a.relative_path   = ?
                  AND a.is_primary      = 1
                LIMIT 1
                """,
                (source_provider, source_root, relative_path),
            ).fetchone()
            if row is None:
                return None
            return self._row_to_game(conn, row)

    def list_all(self) -> list[Game]:
        with self._db.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM games ORDER BY system, title"
            ).fetchall()
            return [self._row_to_game(conn, r) for r in rows]

    def count(self) -> int:
        with self._db.connect() as conn:
            return conn.execute("SELECT COUNT(*) FROM games").fetchone()[0]

    # ── helpers ───────────────────────────────────────────────────────────────

    def _row_to_game(self, conn, row) -> Game:  # type: ignore[no-untyped-def]
        asset_rows = conn.execute(
            "SELECT * FROM game_assets WHERE game_id = ? ORDER BY is_primary DESC, filename",
            (row["id"],),
        ).fetchall()
        assets = [
            GameAsset(
                filename=a["filename"],
                relative_path=a["relative_path"],
                size_bytes=a["size_bytes"],
                is_primary=bool(a["is_primary"]),
            )
            for a in asset_rows
        ]
        return Game(
            id=row["id"],
            system=row["system"],
            title=row["title"],
            source_provider=row["source_provider"],
            source_root=row["source_root"],
            assets=assets,
            added_at=_parse_dt(row["added_at"]) or datetime.now(timezone.utc),
            last_played=_parse_dt(row["last_played"]),
        )
