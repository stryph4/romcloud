"""Operation budgets for catalog source-migration reconciliation."""

from __future__ import annotations

import time
from collections import Counter
from pathlib import Path

from romcloud.core.models.game import Game, GameAsset
from romcloud.infrastructure.database import Database
from romcloud.infrastructure.providers.local import LocalFilesystemProvider
from romcloud.infrastructure.repositories.cache import CacheRepository
from romcloud.infrastructure.repositories.game import GameRepository
from romcloud.infrastructure.repositories.proxy import ProxyRepository
from romcloud.integrations.batocera.catalog import CatalogService
from romcloud.integrations.batocera.system_registry import EffectiveSystemRegistry


class _CountingDatabase(Database):
    def __init__(self, path: str) -> None:
        super().__init__(path)
        self.statements: Counter[str] = Counter()

    def connect(self):  # noqa: ANN201
        connection = super().connect()

        def record(statement: str) -> None:
            operation = statement.lstrip().split(None, 1)[0].upper()
            self.statements[operation] += 1

        connection.set_trace_callback(record)
        return connection

    def reset_counts(self) -> None:
        self.statements.clear()


class _CountingCacheRepository(CacheRepository):
    def __init__(self, database: Database) -> None:
        super().__init__(database)
        self.get_calls = 0

    def get(self, game_id: str):  # noqa: ANN201
        self.get_calls += 1
        return super().get(game_id)


class _CountingProxyRepository(ProxyRepository):
    def __init__(self, database: Database) -> None:
        super().__init__(database)
        self.list_all_calls = 0

    def list_all(self):  # noqa: ANN201
        self.list_all_calls += 1
        return super().list_all()


class _ProviderView(LocalFilesystemProvider):
    def __init__(self, provider_id: str, physical_root: Path) -> None:
        super().__init__()
        self._provider_id = provider_id
        self._physical_root = physical_root

    @property
    def provider_id(self) -> str:
        return self._provider_id

    def list_systems(self, rom_root: str):  # noqa: ANN201
        del rom_root
        return super().list_systems(str(self._physical_root))

    def list_entries(self, rom_root: str, system: str):  # noqa: ANN201
        del rom_root
        return super().list_entries(str(self._physical_root), system)


def _service(
    provider,
    games,
    caches,
    proxies,
    local_roms: Path,
    source_root: str,
) -> CatalogService:
    return CatalogService(
        provider,
        games,
        proxies,
        str(local_roms),
        source_root,
        system_registry=EffectiveSystemRegistry.from_extensions({"xbox": {".iso"}}),
        cache_repo=caches,
    )


def _library(root: Path, count: int) -> None:
    (root / "xbox").mkdir(parents=True)
    for index in range(count):
        (root / "xbox" / f"Game {index:04d}.iso").write_bytes(b"game")


def test_clean_smb_to_sftp_migration_uses_batched_catalog_state(tmp_path: Path) -> None:
    count = 40
    source = tmp_path / "source"
    local_roms = tmp_path / "local-roms"
    local_roms.mkdir()
    _library(source, count)
    database = _CountingDatabase(str(tmp_path / "catalog.db"))
    database.initialize()
    games = GameRepository(database)
    caches = _CountingCacheRepository(database)
    proxies = _CountingProxyRepository(database)
    _service(
        _ProviderView("local", source),
        games,
        caches,
        proxies,
        local_roms,
        str(source),
    ).refresh()

    database.reset_counts()
    proxies.list_all_calls = 0
    started = time.perf_counter()
    result = _service(
        _ProviderView("sftp", source),
        games,
        caches,
        proxies,
        local_roms,
        "/Roms",
    ).refresh()
    elapsed = time.perf_counter() - started
    counts = dict(database.statements)

    print({"clean_migration_elapsed": elapsed, "clean_migration_db": counts})
    assert result.errors == []
    assert result.updated == count and result.added == 0
    assert counts.get("SELECT", 0) <= 6
    assert counts.get("COMMIT", 0) == 1
    assert proxies.list_all_calls == 1
    assert result.metrics.games_processed == count
    assert result.metrics.game_write_batches == 1
    assert result.metrics.duplicate_rows_retired == 0
    assert result.metrics.ownership_scans == 0


def test_same_source_and_duplicate_migration_operation_budgets(
    tmp_path: Path, monkeypatch
) -> None:
    count = 40
    source = tmp_path / "source"
    local_roms = tmp_path / "local-roms"
    local_roms.mkdir()
    _library(source, count)
    database = _CountingDatabase(str(tmp_path / "catalog.db"))
    database.initialize()
    games = GameRepository(database)
    caches = _CountingCacheRepository(database)
    proxies = _CountingProxyRepository(database)
    smb = _ProviderView("local", source)
    sftp = _ProviderView("sftp", source)
    smb_catalog = _service(
        smb, games, caches, proxies, local_roms, str(source)
    )
    smb_catalog.refresh()

    database.reset_counts()
    proxies.list_all_calls = 0
    started = time.perf_counter()
    same_result = smb_catalog.refresh()
    same_elapsed = time.perf_counter() - started
    same_counts = dict(database.statements)

    sftp_catalog = _service(sftp, games, caches, proxies, local_roms, "/Roms")
    for old_game in games.find_by_system("xbox"):
        primary = old_game.primary_asset
        assert primary is not None
        duplicate = Game.create(
            "xbox",
            old_game.title,
            "sftp",
            "/Roms",
            [
                GameAsset(
                    primary.filename,
                    primary.relative_path,
                    size_bytes=primary.size_bytes,
                    is_primary=True,
                )
            ],
        )
        games.save(duplicate)
        sftp_catalog.ensure_proxy(duplicate)

    import romcloud.integrations.batocera.catalog as catalog_module

    ownership_scans = 0
    ownership_seconds = 0.0
    real_remove = catalog_module.remove_owned_proxy_files

    def measured_remove(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        nonlocal ownership_scans, ownership_seconds
        ownership_scans += 1
        started_at = time.perf_counter()
        try:
            return real_remove(*args, **kwargs)
        finally:
            ownership_seconds += time.perf_counter() - started_at

    monkeypatch.setattr(catalog_module, "remove_owned_proxy_files", measured_remove)
    database.reset_counts()
    caches.get_calls = 0
    proxies.list_all_calls = 0
    started = time.perf_counter()
    result = sftp_catalog.refresh()
    migration_elapsed = time.perf_counter() - started
    migration_counts = dict(database.statements)

    print(
        {
            "same_elapsed": same_elapsed,
            "same_db": same_counts,
            "migration_elapsed": migration_elapsed,
            "migration_db": migration_counts,
            "cache_gets": caches.get_calls,
            "proxy_manifest_loads": proxies.list_all_calls,
            "ownership_scans": ownership_scans,
            "ownership_seconds": ownership_seconds,
            "refresh_metrics": result.metrics,
        }
    )
    assert result.errors == []
    assert len(games.find_by_system("xbox")) == count
    assert same_result.metrics.games_processed == count
    assert same_counts.get("SELECT", 0) <= 6
    assert migration_counts.get("SELECT", 0) <= 8
    assert migration_counts.get("COMMIT", 0) <= 3
    assert caches.get_calls == 0
    assert proxies.list_all_calls == 1
    assert ownership_scans == 1
    assert result.metrics.games_processed == count
    assert result.metrics.cache_prefetches == 1
    assert result.metrics.proxy_manifest_prefetches == 1
    assert result.metrics.game_row_writes == count
    assert result.metrics.game_write_batches == 1
    assert result.metrics.duplicate_rows_retired == count
    assert result.metrics.duplicate_delete_batches == 1
    assert result.metrics.ownership_scans == 1
