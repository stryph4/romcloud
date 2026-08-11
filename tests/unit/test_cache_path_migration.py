"""Regression coverage for persisted legacy absolute cache paths."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from romcloud.bootstrap.container import Container
from romcloud.core.models.cache import CacheEntry, CacheStatus
from romcloud.core.models.game import Game, GameAsset
from romcloud.core.models.proxy import ProxyRecord
from romcloud.infrastructure.config import AppConfig, CacheConfig, SourceConfig
from romcloud.infrastructure.database import Database
from romcloud.infrastructure.repositories.cache import CacheRepository
from romcloud.infrastructure.repositories.game import GameRepository
from romcloud.integrations.batocera.game_access import reconcile_library_presentation


LEGACY_ROOT = Path("/userdata/romcloud-cache")
GAME_NAME = "Aggressive Inline (USA).chd"


def _game(
    game_repo: GameRepository,
    source_root: Path,
    *,
    filename: str = GAME_NAME,
) -> Game:
    asset = GameAsset(
        filename=filename,
        relative_path=f"ps2/{filename}",
        size_bytes=13,
        is_primary=True,
    )
    game = Game.create(
        "ps2", Path(filename).stem, "local", str(source_root), [asset]
    )
    game_repo.save(game)
    return game


def _entry(game_id: str, path: Path, *, pinned: bool = True) -> CacheEntry:
    cached_at = datetime(2026, 8, 1, 12, 30, tzinfo=timezone.utc)
    return CacheEntry(
        game_id=game_id,
        cache_path=str(path),
        status=CacheStatus.COMPLETE,
        cached_at=cached_at,
        last_accessed=cached_at + timedelta(days=2),
        size_bytes=13,
        is_pinned=pinned,
    )


def test_rebases_existing_legacy_path_and_preserves_all_entry_state(tmp_path: Path) -> None:
    db = Database(str(tmp_path / "catalog.db"))
    db.initialize()
    games = GameRepository(db)
    caches = CacheRepository(db)
    game = _game(games, tmp_path / "source")
    original = _entry(game.id, LEGACY_ROOT / "ps2" / GAME_NAME)
    caches.save(original)
    configured_root = tmp_path / "configured-cache"
    destination = configured_root / "ps2" / GAME_NAME
    destination.parent.mkdir(parents=True)
    destination.write_bytes(b"cached bytes!")

    first = caches.reconcile_legacy_cache_paths(configured_root)
    migrated = caches.get(game.id)
    second = caches.reconcile_legacy_cache_paths(configured_root)

    assert first.migrated == 1 and first.missing == 0
    assert second.migrated == second.missing == 0
    assert migrated is not None
    assert migrated.cache_path == str(destination)
    assert migrated.status == original.status
    assert migrated.size_bytes == original.size_bytes
    assert migrated.cached_at == original.cached_at
    assert migrated.last_accessed == original.last_accessed
    assert migrated.is_pinned == original.is_pinned
    assert destination.read_bytes() == b"cached bytes!"


def test_correct_path_is_unchanged_and_missing_rebased_path_is_not_promoted(tmp_path: Path) -> None:
    db = Database(str(tmp_path / "catalog.db"))
    db.initialize()
    games = GameRepository(db)
    caches = CacheRepository(db)
    configured_root = tmp_path / "custom-cache"

    correct_game = _game(games, tmp_path / "source-a")
    correct_path = configured_root / "ps2" / GAME_NAME
    correct_path.parent.mkdir(parents=True)
    correct_path.write_bytes(b"cached bytes!")
    caches.save(_entry(correct_game.id, correct_path))

    missing_name = "Missing Game.chd"
    missing_game = _game(games, tmp_path / "source-b", filename=missing_name)
    missing_legacy = LEGACY_ROOT / "ps2" / missing_name
    caches.save(_entry(missing_game.id, missing_legacy, pinned=False))

    result = caches.reconcile_legacy_cache_paths(configured_root)

    assert result.migrated == 0 and result.missing == 1
    assert caches.get(correct_game.id).cache_path == str(correct_path)
    assert caches.get(missing_game.id).cache_path == str(missing_legacy)


def test_offline_presentation_recognizes_migrated_game_as_playable(tmp_path: Path) -> None:
    configured_root = tmp_path / "chosen-cache-root"
    local_roms = tmp_path / "roms"
    source_root = tmp_path / "source"
    data_root = tmp_path / "data"
    local_roms.mkdir()
    source_root.mkdir()
    config = AppConfig(
        source=SourceConfig("local", str(source_root)),
        cache=CacheConfig(str(configured_root)),
        local_roms_path=str(local_roms),
        data_path=str(data_root),
    )
    container = Container(config)
    game = _game(container.game_repo, source_root)
    destination = configured_root / "ps2" / GAME_NAME
    destination.parent.mkdir(parents=True)
    destination.write_bytes(b"cached bytes!")
    container.cache_repo.save(_entry(game.id, LEGACY_ROOT / "ps2" / GAME_NAME))
    proxy_path = local_roms / "ps2" / "Aggressive Inline (USA).romcloud"
    container.proxy_repo.save(ProxyRecord.create(game.id, str(proxy_path)))

    report = reconcile_library_presentation(config, offline=True)

    assert report.visible == 1
    assert proxy_path.is_file()
    assert Container(config).cache.get_launch_path(game.id) == str(destination)
    assert Container(config).cache_repo.get(game.id).cache_path == str(destination)
