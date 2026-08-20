"""Focused positive-allowlist and deselection ownership tests."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from romcloud.core.models.cache import CacheEntry, CacheStatus
from romcloud.infrastructure.config import (
    AppConfig,
    CacheConfig,
    SavesConfig,
    SourceConfig,
    load_config,
    write_config,
)
from romcloud.infrastructure.database import Database
from romcloud.infrastructure.providers.local import LocalFilesystemProvider
from romcloud.infrastructure.repositories.cache import CacheRepository
from romcloud.infrastructure.repositories.game import GameRepository
from romcloud.infrastructure.repositories.proxy import ProxyRepository
from romcloud.integrations.batocera.catalog import CatalogService
from tests.system_registry_fixture import TEST_SYSTEM_REGISTRY


def _repos(tmp_path: Path):
    db = Database(str(tmp_path / "catalog.db"))
    db.initialize()
    return GameRepository(db), ProxyRepository(db), CacheRepository(db)


def _catalog(
    source: Path,
    local_roms: Path,
    game_repo: GameRepository,
    proxy_repo: ProxyRepository,
    selected_systems,
    *,
    provider_id: str = "local",
) -> CatalogService:
    class Provider(LocalFilesystemProvider):
        PROVIDER_ID = provider_id

    return CatalogService(
        provider=Provider(),
        game_repo=game_repo,
        proxy_repo=proxy_repo,
        local_roms_root=str(local_roms),
        source_root=str(source),
        system_registry=TEST_SYSTEM_REGISTRY,
        selected_systems=selected_systems,
    )


def _source(tmp_path: Path) -> Path:
    source = tmp_path / "source"
    (source / "nes").mkdir(parents=True)
    (source / "ps2").mkdir()
    (source / "nes" / "Mario.nes").write_bytes(b"nes")
    (source / "ps2" / "Game.iso").write_bytes(b"ps2")
    return source


def test_missing_selected_systems_means_all_detected(tmp_path: Path):
    path = tmp_path / "romcloud.toml"
    path.write_text('[source]\nprovider = "local"\nrom_root = "/roms"\n')
    assert load_config(str(path), resolve_paths=False).source.selected_systems is None

    source = _source(tmp_path)
    local = tmp_path / "local"
    games, proxies, _caches = _repos(tmp_path)
    result = _catalog(source, local, games, proxies, None).refresh()
    assert result.added == 2
    assert games.list_systems() == ["nes", "ps2"]


def test_explicit_subset_round_trips_and_filters_catalog(tmp_path: Path):
    config = AppConfig(
        source=SourceConfig("local", "/roms", selected_systems=("ps2",)),
        cache=CacheConfig("/cache"),
        local_roms_path="/local",
        data_path="/data",
        saves=SavesConfig(local_path="/userdata/saves"),
    )
    path = tmp_path / "romcloud.toml"
    write_config(config, str(path))
    assert load_config(str(path), resolve_paths=False).source.selected_systems == ("ps2",)

    source = _source(tmp_path)
    local = tmp_path / "local"
    games, proxies, _caches = _repos(tmp_path)
    result = _catalog(source, local, games, proxies, ("ps2",)).refresh()
    assert result.added == 1
    assert games.list_systems() == ["ps2"]
    assert not (local / "nes").exists()


def test_new_selection_is_cataloged_and_deselection_only_removes_owned_artifacts(
    tmp_path: Path,
):
    source = _source(tmp_path)
    local = tmp_path / "local"
    nes_local = local / "nes"
    nes_local.mkdir(parents=True)
    user_rom = nes_local / "My Local Game.nes"
    user_rom.write_bytes(b"user-owned")
    foreign_proxy = nes_local / "notes.romcloud"
    foreign_proxy.write_text("not a ROMCloud proxy")
    gamelist = nes_local / "gamelist.xml"
    gamelist.write_text("<gameList />")
    games, proxies, caches = _repos(tmp_path)

    first = _catalog(source, local, games, proxies, ("ps2",)).refresh()
    assert first.added == 1
    assert games.list_systems() == ["ps2"]

    second = _catalog(source, local, games, proxies, ("nes", "ps2")).refresh()
    assert second.added == 1
    nes_game = games.find_by_system("nes")[0]
    owned_proxy = Path(proxies.get(nes_game.id).proxy_path)
    assert owned_proxy.is_file()

    cache_payload = tmp_path / "cache" / "nes" / "Mario.nes"
    cache_payload.parent.mkdir(parents=True)
    cache_payload.write_bytes(b"cached-payload")
    now = datetime.now(timezone.utc)
    caches.save(
        CacheEntry(
            game_id=nes_game.id,
            cache_path=str(cache_payload),
            status=CacheStatus.COMPLETE,
            cached_at=now,
            last_accessed=now,
            size_bytes=cache_payload.stat().st_size,
        )
    )

    third = _catalog(source, local, games, proxies, ("ps2",)).refresh()
    assert third.removed == 1
    assert games.find_by_system("nes") == []
    assert games.find_by_system("nes", include_ineligible=True)[0].id == nes_game.id
    assert proxies.get(nes_game.id) is None
    assert not owned_proxy.exists()
    assert user_rom.read_bytes() == b"user-owned"
    assert foreign_proxy.read_text() == "not a ROMCloud proxy"
    assert gamelist.read_text() == "<gameList />"
    assert cache_payload.read_bytes() == b"cached-payload"
    assert caches.get(nes_game.id) is not None


@pytest.mark.parametrize("provider_id", ["local", "smb", "sftp"])
def test_provider_identity_does_not_change_selection_behavior(
    tmp_path: Path, provider_id: str
):
    source = _source(tmp_path)
    local = tmp_path / "local"
    games, proxies, _caches = _repos(tmp_path)
    result = _catalog(
        source, local, games, proxies, ("ps2",), provider_id=provider_id
    ).refresh()
    assert result.added == 1
    assert games.list_systems() == ["ps2"]
    assert games.find_by_system("ps2")[0].source_provider == provider_id
