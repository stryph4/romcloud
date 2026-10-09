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
CREATE INDEX IF NOT EXISTS idx_cache_entries_library_state
    ON cache_entries(is_pinned, status, game_id);

CREATE TABLE IF NOT EXISTS proxy_records (
    game_id     TEXT PRIMARY KEY REFERENCES games(id) ON DELETE CASCADE,
    proxy_path  TEXT NOT NULL UNIQUE,
    created_at  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_proxy_records_path ON proxy_records(proxy_path);

CREATE TABLE IF NOT EXISTS download_items (
    id                 TEXT PRIMARY KEY,
    batch_id           TEXT,
    game_id            TEXT REFERENCES games(id) ON DELETE SET NULL,
    game_title         TEXT NOT NULL,
    system             TEXT NOT NULL,
    origin             TEXT NOT NULL CHECK (origin IN ('manual', 'pinned', 'selected')),
    state              TEXT NOT NULL CHECK (state IN (
        'queued', 'running', 'paused', 'interrupted', 'verifying',
        'failed', 'cancelled', 'complete'
    )),
    queue_seq          INTEGER NOT NULL,
    worker_instance_id TEXT,
    bytes_total        INTEGER NOT NULL DEFAULT 0 CHECK (bytes_total >= 0),
    bytes_present      INTEGER NOT NULL DEFAULT 0 CHECK (bytes_present >= 0),
    error_code         TEXT,
    error_message      TEXT,
    created_at         TEXT NOT NULL,
    updated_at         TEXT NOT NULL,
    started_at         TEXT,
    finished_at        TEXT
);

CREATE INDEX IF NOT EXISTS idx_download_items_state_queue
    ON download_items(state, queue_seq);
CREATE INDEX IF NOT EXISTS idx_download_items_batch ON download_items(batch_id);
CREATE INDEX IF NOT EXISTS idx_download_items_game ON download_items(game_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_download_items_active_game
    ON download_items(game_id)
    WHERE game_id IS NOT NULL AND state IN (
        'queued', 'running', 'paused', 'interrupted', 'verifying'
    );

CREATE TABLE IF NOT EXISTS cache_staging_assets (
    relative_path         TEXT PRIMARY KEY,
    system                TEXT NOT NULL,
    asset_kind            TEXT NOT NULL CHECK (asset_kind IN ('file', 'directory')),
    source_provider       TEXT NOT NULL,
    source_root           TEXT NOT NULL,
    expected_size         INTEGER CHECK (expected_size IS NULL OR expected_size >= 0),
    source_manifest_sha256 TEXT,
    manifest_version      INTEGER NOT NULL DEFAULT 1,
    created_at            TEXT NOT NULL,
    updated_at            TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS cache_staging_files (
    asset_relative_path  TEXT NOT NULL REFERENCES cache_staging_assets(relative_path) ON DELETE CASCADE,
    member_relative_path TEXT NOT NULL,
    expected_size        INTEGER CHECK (expected_size IS NULL OR expected_size >= 0),
    source_object_id     TEXT,
    source_revision      TEXT,
    source_checksum      TEXT,
    source_modified_epoch REAL,
    state                TEXT NOT NULL CHECK (state IN ('pending', 'transferring', 'partial', 'complete')),
    checkpoint_bytes     INTEGER NOT NULL DEFAULT 0 CHECK (checkpoint_bytes >= 0),
    checkpoint_sha256    TEXT,
    content_sha256       TEXT,
    completed_at         TEXT,
    PRIMARY KEY (asset_relative_path, member_relative_path),
    CHECK (checkpoint_bytes = 0 OR checkpoint_sha256 IS NOT NULL)
);

CREATE INDEX IF NOT EXISTS idx_cache_staging_files_state
    ON cache_staging_files(state);
CREATE UNIQUE INDEX IF NOT EXISTS idx_cache_staging_one_transfer
    ON cache_staging_files(asset_relative_path) WHERE state = 'transferring';

CREATE TABLE IF NOT EXISTS cache_reservations (
    id                 TEXT PRIMARY KEY,
    download_item_id   TEXT REFERENCES download_items(id) ON DELETE SET NULL,
    game_id            TEXT REFERENCES games(id) ON DELETE SET NULL,
    owner_kind         TEXT NOT NULL CHECK (owner_kind IN ('manager', 'launch', 'cli')),
    owner_instance_id  TEXT NOT NULL,
    reserved_bytes     INTEGER NOT NULL CHECK (reserved_bytes >= 0),
    created_at         TEXT NOT NULL,
    updated_at         TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_cache_reservations_game
    ON cache_reservations(game_id);
CREATE INDEX IF NOT EXISTS idx_cache_reservations_download
    ON cache_reservations(download_item_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_cache_reservations_one_download
    ON cache_reservations(download_item_id) WHERE download_item_id IS NOT NULL;
"""

_CURRENT_SCHEMA_VERSION = 4


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
                self._create_query_indexes(conn)
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
            if version < 4:
                # The v4 tables are additive and are created by ``_SCHEMA``
                # before this migration gate.  Advancing the version inside
                # the same transaction preserves every v3 catalog/cache row.
                version = 4
            conn.execute("UPDATE schema_version SET version = ?", (version,))
            self._create_query_indexes(conn)

    @staticmethod
    def _create_query_indexes(conn: sqlite3.Connection) -> None:
        """Create indexes only after legacy tables have gained new columns."""
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_games_library_browse "
            "ON games(is_eligible, system, title, id)"
        )

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
