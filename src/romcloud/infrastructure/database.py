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
    is_pinned      INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS proxy_records (
    game_id     TEXT PRIMARY KEY REFERENCES games(id) ON DELETE CASCADE,
    proxy_path  TEXT NOT NULL UNIQUE,
    created_at  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_proxy_records_path ON proxy_records(proxy_path);
"""

_CURRENT_SCHEMA_VERSION = 2


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
            elif int(version_row["version"]) < 2:
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
                conn.execute("UPDATE schema_version SET version = 2")
