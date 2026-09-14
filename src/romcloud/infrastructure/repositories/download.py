"""SQLite repositories for durable downloads, staging, and reservations."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable, Optional
import uuid
import sqlite3

from romcloud.core.models.download import DownloadItem, DownloadOrigin, DownloadState
from romcloud.infrastructure.database import Database


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse(value: Optional[str]) -> Optional[datetime]:
    return datetime.fromisoformat(value) if value else None


@dataclass(frozen=True)
class StagingFileRecord:
    asset_relative_path: str
    member_relative_path: str
    expected_size: Optional[int]
    source_object_id: Optional[str]
    source_revision: Optional[str]
    source_checksum: Optional[str]
    source_modified_epoch: Optional[float]
    state: str
    checkpoint_bytes: int
    checkpoint_sha256: Optional[str]
    content_sha256: Optional[str]


@dataclass(frozen=True)
class StagingAssetRecord:
    relative_path: str
    system: str
    asset_kind: str
    expected_size: Optional[int]
    source_manifest_sha256: Optional[str]
    updated_at: datetime


class DownloadRepository:
    TERMINAL_STATES = ("complete", "cancelled", "failed")

    def __init__(self, db: Database) -> None:
        self._db = db

    def enqueue(
        self,
        *,
        game_id: str,
        game_title: str,
        system: str,
        origin: DownloadOrigin,
        batch_id: Optional[str] = None,
    ) -> tuple[DownloadItem, bool]:
        now = _now().isoformat()
        with self._db.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                """
                SELECT * FROM download_items
                WHERE game_id = ? AND state IN
                    ('queued','running','paused','interrupted','verifying')
                LIMIT 1
                """,
                (game_id,),
            ).fetchone()
            if existing is not None:
                return self._row(existing), False
            sequence = int(
                conn.execute(
                    "SELECT COALESCE(MAX(queue_seq), 0) + 1 FROM download_items"
                ).fetchone()[0]
            )
            item_id = uuid.uuid4().hex
            conn.execute(
                """
                INSERT INTO download_items
                    (id,batch_id,game_id,game_title,system,origin,state,queue_seq,
                     bytes_total,bytes_present,created_at,updated_at)
                VALUES (?,?,?,?,?,?, 'queued', ?,0,0,?,?)
                """,
                (
                    item_id,
                    batch_id,
                    game_id,
                    game_title,
                    system,
                    origin.value,
                    sequence,
                    now,
                    now,
                ),
            )
            row = conn.execute(
                "SELECT * FROM download_items WHERE id = ?", (item_id,)
            ).fetchone()
            assert row is not None
            return self._row(row), True

    def get(self, item_id: str) -> Optional[DownloadItem]:
        with self._db.connect() as conn:
            row = conn.execute(
                "SELECT * FROM download_items WHERE id = ?", (item_id,)
            ).fetchone()
        return self._row(row) if row is not None else None

    def list(self, *, terminal_limit: int = 100) -> list[DownloadItem]:
        with self._db.connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM download_items
                WHERE state NOT IN ('complete','cancelled','failed')
                UNION ALL
                SELECT * FROM (
                    SELECT * FROM download_items
                    WHERE state IN ('complete','cancelled','failed')
                    ORDER BY updated_at DESC LIMIT ?
                )
                ORDER BY queue_seq
                """,
                (terminal_limit,),
            ).fetchall()
        return [self._row(row) for row in rows]

    def list_all(self) -> list[DownloadItem]:
        with self._db.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM download_items ORDER BY queue_seq"
            ).fetchall()
        return [self._row(row) for row in rows]

    def next_queued(self) -> Optional[DownloadItem]:
        with self._db.connect() as conn:
            row = conn.execute(
                "SELECT * FROM download_items WHERE state = 'queued' "
                "ORDER BY queue_seq LIMIT 1"
            ).fetchone()
        return self._row(row) if row is not None else None

    def transition(
        self,
        item_id: str,
        *,
        from_states: Iterable[DownloadState],
        to_state: DownloadState,
        worker_instance_id: Optional[str] = None,
        error_code: Optional[str] = None,
        error_message: Optional[str] = None,
    ) -> bool:
        allowed = tuple(state.value for state in from_states)
        if not allowed:
            return False
        placeholders = ",".join("?" for _ in allowed)
        now = _now().isoformat()
        started = now if to_state in {DownloadState.RUNNING, DownloadState.VERIFYING} else None
        finished = now if to_state in {
            DownloadState.COMPLETE,
            DownloadState.CANCELLED,
            DownloadState.FAILED,
        } else None
        with self._db.connect() as conn:
            try:
                cursor = conn.execute(
                    f"""
                    UPDATE download_items SET state=?, worker_instance_id=?, updated_at=?,
                        started_at=COALESCE(started_at, ?),
                        finished_at=?, error_code=?, error_message=?
                    WHERE id=? AND state IN ({placeholders})
                    """,
                    (
                        to_state.value,
                        worker_instance_id,
                        now,
                        started,
                        finished,
                        error_code,
                        error_message,
                        item_id,
                        *allowed,
                    ),
                )
            except sqlite3.IntegrityError:
                return False
            return cursor.rowcount == 1

    def update_progress(self, item_id: str, present: int, total: int) -> None:
        now = _now().isoformat()
        with self._db.connect() as conn:
            conn.execute(
                """
                UPDATE download_items
                SET bytes_present=?, bytes_total=?, updated_at=? WHERE id=?
                """,
                (max(0, present), max(0, total), now, item_id),
            )

    def recover_interrupted(self) -> int:
        now = _now().isoformat()
        with self._db.connect() as conn:
            cursor = conn.execute(
                """
                UPDATE download_items
                SET state='interrupted', worker_instance_id=NULL, updated_at=?
                WHERE state IN ('running','verifying')
                """,
                (now,),
            )
            return cursor.rowcount

    def queued_cancel_all(self) -> int:
        now = _now().isoformat()
        with self._db.connect() as conn:
            cursor = conn.execute(
                """
                UPDATE download_items SET state='cancelled', updated_at=?, finished_at=?
                WHERE state='queued'
                """,
                (now, now),
            )
            return cursor.rowcount

    def retry_all_failed(self) -> int:
        now = _now().isoformat()
        with self._db.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute(
                "SELECT id, game_id FROM download_items WHERE state='failed' "
                "ORDER BY updated_at DESC"
            ).fetchall()
            retried = 0
            claimed_games: set[str] = set()
            for row in rows:
                game_id = row["game_id"]
                if game_id is None or game_id in claimed_games:
                    continue
                active = conn.execute(
                    """
                    SELECT 1 FROM download_items WHERE game_id=? AND state IN
                        ('queued','running','paused','interrupted','verifying') LIMIT 1
                    """,
                    (game_id,),
                ).fetchone()
                if active is not None:
                    continue
                cursor = conn.execute(
                    """
                    UPDATE download_items SET state='queued', updated_at=?, finished_at=NULL,
                        error_code=NULL, error_message=NULL WHERE id=? AND state='failed'
                    """,
                    (now, row["id"]),
                )
                retried += cursor.rowcount
                claimed_games.add(game_id)
            return retried

    def delete_queued(self, item_id: str) -> bool:
        with self._db.connect() as conn:
            cursor = conn.execute(
                "DELETE FROM download_items WHERE id=? AND state='queued'", (item_id,)
            )
            return cursor.rowcount == 1

    def prune_terminal(self, *, keep: int = 200) -> int:
        with self._db.connect() as conn:
            cursor = conn.execute(
                """
                DELETE FROM download_items WHERE id IN (
                    SELECT id FROM download_items
                    WHERE state IN ('complete','cancelled','failed')
                      AND NOT EXISTS (
                          SELECT 1 FROM cache_members AS member
                          JOIN cache_staging_assets AS staged
                            ON staged.relative_path = member.relative_path
                          WHERE member.game_id = download_items.game_id
                      )
                    ORDER BY updated_at DESC LIMIT -1 OFFSET ?
                )
                """,
                (keep,),
            )
            return cursor.rowcount

    @staticmethod
    def _row(row) -> DownloadItem:  # type: ignore[no-untyped-def]
        return DownloadItem(
            id=row["id"], batch_id=row["batch_id"], game_id=row["game_id"],
            game_title=row["game_title"], system=row["system"],
            origin=DownloadOrigin(row["origin"]), state=DownloadState(row["state"]),
            queue_seq=row["queue_seq"], worker_instance_id=row["worker_instance_id"],
            bytes_total=row["bytes_total"], bytes_present=row["bytes_present"],
            error_code=row["error_code"], error_message=row["error_message"],
            created_at=_parse(row["created_at"]) or _now(),
            updated_at=_parse(row["updated_at"]) or _now(),
            started_at=_parse(row["started_at"]), finished_at=_parse(row["finished_at"]),
        )


class StagingRepository:
    def __init__(self, db: Database) -> None:
        self._db = db

    def replace_plan(
        self,
        *,
        relative_path: str,
        system: str,
        asset_kind: str,
        source_provider: str,
        source_root: str,
        expected_size: Optional[int],
        source_manifest_sha256: str,
        files: list[dict[str, object]],
    ) -> None:
        now = _now().isoformat()
        with self._db.connect() as conn:
            conn.execute(
                """
                INSERT INTO cache_staging_assets
                    (relative_path,system,asset_kind,source_provider,source_root,
                     expected_size,source_manifest_sha256,manifest_version,created_at,updated_at)
                VALUES (?,?,?,?,?,?,?,1,?,?)
                ON CONFLICT(relative_path) DO UPDATE SET
                    system=excluded.system, asset_kind=excluded.asset_kind,
                    source_provider=excluded.source_provider, source_root=excluded.source_root,
                    expected_size=excluded.expected_size,
                    source_manifest_sha256=excluded.source_manifest_sha256,
                    updated_at=excluded.updated_at
                """,
                (relative_path, system, asset_kind, source_provider, source_root,
                 expected_size, source_manifest_sha256, now, now),
            )
            existing = {
                row[0]: row
                for row in conn.execute(
                    "SELECT member_relative_path, state, checkpoint_bytes, checkpoint_sha256, content_sha256 "
                    "FROM cache_staging_files WHERE asset_relative_path=?",
                    (relative_path,),
                ).fetchall()
            }
            # Reconciliation happens only when execution begins. A previous
            # process may have died with a member marked transferring.
            conn.execute(
                "UPDATE cache_staging_files SET state='partial' "
                "WHERE asset_relative_path=? AND state='transferring'",
                (relative_path,),
            )
            wanted = {str(item["member_relative_path"]) for item in files}
            conn.execute(
                "DELETE FROM cache_staging_files WHERE asset_relative_path=? AND member_relative_path NOT IN ("
                + (",".join("?" for _ in wanted) if wanted else "''") + ")",
                (relative_path, *sorted(wanted)),
            )
            for item in files:
                member = str(item["member_relative_path"])
                old = existing.get(member)
                conn.execute(
                    """
                    INSERT INTO cache_staging_files
                        (asset_relative_path,member_relative_path,expected_size,
                         source_object_id,source_revision,source_checksum,source_modified_epoch,
                         state,checkpoint_bytes,checkpoint_sha256,content_sha256)
                    VALUES (?,?,?,?,?,?,?, ?,?,?,?)
                    ON CONFLICT(asset_relative_path,member_relative_path) DO UPDATE SET
                        expected_size=excluded.expected_size,
                        source_object_id=excluded.source_object_id,
                        source_revision=excluded.source_revision,
                        source_checksum=excluded.source_checksum,
                        source_modified_epoch=excluded.source_modified_epoch
                    """,
                    (
                        relative_path, member, item.get("expected_size"),
                        item.get("source_object_id"), item.get("source_revision"),
                        item.get("source_checksum"), item.get("source_modified_epoch"),
                        ("partial" if old and old[1] == "transferring" else old[1]) if old else "pending", old[2] if old else 0,
                        old[3] if old else None, old[4] if old else None,
                    ),
                )

    def get_file(self, asset: str, member: str) -> Optional[StagingFileRecord]:
        with self._db.connect() as conn:
            row = conn.execute(
                "SELECT * FROM cache_staging_files WHERE asset_relative_path=? AND member_relative_path=?",
                (asset, member),
            ).fetchone()
        if row is None:
            return None
        return StagingFileRecord(
            asset_relative_path=row["asset_relative_path"],
            member_relative_path=row["member_relative_path"],
            expected_size=row["expected_size"], source_object_id=row["source_object_id"],
            source_revision=row["source_revision"], source_checksum=row["source_checksum"],
            source_modified_epoch=row["source_modified_epoch"], state=row["state"],
            checkpoint_bytes=row["checkpoint_bytes"], checkpoint_sha256=row["checkpoint_sha256"],
            content_sha256=row["content_sha256"],
        )

    def get_asset(self, relative_path: str) -> Optional[StagingAssetRecord]:
        with self._db.connect() as conn:
            row = conn.execute(
                "SELECT * FROM cache_staging_assets WHERE relative_path=?",
                (relative_path,),
            ).fetchone()
        if row is None:
            return None
        return StagingAssetRecord(
            relative_path=row["relative_path"],
            system=row["system"],
            asset_kind=row["asset_kind"],
            expected_size=row["expected_size"],
            source_manifest_sha256=row["source_manifest_sha256"],
            updated_at=_parse(row["updated_at"]) or _now(),
        )

    def list_files(self, relative_path: str) -> list[StagingFileRecord]:
        with self._db.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM cache_staging_files WHERE asset_relative_path=? "
                "ORDER BY member_relative_path",
                (relative_path,),
            ).fetchall()
        return [
            StagingFileRecord(
                asset_relative_path=row["asset_relative_path"],
                member_relative_path=row["member_relative_path"],
                expected_size=row["expected_size"],
                source_object_id=row["source_object_id"],
                source_revision=row["source_revision"],
                source_checksum=row["source_checksum"],
                source_modified_epoch=row["source_modified_epoch"],
                state=row["state"], checkpoint_bytes=row["checkpoint_bytes"],
                checkpoint_sha256=row["checkpoint_sha256"],
                content_sha256=row["content_sha256"],
            )
            for row in rows
        ]

    def list_assets(self) -> list[StagingAssetRecord]:
        with self._db.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM cache_staging_assets ORDER BY relative_path"
            ).fetchall()
        return [
            StagingAssetRecord(
                relative_path=row["relative_path"], system=row["system"],
                asset_kind=row["asset_kind"], expected_size=row["expected_size"],
                source_manifest_sha256=row["source_manifest_sha256"],
                updated_at=_parse(row["updated_at"]) or _now(),
            )
            for row in rows
        ]

    def owner_game_ids(self, relative_path: str) -> set[str]:
        with self._db.connect() as conn:
            rows = conn.execute(
                "SELECT game_id FROM cache_members WHERE relative_path=?",
                (relative_path,),
            ).fetchall()
        return {str(row["game_id"]) for row in rows}

    def checkpoint(self, asset: str, member: str, size: int, digest: str, *, state: str) -> None:
        with self._db.connect() as conn:
            conn.execute(
                """
                UPDATE cache_staging_files SET state=?, checkpoint_bytes=?,
                    checkpoint_sha256=?
                WHERE asset_relative_path=? AND member_relative_path=?
                """,
                (state, size, digest, asset, member),
            )
            conn.execute(
                "UPDATE cache_staging_assets SET updated_at=? WHERE relative_path=?",
                (_now().isoformat(), asset),
            )

    def complete(self, asset: str, member: str, size: int, digest: str) -> None:
        now = _now().isoformat()
        with self._db.connect() as conn:
            conn.execute(
                """
                UPDATE cache_staging_files SET state='complete', checkpoint_bytes=?,
                    checkpoint_sha256=?, content_sha256=?, completed_at=?
                WHERE asset_relative_path=? AND member_relative_path=?
                """,
                (size, digest, digest, now, asset, member),
            )
            conn.execute(
                "UPDATE cache_staging_assets SET updated_at=? WHERE relative_path=?",
                (now, asset),
            )

    def delete_asset(self, relative_path: str) -> None:
        with self._db.connect() as conn:
            conn.execute("DELETE FROM cache_staging_assets WHERE relative_path=?", (relative_path,))

    def bytes_present(self) -> int:
        with self._db.connect() as conn:
            row = conn.execute(
                "SELECT COALESCE(SUM(checkpoint_bytes),0) FROM cache_staging_files"
            ).fetchone()
        return int(row[0])

    def stats_for_game(self, game_id: str) -> dict[str, int]:
        with self._db.connect() as conn:
            row = conn.execute(
                """
                SELECT
                    COUNT(*) AS total_files,
                    COALESCE(SUM(CASE WHEN file.state='complete' OR file.checkpoint_bytes>0 THEN 1 ELSE 0 END),0) AS retained_files,
                    COALESCE(SUM(CASE WHEN file.state IN ('partial','transferring') THEN 1 ELSE 0 END),0) AS interrupted_files,
                    COALESCE(SUM(CASE WHEN file.state!='complete' THEN 1 ELSE 0 END),0) AS remaining_files,
                    COALESCE(SUM(file.checkpoint_bytes),0) AS staged_bytes
                FROM cache_staging_files AS file
                JOIN cache_members AS member
                  ON member.relative_path=file.asset_relative_path
                WHERE member.game_id=?
                """,
                (game_id,),
            ).fetchone()
            retained = conn.execute(
                """
                SELECT COALESCE(SUM(
                    CASE WHEN EXISTS (
                        SELECT 1 FROM cache_staging_files AS staged
                        WHERE staged.asset_relative_path=member.relative_path
                    ) THEN (
                        SELECT COALESCE(SUM(staged.checkpoint_bytes),0)
                        FROM cache_staging_files AS staged
                        WHERE staged.asset_relative_path=member.relative_path
                    ) ELSE member.size_bytes END
                ),0)
                FROM cache_members AS member WHERE member.game_id=?
                """,
                (game_id,),
            ).fetchone()[0]
        result = {key: int(row[key]) for key in (
            "total_files", "retained_files", "interrupted_files", "remaining_files",
        )}
        result["retained_bytes"] = int(retained)
        return result
