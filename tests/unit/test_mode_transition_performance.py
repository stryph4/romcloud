"""Mode-transition scaling regressions.

Covers the two concrete CPU-bound hot paths found on real, production-sized
hardware: (1) per-game SQLite connections opened in a loop instead of bulk
reads, and (2) an O(catalog^2) linear XML rescan inside Library Sync's local
gamelist render. Both must scale with the actual delta being reconciled, not
with the full catalog size, and ordinary mode transitions must never trigger
catalog refresh or Library Sync media materialization.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from romcloud.bootstrap.container import Container
from romcloud.core.capabilities import OperatingMode
from romcloud.core.models.cache import CacheEntry, CacheStatus
from romcloud.core.models.game import Game, GameAsset
from romcloud.core.models.proxy import ProxyRecord
from romcloud.infrastructure.config import AppConfig, CacheConfig, SourceConfig
from romcloud.infrastructure.database import Database
from romcloud.infrastructure.repositories.cache import CacheRepository
from romcloud.infrastructure.repositories.game import GameRepository
from romcloud.infrastructure.repositories.proxy import ProxyRepository
from romcloud.integrations.batocera.game_access import (
    reconcile_library_presentation,
    set_operating_mode,
)
from romcloud.services.library_sync import (
    CANONICAL_FILENAME,
    SCHEMA_VERSION,
    LibrarySyncService,
    library_id_for_game,
)


def _config(tmp_path: Path) -> AppConfig:
    source = tmp_path / "source"
    local = tmp_path / "roms"
    cache = tmp_path / "cache"
    for root in (source / "snes", local / "snes", cache):
        root.mkdir(parents=True, exist_ok=True)
    return AppConfig(
        source=SourceConfig("local", str(source)),
        cache=CacheConfig(str(cache)),
        local_roms_path=str(local),
        data_path=str(tmp_path / "data"),
    )


def _seed_catalog(config: AppConfig, *, total: int, cached: int) -> None:
    db = Database(str(Path(config.data_path) / "catalog.db"))
    db.initialize()
    games = GameRepository(db)
    proxies = ProxyRepository(db)
    caches = CacheRepository(db)
    for i in range(total):
        title = f"Game {i:05d}"
        filename = f"{title}.sfc"
        asset = GameAsset(
            filename=filename,
            relative_path=f"snes/{filename}",
            size_bytes=4,
            is_primary=True,
        )
        game = Game.create("snes", title, "local", config.source.rom_root, [asset])
        games.save(game)
        proxy_path = Path(config.local_roms_path) / "snes" / f"{title}.romcloud"
        proxies.save(ProxyRecord.create(game.id, str(proxy_path)))
        if i < cached:
            cached_path = Path(config.cache.path) / "snes" / filename
            cached_path.parent.mkdir(parents=True, exist_ok=True)
            cached_path.write_bytes(b"data")
            entry = CacheEntry.create(game.id, str(cached_path))
            entry.status = CacheStatus.COMPLETE
            entry.size_bytes = 4
            caches.save(entry)


class _ConnectCounter:
    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.count = 0
        original = Database.connect

        def counting_connect(db_self):  # noqa: ANN001
            self.count += 1
            return original(db_self)

        monkeypatch.setattr(Database, "connect", counting_connect)


def test_offline_visibility_does_not_open_a_connection_per_game(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The Offline Mode visibility computation must bulk-load cache entries
    and games once, not issue two SQLite connections per catalogued game
    (the confirmed real-hardware CPU-bound hot path)."""
    total, cached = 300, 60
    config = _config(tmp_path)
    _seed_catalog(config, total=total, cached=cached)
    counter = _ConnectCounter(monkeypatch)

    report = reconcile_library_presentation(config, offline=True)

    assert report.visible == cached
    # However many connections bulk reconciliation legitimately needs, it
    # must not scale anywhere near 1-per-game (previously 2 * total+).
    assert counter.count < total // 2


def test_large_synthetic_catalog_offline_transition_is_fast(tmp_path: Path) -> None:
    total, cached = 3000, 1000
    config = _config(tmp_path)
    _seed_catalog(config, total=total, cached=cached)

    start = time.monotonic()
    report = reconcile_library_presentation(config, offline=True)
    elapsed = time.monotonic() - start

    assert report.visible == cached
    # Generous ceiling: catches an O(n) DB-connection-per-game or O(n^2)
    # regression without being flaky on slow CI hardware.
    assert elapsed < 5.0, f"Offline transition took {elapsed:.2f}s for {total} games"


def test_offline_transition_does_not_touch_already_correct_proxies(
    tmp_path: Path,
) -> None:
    """Re-entering the same mode must not rewrite proxies that are already
    in the correct state."""
    config = _config(tmp_path)
    _seed_catalog(config, total=20, cached=5)

    first = reconcile_library_presentation(config, offline=True)
    proxy_dir = Path(config.local_roms_path) / "snes"
    mtimes_before = {p: p.stat().st_mtime_ns for p in proxy_dir.glob("*.romcloud")}

    second = reconcile_library_presentation(config, offline=True)
    mtimes_after = {p: p.stat().st_mtime_ns for p in proxy_dir.glob("*.romcloud")}

    assert first.visible == second.visible == 5
    assert mtimes_before == mtimes_after


def test_mode_transition_never_refreshes_catalog_or_materializes_library_sync_media(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path)
    _seed_catalog(config, total=10, cached=3)
    monkeypatch.setattr(
        "romcloud.integrations.batocera.catalog.CatalogService.refresh",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("catalog refresh")),
    )
    monkeypatch.setattr(
        "romcloud.services.library_sync.LibrarySyncService._materialize_remote_media",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("media materialization")),
    )
    monkeypatch.setattr(
        "romcloud.integrations.batocera.game_access._refresh_emulationstation",
        lambda config, systems, **kwargs: None,
    )
    monkeypatch.setattr(
        "romcloud.integrations.batocera.game_access._reload_emulationstation",
        lambda: True,
    )

    for mode in (OperatingMode.CONNECTED, OperatingMode.CACHE, OperatingMode.OFFLINE):
        set_operating_mode(config, mode)


class TestLibrarySyncRenderScaling:
    def _service(self, tmp_path: Path, *, total: int) -> LibrarySyncService:
        source = tmp_path / "source"
        local = tmp_path / "roms"
        data = tmp_path / "data"
        (source / "snes").mkdir(parents=True)
        (local / "snes").mkdir(parents=True)
        (data / "library").mkdir(parents=True)
        db = Database(str(data / "catalog.db"))
        db.initialize()
        games_repo = GameRepository(db)
        proxy_repo = ProxyRepository(db)

        records = {}
        for i in range(total):
            title = f"Game {i:05d}"
            filename = f"{title}.sfc"
            asset = GameAsset(
                filename=filename,
                relative_path=f"snes/{filename}",
                size_bytes=4,
                is_primary=True,
            )
            game = Game.create("snes", title, "local", str(source), [asset])
            games_repo.save(game)
            proxy_path = local / "snes" / f"{title}.romcloud"
            proxy_path.write_text("{}")
            proxy_repo.save(ProxyRecord.create(game.id, str(proxy_path)))
            records[library_id_for_game(game)] = {
                "metadata": {"desc": "d"}, "media": {},
            }

        (data / "library" / CANONICAL_FILENAME).write_text(
            json.dumps({"schema_version": SCHEMA_VERSION, "records": records})
        )
        return LibrarySyncService(
            enabled=True,
            provider=None,
            connectivity_root=None,
            source_root=str(source),
            local_roms_root=str(local),
            data_root=str(data),
            remote_root=None,
            game_access_mode="smart_cache",
            game_repo=games_repo,
            proxy_repo=proxy_repo,
        )

    def test_render_local_scales_linearly_not_quadratically(self, tmp_path: Path) -> None:
        """Regression for the O(catalog^2) path-adoption rescan: rendering
        a fresh gamelist for every game used to re-scan the whole,
        ever-growing XML tree per game."""
        svc = self._service(tmp_path, total=1500)

        start = time.monotonic()
        report = svc.render_local()
        elapsed = time.monotonic() - start

        assert report.rendered == 1500
        assert elapsed < 3.0, f"render_local took {elapsed:.2f}s for 1500 games"

    def test_render_local_still_adopts_pre_marker_entries_by_path(
        self, tmp_path: Path
    ) -> None:
        """Path-based adoption of un-owned pre-existing gamelist entries
        (legacy/beta data with no ownership marker yet) must still work
        after switching to an indexed lookup."""
        svc = self._service(tmp_path, total=3)
        gamelist_path = Path(svc._local_roms_root) / "snes" / "gamelist.xml"
        # Simulate a pre-marker beta entry for "Game 00000" with an existing
        # <favorite> flag that must be preserved across adoption.
        launch_path = svc._local_launch_path(
            svc._games.list_all()[0], None
        )
        gamelist_path.write_text(
            '<?xml version="1.0"?>\n<gameList>\n'
            f'  <game><path>{launch_path}</path><favorite>true</favorite></game>\n'
            "</gameList>\n",
            encoding="utf-8",
        )

        svc.render_local()

        content = gamelist_path.read_text(encoding="utf-8")
        assert "<favorite>true</favorite>" in content
