"""Focused durability, resume, locking, and queue-state coverage."""

from __future__ import annotations

import hashlib
import sqlite3
import time
from contextlib import AbstractContextManager
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from romcloud.core.cancellation import TransferCancellationToken
from romcloud.core.exceptions import InsufficientSpaceError, TransferCancelledError
from romcloud.core.models.cache import CacheEntry, CachePolicy
from romcloud.core.models.download import DownloadOrigin, DownloadState
from romcloud.core.models.game import Game, GameAsset
from romcloud.infrastructure.cache_coordination import (
    AssetLockManager,
    CacheStorageCoordinator,
    LockUnavailable,
    ReservationLease,
)
from romcloud.infrastructure.database import Database
from romcloud.infrastructure.providers.local import LocalFilesystemProvider
from romcloud.infrastructure.repositories.cache import CacheRepository
from romcloud.infrastructure.repositories.download import (
    DownloadRepository,
    StagingRepository,
)
from romcloud.infrastructure.repositories.game import GameRepository
from romcloud.services.download_manager import DownloadManagerService
from romcloud.services.cache import CacheService
from romcloud.services.transfer import TransferService


def _game(game_repo: GameRepository, root: Path, name: str = "Game.iso", size: int = 1) -> Game:
    (root / "ps2").mkdir(parents=True, exist_ok=True)
    source = root / "ps2" / name
    source.write_bytes((b"A" * size))
    game = Game.create(
        "ps2", source.stem, "local", str(root),
        [GameAsset(name, f"ps2/{name}", size_bytes=size, is_primary=True)],
    )
    game_repo.save(game)
    return game


def test_schema_v4_migration_preserves_v3_catalog_cache_and_pin(tmp_path):
    path = tmp_path / "catalog.db"
    db = Database(str(path))
    db.initialize()
    game_repo = GameRepository(db)
    cache_repo = CacheRepository(db)
    game = _game(game_repo, tmp_path / "source")
    from romcloud.core.models.cache import CacheEntry, CacheStatus

    entry = CacheEntry.create(game.id, str(tmp_path / "cache" / "ps2" / "Game.iso"))
    entry.status = CacheStatus.INCOMPLETE
    entry.is_pinned = True
    cache_repo.save(entry)
    with db.connect() as conn:
        conn.execute("UPDATE schema_version SET version=3")
        conn.executescript(
            """
            DROP TABLE cache_reservations;
            DROP TABLE cache_staging_files;
            DROP TABLE cache_staging_assets;
            DROP TABLE download_items;
            """
        )

    db.initialize()
    db.initialize()
    with db.connect() as conn:
        assert conn.execute("SELECT version FROM schema_version").fetchone()[0] == 4
        assert conn.execute("SELECT title FROM games WHERE id=?", (game.id,)).fetchone()[0] == game.title
        assert conn.execute("SELECT is_pinned FROM cache_entries WHERE game_id=?", (game.id,)).fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM download_items").fetchone()[0] == 0


def test_download_repository_dedupes_active_and_recovers_only_inflight(db, game_repo, tmp_path):
    first = _game(game_repo, tmp_path / "source", "A.iso")
    second = _game(game_repo, tmp_path / "source", "B.iso")
    repo = DownloadRepository(db)
    one, created = repo.enqueue(
        game_id=first.id, game_title=first.title, system=first.system,
        origin=DownloadOrigin.MANUAL,
    )
    duplicate, duplicate_created = repo.enqueue(
        game_id=first.id, game_title=first.title, system=first.system,
        origin=DownloadOrigin.PINNED,
    )
    two, _ = repo.enqueue(
        game_id=second.id, game_title=second.title, system=second.system,
        origin=DownloadOrigin.MANUAL,
    )
    assert created is True and duplicate_created is False and duplicate.id == one.id
    assert repo.transition(one.id, from_states=[DownloadState.QUEUED], to_state=DownloadState.RUNNING)
    assert repo.transition(two.id, from_states=[DownloadState.QUEUED], to_state=DownloadState.VERIFYING)
    assert repo.recover_interrupted() == 2
    assert repo.get(one.id).state is DownloadState.INTERRUPTED
    assert repo.get(two.id).state is DownloadState.INTERRUPTED


def test_terminal_download_history_is_pruned_to_configured_bound(db, game_repo, tmp_path):
    game = _game(game_repo, tmp_path / "source")
    repo = DownloadRepository(db)
    for _index in range(205):
        item, created = repo.enqueue(
            game_id=game.id, game_title=game.title, system=game.system,
            origin=DownloadOrigin.MANUAL,
        )
        assert created
        assert repo.transition(
            item.id, from_states=[DownloadState.QUEUED], to_state=DownloadState.COMPLETE
        )

    assert repo.prune_terminal(keep=200) == 5
    assert len(repo.list_all()) == 200


def test_enqueue_persists_intent_without_claiming_cache_ownership(
    db, game_repo, cache_dir, tmp_path
):
    game = _game(game_repo, tmp_path / "source")
    manager = DownloadManagerService(
        repository=DownloadRepository(db), staging_repository=StagingRepository(db),
        game_repo=game_repo, cache=_WorkerCache(), cache_root=str(cache_dir),
    )

    first = manager.enqueue([game.id])
    duplicate = manager.enqueue([game.id], origin=DownloadOrigin.PINNED)

    assert first["created"] == 1 and duplicate["created"] == 0
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM cache_entries").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM cache_members").fetchone()[0] == 0


def test_status_combines_valid_final_members_with_staging_checkpoints(
    db, game_repo, cache_repo, cache_dir, tmp_path
):
    source_root = tmp_path / "source"
    (source_root / "psx").mkdir(parents=True)
    assets = [
        GameAsset("Shared.chd", "psx/Shared.chd", 10, False),
        GameAsset("Game.m3u", "psx/Game.m3u", 20, True),
    ]
    game = Game.create("psx", "Game", "local", str(source_root), assets)
    game_repo.save(game)
    cache_repo.save(CacheEntry.create(game.id, str(cache_dir / "psx" / "Game.m3u")))
    cache_repo.replace_membership(
        game.id, assets, {"psx/Shared.chd": 10, "psx/Game.m3u": 0}
    )
    staging = StagingRepository(db)
    staging.replace_plan(
        relative_path="psx/Game.m3u", system="psx", asset_kind="file",
        source_provider="local", source_root=str(source_root), expected_size=20,
        source_manifest_sha256="manifest", files=[{
            "member_relative_path": "", "expected_size": 20,
            "source_object_id": None, "source_revision": None,
            "source_checksum": None, "source_modified_epoch": None,
        }],
    )
    staging.checkpoint("psx/Game.m3u", "", 4, "digest", state="partial")
    repo = DownloadRepository(db)
    item, _ = repo.enqueue(
        game_id=game.id, game_title=game.title, system=game.system,
        origin=DownloadOrigin.MANUAL,
    )
    repo.update_progress(item.id, 4, 30)
    assert repo.transition(
        item.id, from_states=[DownloadState.QUEUED], to_state=DownloadState.FAILED
    )
    manager = DownloadManagerService(
        repository=repo, staging_repository=staging, game_repo=game_repo,
        cache=_WorkerCache(), cache_root=str(cache_dir),
    )

    payload = manager.status()["failed"][0]

    assert payload["bytes_present"] == 14
    assert payload["has_partial"] is True


def test_asset_locks_collide_by_physical_path_without_global_serialization(tmp_path):
    manager = AssetLockManager(tmp_path / "locks")
    first = manager.acquire([tmp_path / "cache" / "psx" / "Shared.chd"], blocking=False)
    with pytest.raises(LockUnavailable):
        manager.acquire([tmp_path / "cache" / "psx" / "Shared.chd"], blocking=False)
    unrelated = manager.acquire([tmp_path / "cache" / "ps2" / "Other.iso"], blocking=False)
    unrelated.release()
    first.release()


def test_reservations_count_future_growth_and_reject_concurrent_overcommit(
    db, cache_repo, cache_dir, data_dir
):
    coordinator = CacheStorageCoordinator(
        db=db, cache_repo=cache_repo, cache_root=cache_dir,
        lock_root=data_dir / "locks", max_size_bytes=100, min_free_bytes=0,
    )
    partial = cache_dir / ".partial" / "ps2" / "Game.iso.part"
    partial.parent.mkdir(parents=True)
    partial.write_bytes(b"x" * 37)
    lease = coordinator.admit(
        requested_growth=33, game_id=None, owner_kind="cli", owner_instance_id="one"
    )
    with db.connect() as conn:
        assert conn.execute("SELECT reserved_bytes FROM cache_reservations").fetchone()[0] == 33
    with pytest.raises(InsufficientSpaceError):
        coordinator.admit(
            requested_growth=31, game_id=None, owner_kind="cli", owner_instance_id="two"
        )
    lease.release()


def test_live_reservation_owner_lock_prevents_stale_reclamation(
    db, cache_repo, cache_dir, data_dir
):
    coordinator = CacheStorageCoordinator(
        db=db, cache_repo=cache_repo, cache_root=cache_dir,
        lock_root=data_dir / "locks", max_size_bytes=100, min_free_bytes=0,
    )
    first = coordinator.admit(
        requested_growth=30, game_id=None, owner_kind="cli", owner_instance_id="first"
    )
    second = coordinator.admit(
        requested_growth=20, game_id=None, owner_kind="cli", owner_instance_id="second"
    )
    try:
        with db.connect() as conn:
            rows = conn.execute(
                "SELECT id,reserved_bytes FROM cache_reservations ORDER BY reserved_bytes"
            ).fetchall()
        assert [(row["id"], row["reserved_bytes"]) for row in rows] == [
            (second.id, 20), (first.id, 30),
        ]
    finally:
        second.release()
        first.release()


def _coordinated_cache(db, cache_repo, game_repo, cache_dir, data_dir, source_root, capacity):
    staging = StagingRepository(db)
    transfer = TransferService(
        LocalFilesystemProvider(), str(cache_dir), source_root=str(source_root),
        staging_repository=staging,
    )
    coordinator = CacheStorageCoordinator(
        db=db, cache_repo=cache_repo, cache_root=cache_dir,
        lock_root=data_dir / "locks", max_size_bytes=capacity, min_free_bytes=0,
    )
    cache = CacheService(
        cache_repo, game_repo, transfer, str(cache_dir),
        CachePolicy(max_size_bytes=capacity, min_free_bytes=0),
        storage_coordinator=coordinator,
    )
    return cache, transfer, staging, coordinator


def test_post_admission_exception_releases_row_and_os_lease(
    db, cache_repo, game_repo, cache_dir, data_dir, tmp_path, monkeypatch
):
    source_root = tmp_path / "source"
    game = _game(game_repo, source_root, size=32)
    cache, _transfer, _staging, coordinator = _coordinated_cache(
        db, cache_repo, game_repo, cache_dir, data_dir, source_root, 64
    )
    monkeypatch.setattr(
        cache_repo, "save", lambda _entry: (_ for _ in ()).throw(OSError("db write failed"))
    )

    with pytest.raises(OSError, match="db write failed"):
        cache.cache_game(game.id)

    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM cache_reservations").fetchone()[0] == 0
    # A second lease proves the first process-level reservation lock was also
    # closed rather than merely deleting its SQLite row.
    lease = coordinator.admit(
        requested_growth=64, game_id=None, owner_kind="cli", owner_instance_id="retry"
    )
    lease.release()


def test_invalid_partial_and_promotion_never_open_capacity_hole(
    db, cache_repo, game_repo, cache_dir, data_dir, tmp_path, monkeypatch
):
    source_root = tmp_path / "source"
    game = _game(game_repo, source_root, size=70)
    cache, _transfer, staging, coordinator = _coordinated_cache(
        db, cache_repo, game_repo, cache_dir, data_dir, source_root, 70
    )
    relative = "ps2/Game.iso"
    staging.replace_plan(
        relative_path=relative, system="ps2", asset_kind="file",
        source_provider="local", source_root=str(source_root), expected_size=70,
        source_manifest_sha256="stale", files=[{
            "member_relative_path": "", "expected_size": 70,
            "source_object_id": None, "source_revision": None,
            "source_checksum": None, "source_modified_epoch": None,
        }],
    )
    part = cache_dir / ".partial" / "ps2" / "Game.iso.part"
    part.parent.mkdir(parents=True)
    part.write_bytes(b"B" * 37)
    staging.checkpoint(
        relative, "", 37, hashlib.sha256(part.read_bytes()).hexdigest(), state="partial"
    )

    protected_windows: list[tuple[int, int, int]] = []
    original_admit = coordinator.admit
    original_protect = ReservationLease.protect_removal

    def protected_removal(lease, amount):
        original_protect(lease, amount)
        snapshot = coordinator.snapshot()
        protected_windows.append((
            snapshot["final_bytes"], snapshot["staging_bytes"],
            snapshot["reserved_growth"],
        ))
        with pytest.raises(InsufficientSpaceError):
            original_admit(
                requested_growth=1, game_id=None, owner_kind="cli",
                owner_instance_id="contender",
            )

    monkeypatch.setattr(
        "romcloud.infrastructure.cache_coordination.ReservationLease.protect_removal",
        protected_removal,
    )
    final = Path(cache.cache_game(game.id))

    assert final.read_bytes() == b"A" * 70
    # One protected transition covers invalid partial truncation and another
    # covers staging-to-final promotion. Each keeps at least the 70-byte target
    # continuously owned in final + staging + reservation accounting.
    assert len(protected_windows) >= 2
    assert all(sum(window) >= 70 for window in protected_windows)
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM cache_reservations").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM cache_staging_assets").fetchone()[0] == 0


def test_stale_reservation_reclaim_keeps_promoted_recovery_bytes_counted(
    db, cache_repo, cache_dir, data_dir
):
    relative = "ps2/Promoted.iso"
    final = cache_dir / "ps2" / "Promoted.iso"
    final.parent.mkdir(parents=True)
    final.write_bytes(b"P" * 70)
    staging = StagingRepository(db)
    staging.replace_plan(
        relative_path=relative, system="ps2", asset_kind="file",
        source_provider="local", source_root="/source", expected_size=70,
        source_manifest_sha256="manifest", files=[{
            "member_relative_path": "", "expected_size": 70,
            "source_object_id": None, "source_revision": None,
            "source_checksum": None, "source_modified_epoch": None,
        }],
    )
    staging.complete(relative, "", 70, hashlib.sha256(final.read_bytes()).hexdigest())
    now = datetime.now(timezone.utc).isoformat()
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO cache_reservations "
            "(id,owner_kind,owner_instance_id,reserved_bytes,created_at,updated_at) "
            "VALUES ('dead','cli','gone',70,?,?)",
            (now, now),
        )
    coordinator = CacheStorageCoordinator(
        db=db, cache_repo=cache_repo, cache_root=cache_dir,
        lock_root=data_dir / "locks", max_size_bytes=70, min_free_bytes=0,
    )

    lease = coordinator.admit(
        requested_growth=0, game_id=None, owner_kind="cli", owner_instance_id="live"
    )
    snapshot = coordinator.snapshot()
    lease.release()

    assert snapshot["staging_bytes"] == 70
    assert snapshot["reserved_growth"] == 0
    with db.connect() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM cache_reservations WHERE id='dead'"
        ).fetchone()[0] == 0


def test_directory_replacement_final_and_backup_are_both_accounted(
    db, cache_repo, cache_dir, data_dir
):
    staging = StagingRepository(db)
    staging.replace_plan(
        relative_path="ps3/Game.ps3", system="ps3", asset_kind="directory",
        source_provider="local", source_root="/source", expected_size=5,
        source_manifest_sha256="manifest", files=[{
            "member_relative_path": "USRDIR/EBOOT.BIN", "expected_size": 5,
            "source_object_id": None, "source_revision": None,
            "source_checksum": None, "source_modified_epoch": None,
        }],
    )
    final = cache_dir / "ps3" / "Game.ps3"
    backup = final.with_name(final.name + ".romcloud-replaced")
    (final / "USRDIR").mkdir(parents=True)
    (final / "USRDIR" / "EBOOT.BIN").write_bytes(b"N" * 5)
    (backup / "USRDIR").mkdir(parents=True)
    (backup / "USRDIR" / "EBOOT.BIN").write_bytes(b"O" * 7)
    coordinator = CacheStorageCoordinator(
        db=db, cache_repo=cache_repo, cache_root=cache_dir,
        lock_root=data_dir / "locks", max_size_bytes=100, min_free_bytes=0,
    )

    assert coordinator.snapshot()["staging_bytes"] == 12


def test_admission_accounting_does_not_scan_unrelated_cache_trees(
    db, cache_repo, cache_dir, data_dir, monkeypatch
):
    partial = cache_dir / ".partial" / "ps2" / "Game.iso.part"
    partial.parent.mkdir(parents=True)
    partial.write_bytes(b"P")
    unrelated = cache_dir / "ps3" / "Huge.ps3"
    unrelated.mkdir(parents=True)
    (unrelated / "orphan.romcloud-replaced").write_bytes(b"not recovery state")
    coordinator = CacheStorageCoordinator(
        db=db, cache_repo=cache_repo, cache_root=cache_dir,
        lock_root=data_dir / "locks", max_size_bytes=100, min_free_bytes=0,
    )
    scanned: list[Path] = []
    original_rglob = Path.rglob

    def track_rglob(path, pattern):
        scanned.append(path)
        return original_rglob(path, pattern)

    monkeypatch.setattr(Path, "rglob", track_rglob)

    assert coordinator.snapshot()["staging_bytes"] == 1
    assert scanned == [cache_dir / ".partial"]


def test_descriptor_transfer_uses_the_exact_snapshot_paired_with_its_locks(
    db, cache_repo, game_repo, cache_dir, data_dir, tmp_path, monkeypatch
):
    source_root = tmp_path / "source"
    system = source_root / "psx"
    system.mkdir(parents=True)
    playlist = system / "Collection.m3u"
    playlist.write_text("Disc A.chd\n", encoding="utf-8")
    (system / "Disc A.chd").write_bytes(b"A" * 9)
    (system / "Disc B.chd").write_bytes(b"B" * 9)
    game = Game.create(
        "psx", "Collection", "local", str(source_root),
        [GameAsset("Collection.m3u", "psx/Collection.m3u", playlist.stat().st_size, True)],
    )
    game_repo.save(game)
    cache, transfer, _staging, coordinator = _coordinated_cache(
        db, cache_repo, game_repo, cache_dir, data_dir, source_root, 1024
    )
    held: set[Path] = set()
    transferred: list[tuple[str, ...]] = []
    original_acquire = coordinator.assets.acquire
    original_transfer = transfer.transfer

    def acquire_then_mutate(paths, *, blocking):
        locked = {Path(path) for path in paths}
        held.clear()
        held.update(locked)
        playlist.write_text("Disc B.chd\n", encoding="utf-8")
        underlying = original_acquire(paths, blocking=blocking)

        class TrackingLease:
            def release(self):
                held.clear()
                underlying.release()

        return TrackingLease()

    def record_transfer(resolved, *args, **kwargs):
        transferred.append(tuple(asset.relative_path for asset in resolved.assets))
        destinations = {
            cache_dir.joinpath(*Path(asset.relative_path).parts)
            for asset in resolved.assets
        }
        assert destinations <= held
        return original_transfer(resolved, *args, **kwargs)

    monkeypatch.setattr(coordinator.assets, "acquire", acquire_then_mutate)
    monkeypatch.setattr(transfer, "transfer", record_transfer)

    cache.cache_game(game.id)

    assert transferred == [("psx/Collection.m3u", "psx/Disc B.chd")]
    assert (cache_dir / "psx" / "Disc B.chd").read_bytes() == b"B" * 9
    assert not (cache_dir / "psx" / "Disc A.chd").exists()
    assert {member.relative_path for member in cache_repo.list_members(game.id)} == {
        "psx/Collection.m3u", "psx/Disc B.chd",
    }


def test_shared_m3u_dependency_locks_collide_by_resolved_physical_path(
    db, cache_repo, game_repo, cache_dir, data_dir, tmp_path
):
    source_root = tmp_path / "source"
    system = source_root / "psx"
    system.mkdir(parents=True)
    (system / "Shared.chd").write_bytes(b"shared")
    games = []
    for name in ("Collection A", "Collection B"):
        playlist = system / f"{name}.m3u"
        playlist.write_text("Shared.chd\n", encoding="utf-8")
        game = Game.create(
            "psx", name, "local", str(source_root),
            [GameAsset(playlist.name, f"psx/{playlist.name}", playlist.stat().st_size, True)],
        )
        game_repo.save(game)
        games.append(game)
    cache, _transfer, _staging, _coordinator = _coordinated_cache(
        db, cache_repo, game_repo, cache_dir, data_dir, source_root, 1024
    )

    first = cache.try_asset_locks(games[0].id)
    assert first is not None
    try:
        assert cache.try_asset_locks(games[1].id) is None
    finally:
        first.release()
    second = cache.try_asset_locks(games[1].id)
    assert second is not None
    second.release()


class _RecordingFile(AbstractContextManager):
    def __init__(self, path: str, seeks: list[int]) -> None:
        self._file = Path(path).open("rb")
        self._seeks = seeks

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self._file.close()

    def read(self, size=-1):
        return self._file.read(size)

    def seek(self, offset, whence=0):
        self._seeks.append(offset)
        return self._file.seek(offset, whence)


class _RecordingProvider(LocalFilesystemProvider):
    def __init__(self) -> None:
        super().__init__()
        self.seeks: list[int] = []

    def open_binary(self, path: str):
        return _RecordingFile(path, self.seeks)


def test_exact_checkpoint_resume_and_changed_source_restart(db, game_repo, cache_dir, tmp_path):
    source_root = tmp_path / "source"
    game = _game(game_repo, source_root, size=3 * 1024 * 1024)
    provider = _RecordingProvider()
    staging = StagingRepository(db)
    service = TransferService(
        provider, str(cache_dir), source_root=str(source_root), staging_repository=staging
    )
    token = TransferCancellationToken()
    with pytest.raises(TransferCancelledError):
        service.transfer(
            game,
            on_progress=lambda done, _total: token.cancel() if done >= 1024 * 1024 else None,
            cancellation=token,
        )
    part = cache_dir / ".partial" / "ps2" / "Game.iso.part"
    assert part.stat().st_size == 1024 * 1024
    row = staging.get_file("ps2/Game.iso", "")
    assert row is not None and row.checkpoint_bytes == part.stat().st_size
    assert row.checkpoint_sha256 == hashlib.sha256(part.read_bytes()).hexdigest()
    # Bytes written after the last durable checkpoint are never trusted. The
    # retry truncates this tail before verifying and appending at the checkpoint.
    with part.open("ab") as handle:
        handle.write(b"uncommitted tail")

    final = Path(service.transfer(game))
    assert 1024 * 1024 in provider.seeks
    assert final.read_bytes() == (b"A" * (3 * 1024 * 1024))

    final.unlink()
    token = TransferCancellationToken()
    with pytest.raises(TransferCancelledError):
        service.transfer(game, lambda *_: token.cancel(), token)
    source = source_root / "ps2" / "Game.iso"
    source.write_bytes(b"B" * source.stat().st_size)
    final = Path(service.transfer(game))
    assert final.read_bytes() == source.read_bytes()


class _WorkerCache:
    def __init__(self) -> None:
        self.completed: list[str] = []

    def try_asset_locks(self, _game_id):
        from romcloud.infrastructure.cache_coordination import FileLockLease
        return FileLockLease([])

    def retained_staging_size(self, _game_id):
        return 0

    def cache_game(self, game_id, on_progress=None, **_kwargs):
        on_progress(1, 1)
        self.completed.append(game_id)
        return "/cache/game"

    def total_staging_size(self):
        return 0

    def discard_staging_asset(self, _system, _relative_path):
        return None


def test_serial_worker_fifo_and_restart_state_preservation(db, game_repo, cache_dir, tmp_path):
    first = _game(game_repo, tmp_path / "source", "A.iso")
    second = _game(game_repo, tmp_path / "source", "B.iso")
    repo = DownloadRepository(db)
    cache = _WorkerCache()
    manager = DownloadManagerService(
        repository=repo, staging_repository=StagingRepository(db), game_repo=game_repo,
        cache=cache, cache_root=str(cache_dir),
    )
    manager.enqueue([first.id, second.id])
    manager.start()
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline and len(cache.completed) < 2:
        time.sleep(0.01)
    manager.shutdown()
    assert cache.completed == [first.id, second.id]
    assert [item.state for item in repo.list()] == [DownloadState.COMPLETE, DownloadState.COMPLETE]


def test_closure_resolution_failure_fails_item_without_killing_worker(
    db, game_repo, cache_dir, tmp_path
):
    first = _game(game_repo, tmp_path / "source", "A.iso")
    second = _game(game_repo, tmp_path / "source", "B.iso")

    class LockFailCache(_WorkerCache):
        def try_asset_locks(self, game_id):
            if game_id == first.id:
                raise OSError("descriptor changed repeatedly")
            return super().try_asset_locks(game_id)

    repo = DownloadRepository(db)
    cache = LockFailCache()
    manager = DownloadManagerService(
        repository=repo, staging_repository=StagingRepository(db),
        game_repo=game_repo, cache=cache, cache_root=str(cache_dir),
    )
    first_id = manager.enqueue([first.id])["items"][0]["id"]
    second_id = manager.enqueue([second.id])["items"][0]["id"]
    manager.start()
    _wait_state(repo, first_id, DownloadState.FAILED)
    _wait_state(repo, second_id, DownloadState.COMPLETE)
    manager.shutdown()

    assert cache.completed == [second.id]


class _ControlledCache(_WorkerCache):
    def __init__(self, *, fail_first: bool = False) -> None:
        super().__init__()
        self.calls = 0
        self.fail_first = fail_first
        self.started = False
        self.release = False
        self.partial = 0

    def retained_staging_size(self, _game_id):
        return self.partial

    def cache_game(self, game_id, on_progress=None, cancellation=None, **_kwargs):
        self.calls += 1
        self.started = True
        if self.fail_first and self.calls == 1:
            self.partial = 4
            on_progress(4, 10)
            raise OSError("temporary source failure")
        while not self.release:
            if cancellation is not None:
                cancellation.raise_if_cancelled()
            self.partial = 4
            on_progress(4, 10)
            time.sleep(0.005)
        on_progress(10, 10)
        self.completed.append(game_id)
        return "/cache/game"


def _wait_state(repo: DownloadRepository, item_id: str, state: DownloadState) -> None:
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        item = repo.get(item_id)
        if item is not None and item.state is state:
            return
        time.sleep(0.01)
    raise AssertionError(f"download did not reach {state.value}: {repo.get(item_id)}")


def test_pause_resume_cancel_and_cancelled_resume(db, game_repo, cache_dir, tmp_path):
    game = _game(game_repo, tmp_path / "source")
    repo = DownloadRepository(db)
    cache = _ControlledCache()
    manager = DownloadManagerService(
        repository=repo, staging_repository=StagingRepository(db), game_repo=game_repo,
        cache=cache, cache_root=str(cache_dir),
    )
    item_id = manager.enqueue([game.id])["items"][0]["id"]
    manager.start()
    _wait_state(repo, item_id, DownloadState.RUNNING)
    manager.pause(item_id)
    _wait_state(repo, item_id, DownloadState.PAUSED)
    assert repo.get(item_id).bytes_present == 4

    manager.resume(item_id)
    _wait_state(repo, item_id, DownloadState.RUNNING)
    manager.cancel(item_id)
    _wait_state(repo, item_id, DownloadState.CANCELLED)
    assert repo.get(item_id).bytes_present == 4

    manager.resume(item_id)
    cache.release = True
    _wait_state(repo, item_id, DownloadState.COMPLETE)
    manager.shutdown()


def test_retry_failed_remove_queued_and_bulk_controls(db, game_repo, cache_dir, tmp_path):
    first = _game(game_repo, tmp_path / "source", "A.iso")
    second = _game(game_repo, tmp_path / "source", "B.iso")
    repo = DownloadRepository(db)
    cache = _ControlledCache(fail_first=True)
    manager = DownloadManagerService(
        repository=repo, staging_repository=StagingRepository(db), game_repo=game_repo,
        cache=cache, cache_root=str(cache_dir),
    )
    first_id = manager.enqueue([first.id])["items"][0]["id"]
    manager.start()
    _wait_state(repo, first_id, DownloadState.FAILED)
    assert manager.retry_all_failed() == 1
    cache.release = True
    _wait_state(repo, first_id, DownloadState.COMPLETE)
    manager.shutdown()

    second_id = manager.enqueue([second.id])["items"][0]["id"]
    manager.remove_queued(second_id)
    assert repo.get(second_id) is None
    third_id = manager.enqueue([second.id])["items"][0]["id"]
    assert manager.cancel_all() == 1
    assert repo.get(third_id).state is DownloadState.CANCELLED


def test_startup_preserves_queued_and_paused_but_interrupts_inflight(
    db, game_repo, cache_dir, tmp_path
):
    games = [_game(game_repo, tmp_path / "source", f"{name}.iso") for name in "ABCD"]
    repo = DownloadRepository(db)
    items = [
        repo.enqueue(game_id=g.id, game_title=g.title, system=g.system, origin=DownloadOrigin.MANUAL)[0]
        for g in games
    ]
    repo.transition(items[1].id, from_states=[DownloadState.QUEUED], to_state=DownloadState.RUNNING)
    repo.transition(items[1].id, from_states=[DownloadState.RUNNING], to_state=DownloadState.PAUSED)
    repo.transition(items[2].id, from_states=[DownloadState.QUEUED], to_state=DownloadState.RUNNING)
    repo.transition(items[3].id, from_states=[DownloadState.QUEUED], to_state=DownloadState.VERIFYING)
    DownloadManagerService(
        repository=repo, staging_repository=StagingRepository(db), game_repo=game_repo,
        cache=_WorkerCache(), cache_root=str(cache_dir),
    )
    assert repo.get(items[0].id).state is DownloadState.QUEUED
    assert repo.get(items[1].id).state is DownloadState.PAUSED
    assert repo.get(items[2].id).state is DownloadState.INTERRUPTED
    assert repo.get(items[3].id).state is DownloadState.INTERRUPTED


class _GcCache(_WorkerCache):
    def __init__(self, staging: StagingRepository, *, locked: bool = False) -> None:
        super().__init__()
        self._staging = staging
        self.locked = locked
        self.discarded: list[str] = []

    def discard_staging_asset(self, _system, relative_path):
        if self.locked:
            raise RuntimeError("asset locked")
        self.discarded.append(relative_path)
        self._staging.delete_asset(relative_path)


def _stage_owned_asset(
    db, cache_repo, staging: StagingRepository, games: list[Game], relative_path: str
) -> None:
    from romcloud.core.models.cache import CacheStatus

    for game in games:
        entry = CacheEntry.create(game.id, f"/cache/{relative_path}")
        entry.status = CacheStatus.INCOMPLETE
        cache_repo.save(entry)
        cache_repo.replace_membership(
            game.id,
            [GameAsset(Path(relative_path).name, relative_path, 5, game is games[0])],
            {},
        )
    staging.replace_plan(
        relative_path=relative_path, system=games[0].system, asset_kind="file",
        source_provider="local", source_root="/source", expected_size=5,
        source_manifest_sha256="manifest", files=[{
            "member_relative_path": "", "expected_size": 5,
            "source_object_id": None, "source_revision": None,
            "source_checksum": None, "source_modified_epoch": None,
        }],
    )
    old = (datetime.now(timezone.utc) - timedelta(days=31)).isoformat()
    with db.connect() as conn:
        conn.execute(
            "UPDATE cache_staging_assets SET updated_at=? WHERE relative_path=?",
            (old, relative_path),
        )


def _age_downloads(db, *item_ids: str) -> None:
    old = (datetime.now(timezone.utc) - timedelta(days=31)).isoformat()
    with db.connect() as conn:
        conn.executemany(
            "UPDATE download_items SET updated_at=? WHERE id=?",
            [(old, item_id) for item_id in item_ids],
        )


def test_stale_gc_preserves_asset_for_newer_same_game_intent(
    db, game_repo, cache_repo, cache_dir, tmp_path
):
    game = _game(game_repo, tmp_path / "source")
    repo = DownloadRepository(db)
    old, _ = repo.enqueue(
        game_id=game.id, game_title=game.title, system=game.system,
        origin=DownloadOrigin.MANUAL,
    )
    repo.transition(old.id, from_states=[DownloadState.QUEUED], to_state=DownloadState.CANCELLED)
    newer, _ = repo.enqueue(
        game_id=game.id, game_title=game.title, system=game.system,
        origin=DownloadOrigin.MANUAL,
    )
    staging = StagingRepository(db)
    _stage_owned_asset(db, cache_repo, staging, [game], "ps2/Game.iso")
    _age_downloads(db, old.id)
    cache = _GcCache(staging)

    DownloadManagerService(
        repository=repo, staging_repository=staging, game_repo=game_repo,
        cache=cache, cache_root=str(cache_dir),
    )

    assert repo.get(newer.id).state is DownloadState.QUEUED
    assert cache.discarded == []


def test_stale_gc_uses_protected_shared_intent_not_owner_count(
    db, game_repo, cache_repo, cache_dir, tmp_path
):
    first = _game(game_repo, tmp_path / "source", "A.iso")
    second = _game(game_repo, tmp_path / "source", "B.iso")
    repo = DownloadRepository(db)
    stale_ids: list[str] = []
    for game in (first, second):
        item, _ = repo.enqueue(
            game_id=game.id, game_title=game.title, system=game.system,
            origin=DownloadOrigin.MANUAL,
        )
        repo.transition(
            item.id, from_states=[DownloadState.QUEUED], to_state=DownloadState.CANCELLED
        )
        stale_ids.append(item.id)
    staging = StagingRepository(db)
    _stage_owned_asset(db, cache_repo, staging, [first, second], "ps2/Shared.chd")
    _age_downloads(db, *stale_ids)
    cache = _GcCache(staging)

    DownloadManagerService(
        repository=repo, staging_repository=staging, game_repo=game_repo,
        cache=cache, cache_root=str(cache_dir),
    )

    assert cache.discarded == ["ps2/Shared.chd"]


def test_stale_gc_preserves_shared_asset_for_paused_owner_indefinitely(
    db, game_repo, cache_repo, cache_dir, tmp_path
):
    stale_game = _game(game_repo, tmp_path / "source", "A.iso")
    paused_game = _game(game_repo, tmp_path / "source", "B.iso")
    repo = DownloadRepository(db)
    stale, _ = repo.enqueue(
        game_id=stale_game.id, game_title=stale_game.title, system=stale_game.system,
        origin=DownloadOrigin.MANUAL,
    )
    repo.transition(
        stale.id, from_states=[DownloadState.QUEUED], to_state=DownloadState.CANCELLED
    )
    paused, _ = repo.enqueue(
        game_id=paused_game.id, game_title=paused_game.title, system=paused_game.system,
        origin=DownloadOrigin.MANUAL,
    )
    repo.transition(
        paused.id, from_states=[DownloadState.QUEUED], to_state=DownloadState.RUNNING
    )
    repo.transition(
        paused.id, from_states=[DownloadState.RUNNING], to_state=DownloadState.PAUSED
    )
    staging = StagingRepository(db)
    _stage_owned_asset(
        db, cache_repo, staging, [stale_game, paused_game], "ps2/Shared.chd"
    )
    _age_downloads(db, stale.id, paused.id)
    cache = _GcCache(staging)

    DownloadManagerService(
        repository=repo, staging_repository=staging, game_repo=game_repo,
        cache=cache, cache_root=str(cache_dir),
    )

    assert cache.discarded == []


def test_stale_gc_skips_asset_held_by_live_writer_lock(
    db, game_repo, cache_repo, cache_dir, tmp_path
):
    game = _game(game_repo, tmp_path / "source")
    repo = DownloadRepository(db)
    item, _ = repo.enqueue(
        game_id=game.id, game_title=game.title, system=game.system,
        origin=DownloadOrigin.MANUAL,
    )
    repo.transition(
        item.id, from_states=[DownloadState.QUEUED], to_state=DownloadState.CANCELLED
    )
    staging = StagingRepository(db)
    _stage_owned_asset(db, cache_repo, staging, [game], "ps2/Game.iso")
    _age_downloads(db, item.id)
    cache = _GcCache(staging, locked=True)

    manager = DownloadManagerService(
        repository=repo, staging_repository=staging, game_repo=game_repo,
        cache=cache, cache_root=str(cache_dir),
    )

    assert manager.cleanup_stale_partials() == 0
    assert staging.get_asset("ps2/Game.iso") is not None
