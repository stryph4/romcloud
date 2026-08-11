"""Game repository — persistence for :class:`~romcloud.core.models.game.Game`."""

from __future__ import annotations

import uuid
from collections import defaultdict
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
        """Insert a new game, or update an existing one, plus all its assets.

        Deliberately uses ``INSERT ... ON CONFLICT DO UPDATE`` (a true SQL
        UPDATE on conflict) rather than ``INSERT OR REPLACE``. The latter
        deletes-then-reinserts the conflicting row at the SQLite level,
        which would cascade-delete ``cache_entries``/``proxy_records`` rows
        referencing this ``game_id`` (``ON DELETE CASCADE``) — silently
        wiping pin state and cache/proxy ownership every time an existing
        game's catalog data (e.g. its asset list) is updated in place.
        """
        with self._db.connect() as conn:
            conn.execute(
                """
                INSERT INTO games
                    (id, system, title, source_provider, source_root, last_played, added_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    system          = excluded.system,
                    title           = excluded.title,
                    source_provider = excluded.source_provider,
                    source_root     = excluded.source_root,
                    last_played     = excluded.last_played,
                    added_at        = excluded.added_at
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
            asset_rows = conn.execute(
                "SELECT * FROM game_assets ORDER BY game_id, is_primary DESC, filename"
            ).fetchall()
            assets_by_game: dict[str, list] = defaultdict(list)
            for asset in asset_rows:
                assets_by_game[asset["game_id"]].append(asset)
            return [
                self._game_from_rows(row, assets_by_game[row["id"]])
                for row in rows
            ]

    def list_systems(self) -> list[str]:
        """Return the distinct systems that currently have at least one
        cataloged game — i.e. the systems ROMCloud actually manages."""
        with self._db.connect() as conn:
            rows = conn.execute(
                "SELECT DISTINCT system FROM games ORDER BY system"
            ).fetchall()
            return [r["system"] for r in rows]

    def count(self) -> int:
        with self._db.connect() as conn:
            return conn.execute("SELECT COUNT(*) FROM games").fetchone()[0]

    # ── helpers ───────────────────────────────────────────────────────────────

    def _row_to_game(self, conn, row) -> Game:  # type: ignore[no-untyped-def]
        asset_rows = conn.execute(
            "SELECT * FROM game_assets WHERE game_id = ? ORDER BY is_primary DESC, filename",
            (row["id"],),
        ).fetchall()
        return self._game_from_rows(row, asset_rows)

    @staticmethod
    def _game_from_rows(row, asset_rows) -> Game:  # type: ignore[no-untyped-def]
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
