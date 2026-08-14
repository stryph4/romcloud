"""Shared pytest fixtures."""

from __future__ import annotations

import sys
import pytest
from pathlib import Path
from typing import TYPE_CHECKING

from romcloud.infrastructure.database import Database
from romcloud.infrastructure.repositories.game import GameRepository
from romcloud.infrastructure.repositories.cache import CacheRepository
from romcloud.infrastructure.repositories.proxy import ProxyRepository
from romcloud.core.models.cache import CachePolicy
from romcloud.infrastructure.providers.local import LocalFilesystemProvider
from romcloud.integrations.batocera.catalog import CatalogService
from tests.system_registry_fixture import TEST_SYSTEM_REGISTRY

if TYPE_CHECKING:
    from romcloud.services.cache import CacheService
    from romcloud.services.transfer import TransferService


@pytest.fixture(autouse=True)
def _explicit_test_system_registry(monkeypatch):
    """Container-backed tests run off-device with an explicit ES registry."""
    module = sys.modules.get("romcloud.bootstrap.container")
    if module is None:
        # Avoid importing POSIX-only lifecycle dependencies for focused tests
        # that do not use the application container.
        yield
        return
    Container = module.Container

    monkeypatch.setattr(
        Container,
        "system_registry",
        property(lambda _self: TEST_SYSTEM_REGISTRY),
    )
    yield


@pytest.fixture
def rom_root(tmp_path: Path) -> Path:
    """A minimal Batocera-style ROM root with fake game files."""
    root = tmp_path / "source_roms"
    (root / "ps2").mkdir(parents=True)
    (root / "nes").mkdir(parents=True)
    (root / "snes").mkdir(parents=True)
    # Single-file games
    (root / "ps2" / "Final Fantasy X.iso").write_bytes(b"fake_iso_data" * 200)
    (root / "ps2" / "Shadow of the Colossus.iso").write_bytes(b"fake_iso2" * 150)
    (root / "nes" / "Super Mario Bros.nes").write_bytes(b"nes_rom_data" * 50)
    # Hidden file — should be ignored
    (root / "ps2" / ".hidden_file").write_text("ignore me")
    # Already-proxied — should not be re-catalogued
    (root / "snes" / "Some Game.romcloud").write_text("{}")
    return root


@pytest.fixture
def local_roms_dir(tmp_path: Path) -> Path:
    d = tmp_path / "local_roms"
    d.mkdir()
    return d


@pytest.fixture
def cache_dir(tmp_path: Path) -> Path:
    d = tmp_path / "romcloud_cache"
    d.mkdir()
    return d


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    d = tmp_path / "data"
    d.mkdir()
    return d


@pytest.fixture
def db(data_dir: Path) -> Database:
    database = Database(str(data_dir / "catalog.db"))
    database.initialize()
    return database


@pytest.fixture
def game_repo(db: Database) -> GameRepository:
    return GameRepository(db)


@pytest.fixture
def cache_repo(db: Database) -> CacheRepository:
    return CacheRepository(db)


@pytest.fixture
def proxy_repo(db: Database) -> ProxyRepository:
    return ProxyRepository(db)


@pytest.fixture
def provider() -> LocalFilesystemProvider:
    return LocalFilesystemProvider()


@pytest.fixture
def transfer_service(provider: LocalFilesystemProvider, cache_dir: Path) -> TransferService:
    from romcloud.services.transfer import TransferService

    return TransferService(provider=provider, cache_root=str(cache_dir))


@pytest.fixture
def policy() -> CachePolicy:
    return CachePolicy.from_gb(max_size_gb=10.0, min_free_gb=0.001)


@pytest.fixture
def cache_service(
    cache_repo: CacheRepository,
    game_repo: GameRepository,
    transfer_service: TransferService,
    cache_dir: Path,
    policy: CachePolicy,
) -> CacheService:
    from romcloud.services.cache import CacheService

    return CacheService(
        cache_repo=cache_repo,
        game_repo=game_repo,
        transfer_service=transfer_service,
        cache_root=str(cache_dir),
        policy=policy,
    )


@pytest.fixture
def catalog_service(
    provider: LocalFilesystemProvider,
    game_repo: GameRepository,
    proxy_repo: ProxyRepository,
    local_roms_dir: Path,
    rom_root: Path,
) -> CatalogService:
    return CatalogService(
        provider=provider,
        game_repo=game_repo,
        proxy_repo=proxy_repo,
        local_roms_root=str(local_roms_dir),
        source_root=str(rom_root),
        system_registry=TEST_SYSTEM_REGISTRY,
    )
