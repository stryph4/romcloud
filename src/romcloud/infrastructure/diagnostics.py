"""Central, redacted, fail-open structured diagnostics for ROMCloud.

The database is deliberately independent from the catalog database: catalog
migrations and failures must not affect support logging (or vice versa).  Every
process uses SQLite WAL and short transactions so CLI hooks, the GUI backend,
and workers can write concurrently.
"""

from __future__ import annotations

import contextlib
import contextvars
import json
import logging
import os
import re
import sqlite3
import sys
import threading
import time
import uuid
from functools import wraps
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator, Mapping, Optional

from romcloud import __version__

SCHEMA_VERSION = 2
DEFAULT_FILENAME = "diagnostics.db"
RETENTION_MAX_AGE_DAYS = 30
RETENTION_MAX_EVENTS = 100_000
RETENTION_MAX_BYTES = 32 * 1024 * 1024
RETENTION_PRUNE_BATCH = 1_000
RETENTION_CHECK_INTERVAL = 256
BUSY_TIMEOUT_MS = 250
MAX_MESSAGE_LENGTH = 8_192
MAX_METADATA_LENGTH = 32_768

_SCHEMA = """
PRAGMA auto_vacuum = INCREMENTAL;
PRAGMA journal_mode = WAL;
PRAGMA synchronous = NORMAL;
CREATE TABLE IF NOT EXISTS diagnostic_schema_version (
    version INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS diagnostic_events (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp_utc       TEXT NOT NULL,
    monotonic_ns        INTEGER NOT NULL,
    level               TEXT NOT NULL,
    subsystem           TEXT NOT NULL,
    event_code          TEXT NOT NULL,
    message             TEXT NOT NULL,
    operation_id        TEXT,
    parent_operation_id TEXT,
    process_id          INTEGER NOT NULL,
    thread_id           INTEGER NOT NULL,
    thread_name         TEXT NOT NULL,
    process_name        TEXT NOT NULL,
    romcloud_version    TEXT NOT NULL,
    build               TEXT,
    metadata_json       TEXT NOT NULL,
    exception_type      TEXT,
    exception_message   TEXT
);
CREATE TABLE IF NOT EXISTS diagnostic_operations (
    operation_id        TEXT PRIMARY KEY,
    parent_operation_id TEXT,
    subsystem           TEXT NOT NULL,
    started_at_utc      TEXT NOT NULL,
    completed_at_utc    TEXT,
    name                TEXT NOT NULL,
    source              TEXT,
    status              TEXT NOT NULL DEFAULT 'running',
    duration_ms         INTEGER,
    effective_mode      TEXT,
    provider_id         TEXT,
    provider_type       TEXT,
    generation_before   INTEGER,
    generation_after    INTEGER,
    examined            INTEGER NOT NULL DEFAULT 0,
    uploaded            INTEGER NOT NULL DEFAULT 0,
    downloaded          INTEGER NOT NULL DEFAULT 0,
    conflicts           INTEGER NOT NULL DEFAULT 0,
    unchanged           INTEGER NOT NULL DEFAULT 0,
    repairs             INTEGER NOT NULL DEFAULT 0,
    event_count         INTEGER NOT NULL DEFAULT 0,
    search_text         TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_diagnostic_events_newest
    ON diagnostic_events(timestamp_utc DESC, id DESC);
CREATE INDEX IF NOT EXISTS idx_diagnostic_events_operation
    ON diagnostic_events(operation_id, id);
CREATE INDEX IF NOT EXISTS idx_diagnostic_events_operation_level
    ON diagnostic_events(operation_id, level);
CREATE INDEX IF NOT EXISTS idx_diagnostic_events_subsystem
    ON diagnostic_events(subsystem, timestamp_utc DESC);
CREATE INDEX IF NOT EXISTS idx_diagnostic_events_level
    ON diagnostic_events(level, timestamp_utc DESC);
CREATE INDEX IF NOT EXISTS idx_diagnostic_operations_newest
    ON diagnostic_operations(started_at_utc DESC, operation_id);
CREATE INDEX IF NOT EXISTS idx_diagnostic_operations_subsystem
    ON diagnostic_operations(subsystem, started_at_utc DESC);
CREATE INDEX IF NOT EXISTS idx_diagnostic_operations_status
    ON diagnostic_operations(status, started_at_utc DESC);
"""

# Metadata is intentionally closed by default. Add names here when a subsystem
# needs a new forensic field; secret-looking names are still always redacted.
SAFE_METADATA_KEYS = frozenset(
    {
        "action", "affected_groups", "artifact_count", "baseline_artifacts",
        "baseline_hash", "bytes", "candidate_groups", "changed_groups",
        "classification", "conflicts", "cursor_after", "cursor_before",
        "decision", "decision_source", "destination_views", "difference",
        "direction", "dirty_paths", "downloaded", "effective_mode", "entrypoint",
        "existing", "expected", "generation", "generation_after",
        "generation_before", "group_id", "journal_entries", "layout_id",
        "layouts", "local_artifacts", "local_hash", "missing", "mode",
        "mutation_count", "operation_name", "path", "paths", "pending_groups",
        "physical_path", "process", "processed_entries", "processed_groups",
        "provider_id", "provider_type", "reason", "remote_artifacts",
        "remote_hash", "repairs", "result", "revision", "root", "scope",
        "selected_side", "source", "stage", "status", "transaction_id",
        "transaction_root", "transaction_view", "trigger", "unchanged",
        "uploaded", "worker_state", "quick_ready", "duration_ms", "count",
        "examined",
        "current_hash", "desired_hash", "previous_hash", "size_bytes", "detail",
        "event",
        "arguments", "browser_pid", "browser_view", "display", "exit_code",
        "fallback_attempted", "home", "launch_strategy", "runtime_source",
        "process_ownership", "profile_directory", "runtime_type", "sandbox_enabled",
        "server_ready", "url",
        "wayland_display", "working_directory", "xauthority", "xdg_runtime_dir",
    }
)
_SECRET_KEY = re.compile(
    r"(?:pass(?:word|phrase)?|secret|token|api[_-]?key|authorization|auth[_-]?header|"
    r"credential|private[_-]?key|client[_-]?secret|session[_-]?key|cookie)", re.I
)
_SECRET_VALUE = re.compile(
    r"(?:bearer\s+[A-Za-z0-9._~+/=-]+|-----BEGIN [A-Z ]*PRIVATE KEY-----|"
    r"(?:access|refresh)[_-]?token\s*[:=]\s*[^\s,;]+)", re.I
)
_SECRET_ASSIGNMENT = re.compile(
    r"(?i)\b(password|passphrase|secret|access[_-]?token|refresh[_-]?token|"
    r"api[_-]?key|authorization|cookie)\b(\s*[:=]\s*)([^\s,;]+)"
)
_URL_USERINFO = re.compile(r"([a-z][a-z0-9+.-]*://[^/@:\s]+:)[^/@\s]+@", re.I)

_operation_id: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "romcloud_diagnostic_operation_id", default=None
)
_parent_operation_id: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "romcloud_diagnostic_parent_operation_id", default=None
)
_active_store: Optional["DiagnosticStore"] = None
_active_store_lock = threading.RLock()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _clean_text(value: object, *, limit: int = MAX_MESSAGE_LENGTH) -> str:
    text = str(value)
    if "PRIVATE KEY-----" in text.upper():
        return "[REDACTED]"
    text = _URL_USERINFO.sub(r"\1[REDACTED]@", text)
    if _SECRET_VALUE.search(text):
        text = _SECRET_VALUE.sub("[REDACTED]", text)
    text = _SECRET_ASSIGNMENT.sub(r"\1\2[REDACTED]", text)
    return text[:limit]


def redact_metadata(metadata: Optional[Mapping[str, Any]]) -> dict[str, Any]:
    """Return JSON-safe, allowlisted metadata with defense-in-depth redaction."""
    if not metadata:
        return {}

    def clean(key: str, value: Any, depth: int = 0) -> Any:
        if _SECRET_KEY.search(key):
            return "[REDACTED]"
        if key not in SAFE_METADATA_KEYS and depth == 0:
            return "[REDACTED:UNAPPROVED_FIELD]"
        if depth > 4:
            return "[TRUNCATED]"
        if value is None or isinstance(value, (bool, int, float)):
            return value
        if isinstance(value, str) or isinstance(value, Path):
            return _clean_text(value, limit=2_048)
        if isinstance(value, Mapping):
            return {
                str(child_key)[:128]: clean(str(child_key), child_value, depth + 1)
                for child_key, child_value in list(value.items())[:100]
            }
        if isinstance(value, (list, tuple, set, frozenset)):
            return [clean(key, item, depth + 1) for item in list(value)[:100]]
        return _clean_text(value, limit=2_048)

    return {str(key)[:128]: clean(str(key), value) for key, value in metadata.items()}


def _build_identifier() -> Optional[str]:
    try:
        candidate = Path(__file__).resolve().parents[3] / "version.json"
        payload = json.loads(candidate.read_text(encoding="utf-8"))
        bits = [str(payload.get(name)) for name in ("build_date", "commit") if payload.get(name)]
        return "/".join(bits) or None
    except (OSError, ValueError, TypeError):
        return None


@dataclass(frozen=True)
class DiagnosticQuery:
    subsystem: Optional[str] = None
    level: Optional[str] = None
    operation_id: Optional[str] = None
    start_utc: Optional[str] = None
    end_utc: Optional[str] = None
    text: Optional[str] = None
    page: int = 1
    page_size: int = 50
    chronological: bool = False


class DiagnosticStore:
    """Concurrent SQLite event store. Writes are best effort by design."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._connection: Optional[sqlite3.Connection] = None
        self._connection_pid: Optional[int] = None
        self._lock = threading.RLock()
        self._writes = 0
        self._disabled = False

    @property
    def available(self) -> bool:
        """Whether this initialized store remains available to local consumers."""
        return not self._disabled and self._connection is not None

    def _connect(self) -> sqlite3.Connection:
        pid = os.getpid()
        if self._connection is not None and self._connection_pid == pid:
            return self._connection
        if self._connection is not None:
            with contextlib.suppress(Exception):
                self._connection.close()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(
            str(self.path), timeout=BUSY_TIMEOUT_MS / 1000, check_same_thread=False
        )
        conn.row_factory = sqlite3.Row
        conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
        self._connection = conn
        self._connection_pid = pid
        return conn

    def initialize(self) -> bool:
        try:
            with self._lock:
                conn = self._connect()
                conn.executescript(_SCHEMA)
                row = conn.execute(
                    "SELECT version FROM diagnostic_schema_version LIMIT 1"
                ).fetchone()
                if row is None:
                    self._backfill_operation_summaries(conn)
                    conn.execute(
                        "INSERT INTO diagnostic_schema_version(version) VALUES (?)",
                        (SCHEMA_VERSION,),
                    )
                elif int(row["version"]) > SCHEMA_VERSION:
                    raise sqlite3.DatabaseError("unsupported diagnostics schema")
                elif int(row["version"]) < SCHEMA_VERSION:
                    self._backfill_operation_summaries(conn)
                    conn.execute(
                        "UPDATE diagnostic_schema_version SET version = ?",
                        (SCHEMA_VERSION,),
                    )
                conn.commit()
            return True
        except Exception:
            self._disabled = True
            self._fallback("ROMCloud diagnostics database could not be initialized")
            return False

    def _backfill_operation_summaries(self, conn: sqlite3.Connection) -> None:
        """One-time v1→v2 migration; normal page queries never scan event history."""
        conn.execute("DELETE FROM diagnostic_operations")
        rows = conn.execute(
            """SELECT timestamp_utc, subsystem, event_code, message, metadata_json,
                operation_id, parent_operation_id
            FROM diagnostic_events WHERE operation_id IS NOT NULL ORDER BY id"""
        )
        for row in rows:
            try:
                metadata = json.loads(row["metadata_json"])
            except (TypeError, ValueError):
                metadata = {}
            self._update_operation_summary(
                conn,
                timestamp=str(row["timestamp_utc"]),
                subsystem=str(row["subsystem"]),
                event_code=str(row["event_code"]),
                message=str(row["message"]),
                metadata=metadata if isinstance(metadata, dict) else {},
                operation_id=str(row["operation_id"]),
                parent_operation_id=row["parent_operation_id"],
            )

    def close(self) -> None:
        with self._lock:
            if self._connection is not None:
                with contextlib.suppress(Exception):
                    self._connection.close()
            self._connection = None
            self._connection_pid = None
            self._disabled = True

    def write(
        self, *, level: str, subsystem: str, event_code: str, message: str,
        metadata: Optional[Mapping[str, Any]] = None,
        operation_id: Optional[str] = None,
        parent_operation_id: Optional[str] = None,
        exception_type: Optional[str] = None,
        exception_message: Optional[str] = None,
    ) -> bool:
        if self._disabled:
            return False
        safe_metadata = redact_metadata(metadata)
        encoded = json.dumps(safe_metadata, sort_keys=True, separators=(",", ":"))
        if len(encoded) > MAX_METADATA_LENGTH:
            encoded = json.dumps({"status": "metadata-truncated"})
        try:
            with self._lock:
                conn = self._connect()
                timestamp = _utc_now()
                conn.execute(
                    """INSERT INTO diagnostic_events (
                       timestamp_utc, monotonic_ns, level, subsystem, event_code,
                       message, operation_id, parent_operation_id, process_id,
                       thread_id, thread_name, process_name, romcloud_version,
                       build, metadata_json, exception_type, exception_message
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        timestamp, time.monotonic_ns(), level.upper()[:16],
                        subsystem[:128], event_code[:128], _clean_text(message),
                        operation_id or _operation_id.get(),
                        parent_operation_id or _parent_operation_id.get(), os.getpid(),
                        threading.get_ident(), threading.current_thread().name[:128],
                        Path(sys.argv[0]).name[:128], __version__, _build_identifier(),
                        encoded, exception_type[:256] if exception_type else None,
                        _clean_text(exception_message, limit=2_048)
                        if exception_message else None,
                    ),
                )
                if operation_id or _operation_id.get():
                    self._update_operation_summary(
                        conn,
                        timestamp=timestamp,
                        subsystem=subsystem,
                        event_code=event_code,
                        message=_clean_text(message),
                        metadata=safe_metadata,
                        operation_id=operation_id or _operation_id.get() or "",
                        parent_operation_id=(
                            parent_operation_id or _parent_operation_id.get()
                        ),
                    )
                conn.commit()
                self._writes += 1
                if self._writes % RETENTION_CHECK_INTERVAL == 0:
                    self.prune()
            return True
        except Exception:
            self._fallback("ROMCloud structured diagnostic write failed")
            return False

    @staticmethod
    def _update_operation_summary(
        conn: sqlite3.Connection,
        *,
        timestamp: str,
        subsystem: str,
        event_code: str,
        message: str,
        metadata: Mapping[str, Any],
        operation_id: str,
        parent_operation_id: Optional[str],
    ) -> None:
        name = str(metadata.get("operation_name") or event_code)[:256]
        source = metadata.get("source")
        search_piece = " ".join(
            (operation_id, message, json.dumps(metadata, sort_keys=True, separators=(",", ":")))
        )[:32768]
        conn.execute(
            """INSERT OR IGNORE INTO diagnostic_operations (
                operation_id, parent_operation_id, subsystem, started_at_utc,
                name, source, effective_mode, provider_id, provider_type, search_text
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                operation_id, parent_operation_id, subsystem, timestamp, name,
                str(source)[:256] if source is not None else None,
                metadata.get("effective_mode"), metadata.get("provider_id"),
                metadata.get("provider_type"), "",
            ),
        )
        conn.execute(
            """UPDATE diagnostic_operations SET
                event_count = event_count + 1,
                search_text = substr(search_text || ' ' || ?, -65536)
            WHERE operation_id = ?""",
            (search_piece, operation_id),
        )
        if event_code == "operation.started":
            conn.execute(
                """UPDATE diagnostic_operations SET
                    parent_operation_id = ?, subsystem = ?, started_at_utc = ?,
                    name = ?, source = ?, status = 'running', effective_mode = ?,
                    provider_id = ?, provider_type = ? WHERE operation_id = ?""",
                (
                    parent_operation_id, subsystem, timestamp, name,
                    str(source)[:256] if source is not None else None,
                    metadata.get("effective_mode"), metadata.get("provider_id"),
                    metadata.get("provider_type"), operation_id,
                ),
            )
        elif event_code == "reconciliation.decision":
            decision = str(metadata.get("decision") or "")
            repair = str(metadata.get("reason") or "") == "incomplete-local-materialization"
            column = {
                "upload": "uploaded", "download": "downloaded",
                "conflict": "conflicts", "unchanged": "unchanged",
            }.get(decision)
            conn.execute(
                "UPDATE diagnostic_operations SET examined = examined + 1, repairs = repairs + ? WHERE operation_id = ?",
                (1 if repair else 0, operation_id),
            )
            if column:
                conn.execute(
                    f"UPDATE diagnostic_operations SET {column} = {column} + 1 WHERE operation_id = ?",
                    (operation_id,),
                )
        elif event_code == "journal.committed":
            conn.execute(
                "UPDATE diagnostic_operations SET generation_after = ? WHERE operation_id = ?",
                (metadata.get("generation"), operation_id),
            )
        elif event_code == "cursor.advanced":
            conn.execute(
                """UPDATE diagnostic_operations SET
                    generation_before = COALESCE(generation_before, ?),
                    generation_after = COALESCE(?, generation_after)
                WHERE operation_id = ?""",
                (metadata.get("cursor_before"), metadata.get("cursor_after"), operation_id),
            )
        elif event_code == "operation.result":
            conn.execute(
                """UPDATE diagnostic_operations SET status = ?,
                    generation_before = COALESCE(generation_before, ?),
                    generation_after = COALESCE(?, ?, generation_after),
                    examined = CASE WHEN examined = 0 THEN COALESCE(?, examined) ELSE examined END,
                    uploaded = COALESCE(?, uploaded), downloaded = COALESCE(?, downloaded),
                    conflicts = COALESCE(?, conflicts), unchanged = COALESCE(?, unchanged)
                WHERE operation_id = ?""",
                (
                    metadata.get("status") or "success", metadata.get("cursor_before"),
                    metadata.get("cursor_after"), metadata.get("generation"),
                    metadata.get("examined"),
                    metadata.get("uploaded"), metadata.get("downloaded"),
                    metadata.get("conflicts"), metadata.get("unchanged"), operation_id,
                ),
            )
        elif event_code == "operation.failed":
            conn.execute(
                """UPDATE diagnostic_operations SET completed_at_utc = ?,
                    status = 'failed', duration_ms = COALESCE(?, duration_ms)
                WHERE operation_id = ?""",
                (timestamp, metadata.get("duration_ms"), operation_id),
            )
        elif event_code == "operation.completed":
            conn.execute(
                """UPDATE diagnostic_operations SET completed_at_utc = ?,
                    status = CASE WHEN status = 'running' THEN 'success' ELSE status END,
                    duration_ms = COALESCE(?, duration_ms)
                WHERE operation_id = ?""",
                (timestamp, metadata.get("duration_ms"), operation_id),
            )

    def query(self, query: DiagnosticQuery = DiagnosticQuery()) -> list[dict[str, Any]]:
        page_size = max(1, min(int(query.page_size), 200))
        page = max(1, int(query.page))
        clauses: list[str] = []
        args: list[object] = []
        for column, value in (
            ("subsystem", query.subsystem), ("level", query.level and query.level.upper()),
            ("operation_id", query.operation_id),
        ):
            if value:
                clauses.append(f"{column} = ?")
                args.append(value)
        if query.start_utc:
            clauses.append("timestamp_utc >= ?")
            args.append(query.start_utc)
        if query.end_utc:
            clauses.append("timestamp_utc <= ?")
            args.append(query.end_utc)
        if query.text:
            clauses.append(
                "(message LIKE ? ESCAPE '\\' OR metadata_json LIKE ? ESCAPE '\\' "
                "OR operation_id LIKE ? ESCAPE '\\' OR event_code LIKE ? ESCAPE '\\' "
                "OR subsystem LIKE ? ESCAPE '\\')"
            )
            escaped = query.text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            pattern = f"%{escaped}%"
            args.extend((pattern, pattern, pattern, pattern, pattern))
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        order = "ASC" if query.chronological else "DESC"
        sql = f"SELECT * FROM diagnostic_events{where} ORDER BY id {order} LIMIT ? OFFSET ?"
        args.extend((page_size, (page - 1) * page_size))
        try:
            with self._lock:
                rows = self._connect().execute(sql, args).fetchall()
        except Exception:
            self._fallback("ROMCloud structured diagnostic query failed")
            return []
        result = []
        for row in rows:
            item = dict(row)
            try:
                item["metadata"] = json.loads(item.pop("metadata_json"))
            except (ValueError, TypeError):
                item["metadata"] = {}
                item.pop("metadata_json", None)
            result.append(item)
        return result

    def operation_chain(self, operation_id: str, *, page: int = 1, page_size: int = 200) -> list[dict[str, Any]]:
        return self.query(DiagnosticQuery(operation_id=operation_id, page=page, page_size=page_size, chronological=True))

    def operation_summaries(
        self, *, subsystem: Optional[str] = None, level: Optional[str] = None,
        operation_id: Optional[str] = None, start_utc: Optional[str] = None,
        end_utc: Optional[str] = None, text: Optional[str] = None,
        page: int = 1, page_size: int = 20,
    ) -> list[dict[str, Any]]:
        """Return indexed operation summaries without rebuilding event chains."""
        size = max(1, min(int(page_size), 100))
        offset = (max(1, int(page)) - 1) * size
        clauses: list[str] = []
        args: list[object] = []
        if subsystem:
            clauses.append("o.subsystem = ?")
            args.append(subsystem)
        if operation_id:
            clauses.append("o.operation_id LIKE ? ESCAPE '\\'")
            escaped_id = operation_id.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            args.append(f"{escaped_id}%")
        if start_utc:
            clauses.append("o.started_at_utc >= ?")
            args.append(start_utc)
        if end_utc:
            clauses.append("o.started_at_utc <= ?")
            args.append(end_utc)
        if text:
            clauses.append("o.search_text LIKE ? ESCAPE '\\'")
            escaped_text = text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            args.append(f"%{escaped_text}%")
        if level:
            clauses.append(
                "EXISTS (SELECT 1 FROM diagnostic_events e "
                "WHERE e.operation_id = o.operation_id AND e.level = ?)"
            )
            args.append(level.upper())
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        try:
            with self._lock:
                rows = self._connect().execute(
                    f"""SELECT o.* FROM diagnostic_operations o{where}
                    ORDER BY o.started_at_utc DESC, o.operation_id DESC
                    LIMIT ? OFFSET ?""",
                    (*args, size, offset),
                ).fetchall()
        except Exception:
            self._fallback("ROMCloud structured operation query failed")
            return []
        summaries = []
        for row in rows:
            item = dict(row)
            item["timestamp_utc"] = item.pop("started_at_utc")
            item.pop("search_text", None)
            summaries.append(item)
        return summaries

    def facets(self) -> dict[str, list[str]]:
        """Return small distinct filter vocabularies for graphical clients."""
        try:
            with self._lock:
                conn = self._connect()
                subsystems = [
                    str(row[0]) for row in conn.execute(
                        "SELECT DISTINCT subsystem FROM diagnostic_events ORDER BY subsystem"
                    )
                ]
                levels = [
                    str(row[0]) for row in conn.execute(
                        "SELECT DISTINCT level FROM diagnostic_events ORDER BY "
                        "CASE level WHEN 'ERROR' THEN 0 WHEN 'WARNING' THEN 1 "
                        "WHEN 'INFO' THEN 2 WHEN 'DEBUG' THEN 3 ELSE 4 END, level"
                    )
                ]
            return {"subsystems": subsystems, "levels": levels}
        except Exception:
            self._fallback("ROMCloud diagnostic facets query failed")
            return {"subsystems": [], "levels": []}

    def prune(self, *, now: Optional[datetime] = None) -> int:
        """Incrementally enforce age, count, and physical-size bounds."""
        removed = 0
        try:
            with self._lock:
                conn = self._connect()
                cutoff = (now or datetime.now(timezone.utc)) - timedelta(days=RETENTION_MAX_AGE_DAYS)
                cursor = conn.execute(
                    "DELETE FROM diagnostic_events WHERE id IN (SELECT id FROM diagnostic_events WHERE timestamp_utc < ? ORDER BY id LIMIT ?)",
                    (cutoff.isoformat(timespec="microseconds"), RETENTION_PRUNE_BATCH),
                )
                removed += max(cursor.rowcount, 0)
                count = int(conn.execute("SELECT COUNT(*) FROM diagnostic_events").fetchone()[0])
                excess = max(0, count - RETENTION_MAX_EVENTS)
                if excess:
                    cursor = conn.execute(
                        "DELETE FROM diagnostic_events WHERE id IN (SELECT id FROM diagnostic_events ORDER BY id LIMIT ?)",
                        (min(excess, RETENTION_PRUNE_BATCH),),
                    )
                    removed += max(cursor.rowcount, 0)
                page_count = int(conn.execute("PRAGMA page_count").fetchone()[0])
                page_size = int(conn.execute("PRAGMA page_size").fetchone()[0])
                if page_count * page_size > RETENTION_MAX_BYTES:
                    cursor = conn.execute(
                        "DELETE FROM diagnostic_events WHERE id IN (SELECT id FROM diagnostic_events ORDER BY id LIMIT ?)",
                        (RETENTION_PRUNE_BATCH,),
                    )
                    removed += max(cursor.rowcount, 0)
                conn.execute(
                    "DELETE FROM diagnostic_operations WHERE NOT EXISTS ("
                    "SELECT 1 FROM diagnostic_events e "
                    "WHERE e.operation_id = diagnostic_operations.operation_id)"
                )
                conn.commit()
                if removed:
                    conn.execute(f"PRAGMA incremental_vacuum({RETENTION_PRUNE_BATCH})")
            return removed
        except Exception:
            self._fallback("ROMCloud structured diagnostic retention failed")
            return removed

    @staticmethod
    def _fallback(message: str) -> None:
        with contextlib.suppress(Exception):
            print(message, file=sys.stderr)


class SQLiteDiagnosticHandler(logging.Handler):
    """Logging bridge that never lets persistence failures escape emit()."""

    def __init__(self, store: DiagnosticStore) -> None:
        super().__init__()
        self.store = store

    def emit(self, record: logging.LogRecord) -> None:
        try:
            exc_type = exc_message = None
            if record.exc_info and record.exc_info[1] is not None:
                exc_type = type(record.exc_info[1]).__name__
                exc_message = str(record.exc_info[1])
            metadata = getattr(record, "diagnostic_metadata", None)
            self.store.write(
                level=record.levelname,
                subsystem=record.name.removeprefix("romcloud."),
                event_code=getattr(record, "event_code", "log"),
                message=record.getMessage(), metadata=metadata,
                operation_id=getattr(record, "operation_id", None),
                parent_operation_id=getattr(record, "parent_operation_id", None),
                exception_type=exc_type, exception_message=exc_message,
            )
        except Exception:
            DiagnosticStore._fallback("ROMCloud structured diagnostic handler failed")

    def close(self) -> None:
        self.store.close()
        super().close()


def configure_diagnostics(path: str | Path) -> Optional[DiagnosticStore]:
    global _active_store
    store = DiagnosticStore(path)
    if not store.initialize():
        return None
    with _active_store_lock:
        previous = _active_store
        _active_store = store
        if previous is not None and previous is not store:
            previous.close()
    return store


def active_store() -> Optional[DiagnosticStore]:
    return _active_store


def event(
    subsystem: str, event_code: str, message: str, *, level: str = "INFO",
    metadata: Optional[Mapping[str, Any]] = None,
    operation_id: Optional[str] = None,
    parent_operation_id: Optional[str] = None,
    exception: Optional[BaseException] = None,
) -> bool:
    store = active_store()
    if store is None:
        return False
    return store.write(
        level=level, subsystem=subsystem, event_code=event_code, message=message,
        metadata=metadata, operation_id=operation_id,
        parent_operation_id=parent_operation_id,
        exception_type=type(exception).__name__ if exception else None,
        exception_message=str(exception) if exception else None,
    )


@contextlib.contextmanager
def operation(
    name: str, *, subsystem: str, source: Optional[str] = None,
    operation_id: Optional[str] = None, parent_operation_id: Optional[str] = None,
    metadata: Optional[Mapping[str, Any]] = None,
) -> Iterator[str]:
    """Bind one correlation ID to an entire synchronous workflow."""
    parent = parent_operation_id or _operation_id.get()
    identifier = (
        operation_id
        or os.environ.get("ROMCLOUD_DIAGNOSTIC_OPERATION_ID")
        or uuid.uuid4().hex
    )
    token_id = _operation_id.set(identifier)
    token_parent = _parent_operation_id.set(parent)
    started = time.monotonic_ns()
    fields = dict(metadata or {})
    fields.update({"operation_name": name, "source": source})
    event(subsystem, "operation.started", f"{name} started", metadata=fields)
    try:
        yield identifier
    except BaseException as exc:
        event(
            subsystem, "operation.failed", f"{name} failed", level="ERROR",
            metadata={**fields, "status": "failed", "duration_ms": (time.monotonic_ns() - started) // 1_000_000},
            exception=exc,
        )
        raise
    else:
        event(
            subsystem, "operation.completed", f"{name} completed",
            metadata={**fields, "status": "success", "duration_ms": (time.monotonic_ns() - started) // 1_000_000},
        )
    finally:
        _parent_operation_id.reset(token_parent)
        _operation_id.reset(token_id)


def current_operation_id() -> Optional[str]:
    return _operation_id.get()


def correlated_operation(
    name: str, *, subsystem: str = "savesync", source: Optional[str] = None
):
    """Decorator that creates a workflow ID only at the outermost entrypoint."""
    def decorate(function):  # noqa: ANN001, ANN202
        @wraps(function)
        def wrapped(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
            existing = current_operation_id()
            owner = args[0] if args else None
            owner_values = getattr(owner, "__dict__", {})
            provider = owner_values.get("_provider")
            service = owner_values.get("_service")
            if provider is None:
                provider = getattr(service, "__dict__", {}).get("_provider")
            context = {
                "effective_mode": owner_values.get(
                    "_effective_mode",
                    getattr(service, "__dict__", {}).get("_effective_mode"),
                ),
                "provider_id": getattr(provider, "provider_id", None),
                "provider_type": type(provider).__name__ if provider is not None else None,
            }
            if existing is not None:
                event(
                    subsystem, "operation.stage", f"{name} entered",
                    metadata={"operation_name": name, "source": source, **context},
                )
                result = function(*args, **kwargs)
                _record_operation_result(subsystem, name, result)
                return result
            try:
                with operation(
                    name, subsystem=subsystem, source=source, metadata=context
                ) as identifier:
                    result = function(*args, **kwargs)
                    _record_operation_result(subsystem, name, result)
                    return result
            except BaseException as exc:
                # Detached continuations can inherit the failed handoff's ID.
                with contextlib.suppress(Exception):
                    setattr(exc, "diagnostic_operation_id", identifier)
                raise
        return wrapped
    return decorate


def _record_operation_result(subsystem: str, name: str, result: Any) -> None:
    """Record useful common result fields without serializing domain objects."""
    report = getattr(result, "report", None) or result
    metadata: dict[str, Any] = {
        "operation_name": name,
        "status": getattr(result, "status", "success"),
        "reason": getattr(result, "reason", None),
    }
    for field_name in ("uploaded", "downloaded", "conflicts", "unchanged"):
        value = getattr(report, field_name, None)
        if value is not None:
            metadata[field_name] = value
    for field_name in ("cursor_before", "cursor_after", "processed_entries", "processed_groups"):
        value = getattr(result, field_name, None)
        if value is not None:
            metadata[field_name] = value
    processed_groups = metadata.get("processed_groups")
    if isinstance(processed_groups, (list, tuple, set, frozenset)):
        metadata["examined"] = len(processed_groups)
    generation = getattr(result, "remote_generation", None)
    if generation is not None:
        metadata["generation"] = generation
    event(
        subsystem, "operation.result", f"{name} result recorded", metadata=metadata
    )
