"""Unit tests for the [saves] config section (SavesConfig)."""

from __future__ import annotations

from pathlib import Path

from romcloud.infrastructure.config import (
    AppConfig,
    CacheConfig,
    SavesConfig,
    SourceConfig,
    load_config,
    write_config,
)


def _base_config(**saves_kwargs) -> AppConfig:
    return AppConfig(
        source=SourceConfig(provider="local", rom_root="/roms"),
        cache=CacheConfig(path="/cache"),
        local_roms_path="/local",
        data_path="/data",
        saves=SavesConfig(**saves_kwargs) if saves_kwargs else SavesConfig(),
    )


class TestSavesConfigDefaults:
    def test_defaults_present_on_bare_config(self, tmp_path: Path):
        config_path = tmp_path / "romcloud.toml"
        write_config(_base_config(), str(config_path))
        loaded = load_config(str(config_path))

        assert loaded.saves.local_path == "/userdata/saves"
        assert loaded.saves.remote_subdir == "romcloud-saves"
        assert loaded.saves.xbox_enabled is False

    def test_missing_saves_section_in_legacy_config_uses_defaults(self, tmp_path: Path):
        config_path = tmp_path / "romcloud.toml"
        config_path.write_text(
            '[source]\nprovider = "local"\nrom_root = "/roms"\n\n'
            '[cache]\npath = "/cache"\n'
        )

        loaded = load_config(str(config_path))

        assert loaded.saves.local_path == "/userdata/saves"
        assert loaded.saves.xbox_enabled is False


class TestSavesConfigRoundTrip:
    def test_custom_values_round_trip(self, tmp_path: Path):
        config_path = tmp_path / "romcloud.toml"
        config = _base_config(
            local_path="/mnt/saves", remote_subdir="my-saves", xbox_enabled=True
        )
        write_config(config, str(config_path))

        loaded = load_config(str(config_path))

        assert loaded.saves.local_path == "/mnt/saves"
        assert loaded.saves.remote_subdir == "my-saves"
        assert loaded.saves.xbox_enabled is True

    def test_rewriting_preserves_xbox_enabled(self, tmp_path: Path):
        config_path = tmp_path / "romcloud.toml"
        write_config(_base_config(xbox_enabled=True), str(config_path))
        loaded = load_config(str(config_path))
        write_config(loaded, str(config_path))
        reloaded = load_config(str(config_path))

        assert reloaded.saves.xbox_enabled is True
