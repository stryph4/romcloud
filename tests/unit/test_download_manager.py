"""Focused durability, resume, locking, and queue-state coverage."""

from __future__ import annotations

import hashlib
import sqlite3
import time
from contextlib import AbstractContextManager
from datetime import datetime, timezone
from pathlib import Path

import pytest

from romcloud.core.cancellation import TransferCancellationToken
from romcloud.core.exceptions import InsufficientSpaceError, TransferCancelledError
from romcloud.core.models.download import DownloadOrigin, DownloadState
from romcloud.core.models.game import Game, GameAsset
from romcloud.infrastructure.cache_coordination import (
    AssetLockManager,
    CacheStorageCoordinator,
    LockUnavailable,
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

    def prepare_download_membership(self, _game_id):
        return None

    def try_asset_locks(self, _game_id):
        from romcloud.infrastructure.cache_coordination import FileLockLease
        return FileLockLease([])

    def retained_staging_size(self, _game_id):
        return 0

    def cache_game(self, game_id, on_progress=None, **_kwargs):
        on_progress(1, 1)
        self.completed.append(game_id)
        return "/cache/game"

    def discard_staging(self, _game_id):
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
