from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from romcloud.infrastructure.config import (
    AppConfig,
    CacheConfig,
    SavesConfig,
    SourceConfig,
    load_config,
    write_config,
)
from romcloud.services import system_selection
from romcloud.integrations.batocera import game_access
from romcloud.core.capabilities import OperatingMode


def test_post_setup_action_updates_config_and_triggers_reconciliation(
    tmp_path: Path, monkeypatch
):
    config_path = tmp_path / "romcloud.toml"
    write_config(
        AppConfig(
            source=SourceConfig("local", "/source"),
            cache=CacheConfig("/cache"),
            local_roms_path="/roms",
            data_path="/data",
            saves=SavesConfig(local_path="/userdata/saves"),
        ),
        str(config_path),
    )
    refresh_calls = []
    access_calls = []

    class FakeContainer:
        def __init__(self, config):
            self.config = config
            self.provider = SimpleNamespace(
                list_systems=lambda _root: ["nes", "ps2"]
            )
            self.system_registry = SimpleNamespace(names={"nes", "ps2"})
            self.catalog = SimpleNamespace(refresh=self.refresh)

        def refresh(self, progress=None):
            refresh_calls.append(self.config.source.selected_systems)
            return SimpleNamespace(errors=[], added=0, removed=1)

    monkeypatch.setattr(system_selection, "Container", FakeContainer)
    monkeypatch.setattr(
        "romcloud.integrations.batocera.game_access.reconcile_game_access",
        lambda config: access_calls.append(config.source.selected_systems)
        or SimpleNamespace(created=0, removed=1),
    )

    result = system_selection.update_selection(
        config_path, {"selected_systems": ["ps2"]}
    )

    assert load_config(str(config_path)).source.selected_systems == ("ps2",)
    assert refresh_calls == [("ps2",)]
    assert access_calls == [("ps2",)]
    assert result["newly_deselected"] == ["nes"]


def test_game_access_defensively_excludes_unselected_catalog_rows(monkeypatch):
    config = AppConfig(
        source=SourceConfig("local", "/source", selected_systems=("ps2",)),
        cache=CacheConfig("/cache"),
        local_roms_path="/roms",
        data_path="/data",
        saves=SavesConfig(local_path="/userdata/saves"),
    )
    games = [
        SimpleNamespace(id="nes-game", system="nes"),
        SimpleNamespace(id="ps2-game", system="ps2"),
    ]
    fake_container = SimpleNamespace(
        game_repo=SimpleNamespace(list_all=lambda: games),
    )
    restored = []
    removed = []

    monkeypatch.setattr(
        game_access, "Container", lambda *args, **kwargs: fake_container
    )
    monkeypatch.setattr(
        "romcloud.infrastructure.library_view.operating_mode",
        lambda _config: OperatingMode.CACHE,
    )
    monkeypatch.setattr(
        game_access,
        "remove_direct_links",
        lambda _config: SimpleNamespace(created=0, removed=0),
    )
    monkeypatch.setattr(
        game_access,
        "_refresh_emulationstation",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        "romcloud.lifecycle.manage.restore_owned_proxies",
        lambda _config, game_ids=None, **kwargs: restored.append(
            set(game_ids or ())
        )
        or 0,
    )
    monkeypatch.setattr(
        "romcloud.lifecycle.manage.remove_owned_proxies",
        lambda _config, keep_game_ids=None, **kwargs: removed.append(
            set(keep_game_ids or ())
        )
        or 0,
    )

    game_access.reconcile_game_access(
        config, refresh_es=False, render_library_metadata=False
    )

    assert restored == [{"ps2-game"}]
    assert removed == [{"ps2-game"}]
