"""SQLite database initialisation and connection factory.

All SQL goes through :class:`Database`.  No ORM.  All columns that hold
timestamps store ISO-8601 UTC strings (``YYYY-MM-DDTHH:MM:SS.ffffff+00:00``).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

_SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS games (
    id               TEXT PRIMARY KEY,
    system           TEXT NOT NULL,
    title            TEXT NOT NULL,
    source_provider  TEXT NOT NULL,
    source_root      TEXT NOT NULL,
    last_played      TEXT,
    added_at         TEXT NOT NULL,
    is_eligible      INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS game_assets (
    id             TEXT PRIMARY KEY,
    game_id        TEXT NOT NULL REFERENCES games(id) ON DELETE CASCADE,
    relative_path  TEXT NOT NULL,
    filename       TEXT NOT NULL,
    size_bytes     INTEGER,
    is_primary     INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_game_assets_game_id ON game_assets(game_id);

CREATE TABLE IF NOT EXISTS cache_entries (
    game_id        TEXT PRIMARY KEY REFERENCES games(id) ON DELETE CASCADE,
    cache_path     TEXT NOT NULL,
    status         TEXT NOT NULL,
    cached_at      TEXT NOT NULL,
    last_accessed  TEXT NOT NULL,
    size_bytes     INTEGER NOT NULL DEFAULT 0,
    is_pinned      INTEGER NOT NULL DEFAULT 0,
    membership_resolved INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS cache_members (
    game_id        TEXT NOT NULL REFERENCES cache_entries(game_id) ON DELETE CASCADE,
    relative_path  TEXT NOT NULL,
    expected_size  INTEGER,
    size_bytes     INTEGER NOT NULL DEFAULT 0,
    is_primary     INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (game_id, relative_path)
);

CREATE INDEX IF NOT EXISTS idx_cache_members_path ON cache_members(relative_path);

CREATE TABLE IF NOT EXISTS proxy_records (
    game_id     TEXT PRIMARY KEY REFERENCES games(id) ON DELETE CASCADE,
    proxy_path  TEXT NOT NULL UNIQUE,
    created_at  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_proxy_records_path ON proxy_records(proxy_path);
"""

_CURRENT_SCHEMA_VERSION = 3


class Database:
    """Thin wrapper around a SQLite connection factory.

    Usage
    -----
    ::

        db = Database("/path/to/catalog.db")
        db.initialize()           # safe to call on every startup
        with db.connect() as conn:
            conn.execute("SELECT ...")
    """

    def __init__(self, db_path: str) -> None:
        self._path = Path(db_path)
        self._path.parent.mkdir(parents=True, exist_ok=True)

    @property
    def path(self) -> Path:
        return self._path

    def connect(self) -> sqlite3.Connection:
        """Return a new SQLite connection with recommended settings."""
        conn = sqlite3.connect(str(self._path), detect_types=sqlite3.PARSE_DECLTYPES)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        return conn

    def initialize(self) -> None:
        """Create tables (idempotent — safe to call on every startup)."""
        with self.connect() as conn:
            conn.executescript(_SCHEMA)
            version_row = conn.execute(
                "SELECT version FROM schema_version LIMIT 1"
            ).fetchone()
            if version_row is None:
                conn.execute(
                    "INSERT INTO schema_version (version) VALUES (?)",
                    (_CURRENT_SCHEMA_VERSION,),
                )
                return

            version = int(version_row["version"])
            if version < 2:
                columns = {
                    row["name"]
                    for row in conn.execute("PRAGMA table_info(games)").fetchall()
                }
                if "is_eligible" not in columns:
                    # Legacy rows remain visible until a successful positive
                    # eligibility scan classifies their primary path.
                    conn.execute(
                        "ALTER TABLE games ADD COLUMN "
                        "is_eligible INTEGER NOT NULL DEFAULT 1"
                    )
                version = 2
            if version < 3:
                columns = {
                    row["name"]
                    for row in conn.execute(
                        "PRAGMA table_info(cache_entries)"
                    ).fetchall()
                }
                if "membership_resolved" not in columns:
                    conn.execute(
                        "ALTER TABLE cache_entries ADD COLUMN "
                        "membership_resolved INTEGER NOT NULL DEFAULT 0"
                    )
                self._migrate_cache_membership(conn)
                version = 3
            conn.execute("UPDATE schema_version SET version = ?", (version,))

    @staticmethod
    def _migrate_cache_membership(conn: sqlite3.Connection) -> None:
        """Snapshot safe legacy ownership without reading the remote source."""
        from romcloud.core.dependency_resolvers import DESCRIPTOR_EXTENSIONS
        from romcloud.core.models.cache import CacheStatus

        entries = conn.execute(
            "SELECT game_id, status, size_bytes FROM cache_entries"
        ).fetchall()
        for entry in entries:
            game_id = entry["game_id"]
            if conn.execute(
                "SELECT 1 FROM cache_members WHERE game_id = ? LIMIT 1",
                (game_id,),
            ).fetchone() is not None:
                continue
            assets = conn.execute(
                """
                SELECT relative_path, filename, size_bytes, is_primary
                FROM game_assets WHERE game_id = ?
                ORDER BY is_primary DESC, filename
                """,
                (game_id,),
            ).fetchall()
            if not assets:
                if entry["status"] == CacheStatus.COMPLETE.value:
                    conn.execute(
                        "UPDATE cache_entries SET status = ? WHERE game_id = ?",
                        (CacheStatus.INCOMPLETE.value, game_id),
                    )
                continue
            for asset in assets:
                actual_size = asset["size_bytes"]
                if actual_size is None and len(assets) == 1:
                    actual_size = entry["size_bytes"]
                conn.execute(
                    """
                    INSERT OR IGNORE INTO cache_members
                        (game_id, relative_path, expected_size, size_bytes, is_primary)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        game_id,
                        asset["relative_path"],
                        asset["size_bytes"],
                        int(actual_size or 0),
                        asset["is_primary"],
                    ),
                )
            primary = next(
                (asset for asset in assets if asset["is_primary"]), assets[0]
            )
            descriptor = (
                Path(primary["filename"]).suffix.lower()
                in DESCRIPTOR_EXTENSIONS
            )
            conn.execute(
                "UPDATE cache_entries SET membership_resolved = ? WHERE game_id = ?",
                (0 if descriptor else 1, game_id),
            )
            if descriptor and entry["status"] == CacheStatus.COMPLETE.value:
                conn.execute(
                    "UPDATE cache_entries SET status = ? WHERE game_id = ?",
                    (CacheStatus.INCOMPLETE.value, game_id),
                )
