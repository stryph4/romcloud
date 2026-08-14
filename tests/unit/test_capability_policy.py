from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from click.testing import CliRunner

from romcloud.cli.main import cli
from romcloud.core.capabilities import Capability, CapabilityPolicy, PresentationIntent
from romcloud.core.exceptions import CapabilityUnavailableError
from romcloud.core.models.cache import CachePolicy
from romcloud.core.models.game import Game, GameAsset
from romcloud.infrastructure.config import (
    AppConfig,
    CacheConfig,
    SourceConfig,
    write_config,
)
from romcloud.infrastructure.library_view import write_offline_library_state
from romcloud.integrations.batocera.catalog import CatalogService
from tests.system_registry_fixture import TEST_SYSTEM_REGISTRY
from romcloud.services.cache import CacheService
from romcloud.services.saves import SaveSyncService


@pytest.fixture
def offline_policy() -> CapabilityPolicy:
    return CapabilityPolicy("smart_cache", PresentationIntent.OFFLINE)


def test_offline_policy_has_narrow_explicit_capabilities(offline_policy) -> None:
    for capability in (
        Capability.GAME_DOWNLOAD,
        Capability.CATALOG_REFRESH,
        Capability.LIBRARY_SYNC,
        Capability.SAVE_SYNC,
        Capability.UPDATE_NETWORK,
        Capability.REMOTE_VALIDATION,
    ):
        assert not offline_policy.allows(capability)
    for capability in (
        Capability.CACHED_LAUNCH,
        Capability.CACHE_STATUS,
        Capability.CACHE_MANAGE,
        Capability.LOCAL_SETTINGS,
        Capability.LOCAL_DIAGNOSTICS,
        Capability.CONNECTION_RECOVERY,
    ):
        assert offline_policy.allows(capability)

    serialized = offline_policy.serialize()
    assert serialized["presentation_intent"] == "offline"
    assert serialized["capabilities"]["save_sync"] is False
    assert "Offline" in serialized["blocked_reasons"]["save_sync"]


def test_configured_strategy_does_not_override_authoritative_offline_state() -> None:
    policy = CapabilityPolicy("direct_nas", PresentationIntent.OFFLINE)
    assert policy.offline_mode_supported
    assert policy.offline
    assert policy.allows(Capability.OFFLINE_MODE)
    assert policy.serialize()["operating_mode"] == "offline"
    assert policy.serialize()["presentation_intent"] == "offline"
    assert not policy.allows(Capability.CATALOG_REFRESH)


def test_catalog_guard_runs_before_provider_access(
    offline_policy, provider, game_repo, proxy_repo, local_roms_dir, rom_root, monkeypatch
) -> None:
    touched = False

    def list_systems(root):
        nonlocal touched
        touched = True
        return []

    monkeypatch.setattr(provider, "list_systems", list_systems)
    service = CatalogService(
        provider=provider,
        game_repo=game_repo,
        proxy_repo=proxy_repo,
        local_roms_root=str(local_roms_dir),
        source_root=str(rom_root),
        system_registry=TEST_SYSTEM_REGISTRY,
        capability_policy=offline_policy,
    )

    with pytest.raises(CapabilityUnavailableError, match="Offline"):
        service.refresh()
    assert not touched


def test_cached_game_and_local_cache_management_work_but_cache_miss_is_blocked(
    offline_policy,
    cache_service,
    cache_repo,
    game_repo,
    transfer_service,
    cache_dir,
    rom_root,
) -> None:
    source = rom_root / "ps2" / "Offline Game.iso"
    source.parent.mkdir(exist_ok=True)
    source.write_bytes(b"rom")
    game_with_file = Game.create(
        "ps2",
        "Offline Game",
        "local",
        str(rom_root),
        [GameAsset("Offline Game.iso", "ps2/Offline Game.iso", 3, True)],
    )
    game_repo.save(game_with_file)
    cached_path = cache_service.cache_game(game_with_file.id)
    offline_cache = CacheService(
        cache_repo=cache_repo,
        game_repo=game_repo,
        transfer_service=transfer_service,
        cache_root=str(cache_dir),
        policy=CachePolicy.from_gb(10, 0),
        capability_policy=offline_policy,
    )

    assert offline_cache.cache_game(game_with_file.id) == cached_path
    offline_cache.pin(game_with_file.id)
    assert cache_repo.get(game_with_file.id).is_pinned
    offline_cache.unpin(game_with_file.id)
    offline_cache.remove(game_with_file.id)
    with pytest.raises(CapabilityUnavailableError, match="Offline"):
        offline_cache.cache_game(game_with_file.id)


def test_savesync_guard_precedes_connectivity_and_preserves_local_saves(
    tmp_path: Path, offline_policy
) -> None:
    local = tmp_path / "saves"
    remote = tmp_path / "remote" / "saves"
    local.mkdir()
    remote.mkdir(parents=True)
    save = local / "game.sav"
    save.write_bytes(b"local-save")
    provider = Mock()
    service = SaveSyncService(
        provider=provider,
        connectivity_root=str(remote.parent),
        local_root=str(local),
        remote_root=str(remote),
        state_path=tmp_path / "state.json",
        capability_policy=offline_policy,
    )

    with pytest.raises(CapabilityUnavailableError, match="Offline"):
        service.preview_upload()
    provider.is_reachable.assert_not_called()
    assert save.read_bytes() == b"local-save"

    provider.is_reachable.return_value = True
    online = SaveSyncService(
        provider=provider,
        connectivity_root=str(remote.parent),
        local_root=str(local),
        remote_root=str(remote),
        state_path=tmp_path / "state.json",
        capability_policy=CapabilityPolicy("smart_cache", PresentationIntent.CACHE),
    )
    assert online.preview_upload().direction == "upload"
    assert save.read_bytes() == b"local-save"


def test_cli_refresh_and_update_are_blocked_without_network_work(tmp_path: Path) -> None:
    source = tmp_path / "source"
    local = tmp_path / "roms"
    cache = tmp_path / "cache"
    data = tmp_path / "data"
    for path in (source, local, cache, data):
        path.mkdir()
    config = AppConfig(
        source=SourceConfig("local", str(source)),
        cache=CacheConfig(str(cache)),
        local_roms_path=str(local),
        data_path=str(data),
    )
    config_path = tmp_path / "config" / "romcloud.toml"
    write_config(config, str(config_path))
    write_offline_library_state(config, True)
    runner = CliRunner()

    refresh = runner.invoke(cli, ["--config", str(config_path), "refresh", "--dry-run"])
    update = runner.invoke(cli, ["--config", str(config_path), "update", "--check"])

    assert refresh.exit_code == update.exit_code == 1
    assert "Offline" in refresh.output
    assert "Offline" in update.output


def test_uncached_cli_launch_is_blocked_before_provider_probe(
    tmp_path: Path, monkeypatch
) -> None:
    import romcloud.cli.commands.launch as launch_module

    data = tmp_path / "data"
    data.mkdir()
    config = AppConfig(
        source=SourceConfig("local", str(tmp_path / "source")),
        cache=CacheConfig(str(tmp_path / "cache")),
        local_roms_path=str(tmp_path / "roms"),
        data_path=str(data),
    )
    write_offline_library_state(config, True)
    provider = Mock()
    container = SimpleNamespace(
        config=config,
        catalog=SimpleNamespace(
            resolve_proxy=lambda path: SimpleNamespace(id="game", source_root="/source")
        ),
        cache=SimpleNamespace(is_cached=lambda game_id: False),
        provider=provider,
    )
    monkeypatch.setattr(launch_module, "get_container", lambda ctx: container)

    result = CliRunner().invoke(launch_module.launch_cmd, ["Game.romcloud"], obj={})

    assert result.exit_code == 1
    assert "Offline" in result.output
    provider.is_reachable.assert_not_called()
