from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from romcloud.core.models.cache import CachePolicy, CacheStatus
from romcloud.core.models.game import Game, GameAsset
from romcloud.infrastructure.database import Database
from romcloud.infrastructure.repositories.cache import CacheRepository
from romcloud.services.cache import CacheService


def _playlist_game(root: Path, game_repo, title: str) -> Game:
    system = root / "psx"
    system.mkdir(parents=True, exist_ok=True)
    marker = system / f"{title}.m3u"
    marker.write_text("Shared.chd\n")
    shared = system / "Shared.chd"
    if not shared.exists():
        shared.write_bytes(b"shared-disc-bytes")
    game = Game.create(
        "psx",
        title,
        "local",
        str(root),
        [
            GameAsset(
                marker.name,
                f"psx/{marker.name}",
                size_bytes=marker.stat().st_size,
                is_primary=True,
            )
        ],
    )
    game_repo.save(game)
    return game


def test_shared_dependency_has_persistent_multi_owner_lifecycle(
    cache_service, cache_repo, game_repo, tmp_path: Path
) -> None:
    source = tmp_path / "source"
    game_a = _playlist_game(source, game_repo, "Game A")
    game_b = _playlist_game(source, game_repo, "Game B")

    launch_a = Path(cache_service.cache_game(game_a.id))
    launch_b = Path(cache_service.cache_game(game_b.id))
    shared = Path(cache_service._cache_root) / "psx" / "Shared.chd"
    expected_physical = launch_a.stat().st_size + launch_b.stat().st_size + shared.stat().st_size

    assert cache_repo.owner_count("psx/Shared.chd") == 2
    assert cache_repo.total_size() == expected_physical
    assert launch_a.name == "Game A.m3u" and launch_b.name == "Game B.m3u"

    cache_service.remove(game_a.id)

    assert not launch_a.exists()
    assert shared.is_file()
    assert cache_service.is_cached(game_b.id)
    assert cache_repo.owner_count("psx/Shared.chd") == 1

    cache_service.remove(game_b.id)

    assert not launch_b.exists()
    assert not shared.exists()
    assert cache_repo.owner_count("psx/Shared.chd") == 0


def test_pinned_owner_protects_shared_dependency_during_eviction(
    cache_service, cache_repo, game_repo, tmp_path: Path
) -> None:
    source = tmp_path / "source"
    pinned = _playlist_game(source, game_repo, "Pinned")
    evictable = _playlist_game(source, game_repo, "Evictable")
    cache_service.cache_game(pinned.id)
    evictable_launch = Path(cache_service.cache_game(evictable.id))
    cache_service.pin(pinned.id)
    shared = Path(cache_service._cache_root) / "psx" / "Shared.chd"
    cache_service._policy = CachePolicy(max_size_bytes=0, min_free_bytes=0)

    evicted = cache_service.evict()

    assert evicted == [evictable.id]
    assert not evictable_launch.exists()
    assert shared.is_file()
    assert cache_service.is_cached(pinned.id)
    assert cache_repo.owner_count("psx/Shared.chd") == 1


def test_persisted_membership_survives_service_restart_and_descriptor_change(
    cache_service, cache_repo, game_repo, transfer_service, policy, cache_dir,
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    game = _playlist_game(source, game_repo, "Restarted")
    launch = Path(cache_service.cache_game(game.id))
    (source / "psx" / "Restarted.m3u").write_text("Now Missing.chd\n")

    restarted = CacheService(
        cache_repo=cache_repo,
        game_repo=game_repo,
        transfer_service=transfer_service,
        cache_root=str(cache_dir),
        policy=policy,
    )

    assert restarted.is_cached(game.id)
    assert restarted.get_launch_path(game.id) == str(launch)
    assert {
        member.relative_path for member in cache_repo.list_members(game.id)
    } == {"psx/Restarted.m3u", "psx/Shared.chd"}


def test_remove_uses_persisted_membership_not_changed_source_descriptor(
    cache_service, cache_repo, game_repo, tmp_path: Path
) -> None:
    source = tmp_path / "source"
    game = _playlist_game(source, game_repo, "Changed")
    launch = Path(cache_service.cache_game(game.id))
    shared = Path(cache_service._cache_root) / "psx" / "Shared.chd"
    unrelated = source / "psx" / "Unrelated.chd"
    unrelated.write_bytes(b"source-only")
    (source / "psx" / "Changed.m3u").write_text("Unrelated.chd\n")

    cache_service.remove(game.id)

    assert not launch.exists()
    assert not shared.exists()
    assert unrelated.read_bytes() == b"source-only"
    assert cache_repo.list_members(game.id) == []


def test_multidisc_playlist_preserves_relative_layout_and_primary_launch(
    cache_service, cache_repo, game_repo, tmp_path: Path
) -> None:
    source = tmp_path / "source"
    playlist_dir = source / "psx" / "sets"
    discs = playlist_dir / "discs"
    discs.mkdir(parents=True)
    playlist = playlist_dir / "Collection.m3u"
    playlist.write_text("discs/Disc 1.chd\ndiscs/Disc 2.chd\n")
    (discs / "Disc 1.chd").write_bytes(b"one")
    (discs / "Disc 2.chd").write_bytes(b"two")
    game = Game.create(
        "psx", "Collection", "local", str(source),
        [GameAsset("Collection.m3u", "psx/sets/Collection.m3u", playlist.stat().st_size, True)],
    )
    game_repo.save(game)

    launch = Path(cache_service.cache_game(game.id))

    assert launch == Path(cache_service._cache_root) / "psx" / "sets" / "Collection.m3u"
    assert launch.read_text().splitlines() == [
        "discs/Disc 1.chd", "discs/Disc 2.chd"
    ]
    assert (launch.parent / "discs" / "Disc 1.chd").read_bytes() == b"one"
    assert (launch.parent / "discs" / "Disc 2.chd").read_bytes() == b"two"
    assert next(
        member for member in cache_repo.list_members(game.id) if member.is_primary
    ).relative_path == "psx/sets/Collection.m3u"


def test_xbox360_marker_caches_payload_and_nested_iso_is_unchanged(
    cache_service, cache_repo, game_repo, tmp_path: Path
) -> None:
    source = tmp_path / "source"
    xbla = source / "xbox360" / "xbla"
    games = source / "xbox360" / "games"
    xbla.mkdir(parents=True)
    games.mkdir(parents=True)
    marker = xbla / "Castle Crashers.xbox360"
    marker.write_text("Castle Crashers\n")
    payload = xbla / "Castle Crashers" / "584108B7" / "000D0000"
    payload.mkdir(parents=True)
    (payload / "content-package").write_bytes(b"payload")
    iso = games / "Halo 3.iso"
    iso.write_bytes(b"iso")
    marker_game = Game.create(
        "xbox360", "Castle Crashers", "local", str(source),
        [GameAsset(marker.name, "xbox360/xbla/Castle Crashers.xbox360", marker.stat().st_size, True)],
    )
    iso_game = Game.create(
        "xbox360", "Halo 3", "local", str(source),
        [GameAsset(iso.name, "xbox360/games/Halo 3.iso", iso.stat().st_size, True)],
    )
    game_repo.save(marker_game)
    game_repo.save(iso_game)

    marker_launch = Path(cache_service.cache_game(marker_game.id))
    iso_launch = Path(cache_service.cache_game(iso_game.id))

    assert marker_launch.name == "Castle Crashers.xbox360"
    assert (
        marker_launch.parent
        / "Castle Crashers"
        / "584108B7"
        / "000D0000"
        / "content-package"
    ).read_bytes() == b"payload"
    assert iso_launch.read_bytes() == b"iso"
    assert [
        member.relative_path for member in cache_repo.list_members(iso_game.id)
    ] == ["xbox360/games/Halo 3.iso"]


def _create_v2_cache(
    path: Path,
    *,
    game_id: str,
    filename: str,
    relative_path: str,
    cache_path: Path,
    size: int,
) -> tuple[datetime, datetime]:
    cached_at = datetime(2025, 1, 2, tzinfo=timezone.utc)
    accessed = cached_at + timedelta(days=3)
    with sqlite3.connect(path) as conn:
        conn.executescript(
            """
            CREATE TABLE schema_version (version INTEGER NOT NULL);
            INSERT INTO schema_version VALUES (2);
            CREATE TABLE games (
                id TEXT PRIMARY KEY, system TEXT NOT NULL, title TEXT NOT NULL,
                source_provider TEXT NOT NULL, source_root TEXT NOT NULL,
                last_played TEXT, added_at TEXT NOT NULL,
                is_eligible INTEGER NOT NULL DEFAULT 1
            );
            CREATE TABLE game_assets (
                id TEXT PRIMARY KEY, game_id TEXT NOT NULL,
                relative_path TEXT NOT NULL, filename TEXT NOT NULL,
                size_bytes INTEGER, is_primary INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE cache_entries (
                game_id TEXT PRIMARY KEY, cache_path TEXT NOT NULL,
                status TEXT NOT NULL, cached_at TEXT NOT NULL,
                last_accessed TEXT NOT NULL, size_bytes INTEGER NOT NULL DEFAULT 0,
                is_pinned INTEGER NOT NULL DEFAULT 0
            );
            """
        )
        conn.execute(
            "INSERT INTO games VALUES (?, 'psx', 'Legacy', 'local', '/source', NULL, ?, 1)",
            (game_id, cached_at.isoformat()),
        )
        conn.execute(
            "INSERT INTO game_assets VALUES ('asset', ?, ?, ?, ?, 1)",
            (game_id, relative_path, filename, size),
        )
        conn.execute(
            "INSERT INTO cache_entries VALUES (?, ?, 'complete', ?, ?, ?, 1)",
            (game_id, str(cache_path), cached_at.isoformat(), accessed.isoformat(), size),
        )
    return cached_at, accessed


def test_v2_single_file_cache_migrates_idempotently_and_preserves_state(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "single.db"
    cached_file = tmp_path / "cache" / "psx" / "Game.chd"
    cached_file.parent.mkdir(parents=True)
    cached_file.write_bytes(b"game")
    cached_at, accessed = _create_v2_cache(
        db_path,
        game_id="single",
        filename="Game.chd",
        relative_path="psx/Game.chd",
        cache_path=cached_file,
        size=4,
    )

    db = Database(str(db_path))
    db.initialize()
    db.initialize()
    repo = CacheRepository(db)
    entry = repo.get("single")

    assert entry is not None and entry.status is CacheStatus.COMPLETE
    assert entry.is_pinned and entry.cached_at == cached_at
    assert entry.last_accessed == accessed
    assert repo.membership_resolved("single")
    assert [member.relative_path for member in repo.list_members("single")] == [
        "psx/Game.chd"
    ]
    assert cached_file.read_bytes() == b"game"


def test_v2_descriptor_only_cache_becomes_incomplete_without_losing_bytes(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "descriptor.db"
    marker = tmp_path / "cache" / "psx" / "Game.m3u"
    marker.parent.mkdir(parents=True)
    marker.write_text("Disc 1.chd\n")
    cached_at, accessed = _create_v2_cache(
        db_path,
        game_id="descriptor",
        filename="Game.m3u",
        relative_path="psx/Game.m3u",
        cache_path=marker,
        size=marker.stat().st_size,
    )

    db = Database(str(db_path))
    db.initialize()
    repo = CacheRepository(db)
    entry = repo.get("descriptor")

    assert entry is not None and entry.status is CacheStatus.INCOMPLETE
    assert entry.is_pinned and entry.cached_at == cached_at
    assert entry.last_accessed == accessed
    assert not repo.membership_resolved("descriptor")
    assert marker.read_text().strip() == "Disc 1.chd"
