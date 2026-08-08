"""Integration test — full local filesystem vertical slice.

This test exercises the complete path from:
    LocalFilesystemProvider
    → scan Batocera-style ROM root
    → catalog games in SQLite
    → write .romcloud proxy files
    → resolve a proxy
    → cache the game
    → verify cached path and content
    → verify cached game launches path correctly

No mocks.  Everything runs against real files in tmp_path.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from romcloud.infrastructure.database import Database
from romcloud.infrastructure.repositories.game import GameRepository
from romcloud.infrastructure.repositories.cache import CacheRepository
from romcloud.infrastructure.repositories.proxy import ProxyRepository
from romcloud.core.models.cache import CachePolicy
from romcloud.core.providers.local import LocalFilesystemProvider
from romcloud.core.services.transfer import TransferService
from romcloud.core.services.cache import CacheService
from romcloud.core.services.catalog import CatalogService


def test_local_vertical_slice(tmp_path: Path) -> None:
    """
    Full end-to-end local filesystem test.

    The scenario mirrors a real user setup:
    - ROM source: a mounted filesystem with Batocera folder structure
    - Local ROM dir: /userdata/roms (proxy files placed here)
    - Cache: /userdata/romcloud-cache (ROM copies placed here)
    - SQLite catalog: /userdata/system/romcloud/data/catalog.db
    """

    # ── fixtures ──────────────────────────────────────────────────────────────

    source_root = tmp_path / "source_roms"
    (source_root / "ps2").mkdir(parents=True)
    (source_root / "nes").mkdir(parents=True)

    ps2_rom = source_root / "ps2" / "Final Fantasy X.iso"
    ps2_rom.write_bytes(b"ps2_rom_content" * 100)

    nes_rom = source_root / "nes" / "Super Mario Bros.nes"
    nes_rom.write_bytes(b"nes_data" * 50)

    local_roms = tmp_path / "local_roms"
    local_roms.mkdir()

    cache_root = tmp_path / "romcloud_cache"
    cache_root.mkdir()

    data_dir = tmp_path / "data"
    data_dir.mkdir()

    # ── wire dependencies ─────────────────────────────────────────────────────

    db = Database(str(data_dir / "catalog.db"))
    db.initialize()

    game_repo = GameRepository(db)
    cache_repo = CacheRepository(db)
    proxy_repo = ProxyRepository(db)

    provider = LocalFilesystemProvider()
    transfer_svc = TransferService(provider=provider, cache_root=str(cache_root))
    policy = CachePolicy.from_gb(max_size_gb=10.0, min_free_gb=0.001)
    cache_svc = CacheService(
        cache_repo=cache_repo,
        game_repo=game_repo,
        transfer_service=transfer_svc,
        cache_root=str(cache_root),
        policy=policy,
    )
    catalog_svc = CatalogService(
        provider=provider,
        game_repo=game_repo,
        proxy_repo=proxy_repo,
        local_roms_root=str(local_roms),
        source_root=str(source_root),
    )

    # ── Step 1: refresh catalog ───────────────────────────────────────────────

    result = catalog_svc.refresh()
    assert result.added == 2, f"Expected 2 new games, got {result.added}. Errors: {result.errors}"
    assert result.errors == []
    assert game_repo.count() == 2

    # ── Step 2: verify proxy files ────────────────────────────────────────────

    proxy_files = list(local_roms.rglob("*.romcloud"))
    assert len(proxy_files) == 2

    ps2_proxies = [f for f in proxy_files if f.parent.name == "ps2"]
    nes_proxies = [f for f in proxy_files if f.parent.name == "nes"]
    assert len(ps2_proxies) == 1
    assert len(nes_proxies) == 1

    # Validate proxy content
    ps2_proxy_path = ps2_proxies[0]
    payload = json.loads(ps2_proxy_path.read_text())
    assert payload["system"] == "ps2"
    assert payload["title"] == "Final Fantasy X"
    assert payload["source_provider"] == "local"
    assert len(payload["assets"]) == 1
    assert payload["assets"][0]["is_primary"] is True

    # ── Step 3: resolve proxy → game ─────────────────────────────────────────

    game = catalog_svc.resolve_proxy(str(ps2_proxy_path))
    assert game.title == "Final Fantasy X"
    assert game.system == "ps2"
    assert game.primary_asset is not None
    assert game.primary_asset.filename == "Final Fantasy X.iso"

    # ── Step 4: game is not yet cached ────────────────────────────────────────

    assert not cache_svc.is_cached(game.id)
    assert cache_svc.get_launch_path(game.id) is None

    # ── Step 5: cache the game ────────────────────────────────────────────────

    progress_calls = []
    launch_path = cache_svc.cache_game(
        game.id,
        on_progress=lambda done, total: progress_calls.append((done, total)),
    )

    assert launch_path is not None
    assert len(progress_calls) > 0

    # ── Step 6: verify cached ─────────────────────────────────────────────────

    assert cache_svc.is_cached(game.id)
    assert cache_svc.get_launch_path(game.id) == launch_path

    # Cached file exists and has correct content
    cached_file = Path(launch_path)
    assert cached_file.exists()
    assert cached_file.read_bytes() == ps2_rom.read_bytes()

    # ── Step 7: cache path structure ─────────────────────────────────────────

    # Must be directly under cache_root/ps2/, preserving the source basename.
    assert cached_file.parent == cache_root / "ps2"
    assert cached_file.name == "Final Fantasy X.iso"

    # Must NOT be inside the local roms directory
    assert not str(cached_file).startswith(str(local_roms))

    # ── Step 8: second refresh is idempotent ─────────────────────────────────

    result2 = catalog_svc.refresh()
    assert result2.added == 0
    assert result2.skipped == 2

    # ── Step 9: pin/unpin/remove ─────────────────────────────────────────────

    cache_svc.pin(game.id)
    assert cache_repo.get(game.id).is_pinned is True

    cache_svc.unpin(game.id)
    assert cache_repo.get(game.id).is_pinned is False
    # File still there
    assert cached_file.exists()

    cache_svc.remove(game.id)
    assert not cache_svc.is_cached(game.id)
    assert not cached_file.exists()


def test_directory_game_vertical_slice(tmp_path: Path) -> None:
    """Verify that directory-based games (e.g. PS3) are handled correctly."""

    source_root = tmp_path / "source_roms"
    game_dir = source_root / "ps3" / "BCES00000"
    game_dir.mkdir(parents=True)
    (game_dir / "EBOOT.BIN").write_bytes(b"eboot_data" * 30)
    (game_dir / "data" / "archive.pkg").parent.mkdir()
    (game_dir / "data" / "archive.pkg").write_bytes(b"pkg_data" * 100)

    local_roms = tmp_path / "local_roms"
    local_roms.mkdir()
    cache_root = tmp_path / "cache"
    cache_root.mkdir()
    data_dir = tmp_path / "data"
    data_dir.mkdir()

    db = Database(str(data_dir / "catalog.db"))
    db.initialize()
    game_repo = GameRepository(db)
    cache_repo = CacheRepository(db)
    proxy_repo = ProxyRepository(db)

    provider = LocalFilesystemProvider()
    transfer_svc = TransferService(provider=provider, cache_root=str(cache_root))
    policy = CachePolicy.from_gb(10.0, 0.001)
    cache_svc = CacheService(
        cache_repo=cache_repo,
        game_repo=game_repo,
        transfer_service=transfer_svc,
        cache_root=str(cache_root),
        policy=policy,
    )
    catalog_svc = CatalogService(
        provider=provider,
        game_repo=game_repo,
        proxy_repo=proxy_repo,
        local_roms_root=str(local_roms),
        source_root=str(source_root),
    )

    result = catalog_svc.refresh()
    assert result.added == 1
    assert result.errors == []

    proxies = list(local_roms.rglob("*.romcloud"))
    assert len(proxies) == 1

    game = catalog_svc.resolve_proxy(str(proxies[0]))
    assert game.title == "BCES00000"
    assert game.system == "ps3"
    assert game.primary_asset is not None

    launch_path = cache_svc.cache_game(game.id)
    assert cache_svc.is_cached(game.id)

    # For a directory game the launch path should point to the game directory
    launch_p = Path(launch_path)
    assert launch_p.exists()
    # The directory's contents must be intact
    assert (launch_p / "EBOOT.BIN").exists()
    assert (launch_p / "data" / "archive.pkg").exists()


def test_cue_bin_game(tmp_path: Path) -> None:
    """cue+bin game: .cue is catalogued; .bin track is suppressed."""

    source_root = tmp_path / "source_roms"
    (source_root / "psx").mkdir(parents=True)
    cue_file = source_root / "psx" / "Crash Bandicoot.cue"
    bin_file = source_root / "psx" / "Crash Bandicoot.bin"
    cue_file.write_bytes(b"cue_data")
    bin_file.write_bytes(b"bin_data" * 500)

    local_roms = tmp_path / "local_roms"
    local_roms.mkdir()
    data_dir = tmp_path / "data"
    data_dir.mkdir()

    db = Database(str(data_dir / "catalog.db"))
    db.initialize()
    game_repo = GameRepository(db)
    proxy_repo = ProxyRepository(db)

    catalog_svc = CatalogService(
        provider=LocalFilesystemProvider(),
        game_repo=game_repo,
        proxy_repo=proxy_repo,
        local_roms_root=str(local_roms),
        source_root=str(source_root),
    )

    result = catalog_svc.refresh()
    # Only the .cue should be added, not the .bin
    assert result.added == 1
    games = game_repo.find_by_system("psx")
    assert len(games) == 1
    assert games[0].title == "Crash Bandicoot"
    assert games[0].primary_asset.filename == "Crash Bandicoot.cue"
