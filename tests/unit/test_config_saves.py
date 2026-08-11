"""Unit tests for SaveSync settings and general remote-data config."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from romcloud.core.exceptions import ConfigurationError
from romcloud.infrastructure.config import (
    AppConfig,
    CacheConfig,
    LibrarySyncConfig,
    RemoteDataConfig,
    SavesConfig,
    SMBConfig,
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
        assert loaded.saves.xbox_enabled is False
        assert loaded.saves.rpcs3_installed_games_enabled is False
        assert loaded.saves.include_local_games is False
        assert loaded.remote_data is None
        assert loaded.library_sync.enabled is False

    def test_missing_saves_section_in_legacy_config_uses_defaults(self, tmp_path: Path):
        config_path = tmp_path / "romcloud.toml"
        config_path.write_text(
            '[source]\nprovider = "local"\nrom_root = "/roms"\n\n'
            '[cache]\npath = "/cache"\n'
        )

        loaded = load_config(str(config_path))

        assert loaded.saves.local_path == "/userdata/saves"
        assert loaded.saves.xbox_enabled is False
        assert loaded.remote_data is None

    def test_missing_cache_section_uses_consolidated_runtime_path(self, tmp_path: Path):
        config_path = tmp_path / "romcloud.toml"
        config_path.write_text(
            '[source]\nprovider = "local"\nrom_root = "/roms"\n'
        )

        loaded = load_config(str(config_path))

        assert loaded.cache.path == "/userdata/romcloud/cache"

    def test_pre_release_saves_destination_keys_do_not_enable_savesync(self, tmp_path: Path):
        config_path = tmp_path / "romcloud.toml"
        config_path.write_text(
            '[source]\nprovider = "local"\nrom_root = "/roms"\n\n'
            '[saves]\n'
            'remote_mount_path = "/userdata/romcloud-saves-source"\n'
            'remote_subdir = "romcloud-saves"\n'
        )

        loaded = load_config(str(config_path))
        assert loaded.remote_data is None

        write_config(loaded, str(config_path))
        rewritten = config_path.read_text()
        assert "remote_mount_path" not in rewritten
        assert "remote_subdir" not in rewritten


class TestSavesConfigRoundTrip:
    def test_library_sync_opt_in_round_trip(self, tmp_path: Path):
        config_path = tmp_path / "romcloud.toml"
        config = replace(
            _base_config(),
            remote_data=RemoteDataConfig("local", "/mnt/cloud-data"),
            library_sync=LibrarySyncConfig(True),
        )
        write_config(config, str(config_path))

        assert load_config(str(config_path)).library_sync.enabled is True

    def test_library_sync_cannot_be_enabled_without_remote_data(self, tmp_path: Path):
        with pytest.raises(ConfigurationError, match="Library Sync requires"):
            write_config(
                replace(_base_config(), library_sync=LibrarySyncConfig(True)),
                str(tmp_path / "romcloud.toml"),
            )

    def test_custom_values_round_trip(self, tmp_path: Path):
        config_path = tmp_path / "romcloud.toml"
        config = _base_config(
            local_path="/mnt/saves",
            xbox_enabled=True,
        )
        write_config(config, str(config_path))

        loaded = load_config(str(config_path))

        assert loaded.saves.local_path == "/mnt/saves"
        assert loaded.saves.xbox_enabled is True

    def test_rewriting_preserves_xbox_enabled(self, tmp_path: Path):
        config_path = tmp_path / "romcloud.toml"
        write_config(_base_config(xbox_enabled=True), str(config_path))
        loaded = load_config(str(config_path))
        write_config(loaded, str(config_path))
        reloaded = load_config(str(config_path))

        assert reloaded.saves.xbox_enabled is True

    def test_local_remote_data_round_trip(self, tmp_path: Path):
        config_path = tmp_path / "romcloud.toml"
        config = _base_config()
        config = AppConfig(
            **{**config.__dict__, "remote_data": RemoteDataConfig("local", "/mnt/cloud-data")}
        )
        write_config(config, str(config_path))

        loaded = load_config(str(config_path))

        assert loaded.remote_data == RemoteDataConfig("local", "/mnt/cloud-data")

    def test_rpcs3_installed_games_opt_in_round_trips_and_defaults_off(
        self, tmp_path: Path
    ):
        config = _base_config()
        config = replace(
            config,
            saves=replace(config.saves, rpcs3_installed_games_enabled=True),
        )
        path = tmp_path / "romcloud.toml"

        write_config(config, str(path))
        loaded = load_config(str(path))

        assert loaded.saves.rpcs3_installed_games_enabled is True

    def test_local_game_save_opt_in_round_trips(self, tmp_path: Path):
        config = _base_config()
        config = replace(
            config,
            saves=replace(config.saves, include_local_games=True),
        )
        path = tmp_path / "romcloud.toml"

        write_config(config, str(path))

        assert load_config(str(path)).saves.include_local_games is True

    def test_independent_smb_remote_data_round_trip(self, tmp_path: Path):
        config_path = tmp_path / "romcloud.toml"
        config = _base_config()
        config = AppConfig(
            **{
                **config.__dict__,
                "remote_data": RemoteDataConfig(
                    "smb",
                    "/userdata/romcloud/remote",
                    SMBConfig("data-nas", "ROMCloud", "sync-user"),
                ),
            }
        )
        write_config(config, str(config_path))

        loaded = load_config(str(config_path))

        assert loaded.remote_data.smb.server == "data-nas"
        assert loaded.remote_data.smb.share == "ROMCloud"

    def test_smb_remote_data_does_not_require_smb_rom_source(self, tmp_path: Path):
        config_path = tmp_path / "romcloud.toml"
        config = _base_config()
        config = AppConfig(
            **{
                **config.__dict__,
                "remote_data": RemoteDataConfig(
                    "smb",
                    "/userdata/romcloud/remote",
                    SMBConfig("data-nas", "ROMCloud", "writer"),
                ),
            }
        )

        write_config(config, str(config_path))
        loaded = load_config(str(config_path))

        assert loaded.smb is None
        assert loaded.remote_data.smb.server == "data-nas"

    def test_smb_remote_data_cannot_reuse_rom_library_share(self, tmp_path: Path):
        config_path = tmp_path / "romcloud.toml"
        config_path.write_text(
            '[source]\nprovider = "local"\nrom_root = "/mnt/roms"\n\n'
            '[smb]\nserver = "NAS.local"\nshare = "ROMs"\n\n'
            '[remote_data]\nprovider = "smb"\nroot = "/mnt/remote"\n\n'
            '[remote_data.smb]\nserver = "nas.LOCAL"\nshare = "roms"\n'
        )

        with pytest.raises(ConfigurationError, match="separate writable share"):
            load_config(str(config_path))

    def test_smb_remote_mount_cannot_alias_read_only_rom_mount(self, tmp_path: Path):
        config_path = tmp_path / "romcloud.toml"
        config_path.write_text(
            '[source]\nprovider = "local"\nrom_root = "/mnt/share"\n\n'
            '[smb]\nserver = "nas"\nshare = "ROMs"\n\n'
            '[remote_data]\nprovider = "smb"\nroot = "/mnt/share"\n\n'
            '[remote_data.smb]\nserver = "data-nas"\nshare = "ROMCloud"\n'
        )

        with pytest.raises(ConfigurationError, match="must not overlap"):
            load_config(str(config_path))

    def test_smb_remote_mount_cannot_be_nested_under_rom_mount(self, tmp_path: Path):
        config_path = tmp_path / "romcloud.toml"
        config_path.write_text(
            '[source]\nprovider = "local"\nrom_root = "/mnt/share"\n\n'
            '[smb]\nserver = "nas"\nshare = "ROMs"\n\n'
            '[remote_data]\nprovider = "smb"\nroot = "/mnt/share/data"\n\n'
            '[remote_data.smb]\nserver = "data-nas"\nshare = "ROMCloud"\n'
        )

        with pytest.raises(ConfigurationError, match="must not overlap"):
            load_config(str(config_path))

    def test_remote_data_requires_explicit_absolute_root(self, tmp_path: Path):
        config_path = tmp_path / "romcloud.toml"
        config_path.write_text(
            '[source]\nprovider = "local"\nrom_root = "/mnt/share"\n\n'
            '[remote_data]\nprovider = "local"\nroot = "relative"\n'
        )

        with pytest.raises(ConfigurationError, match="explicit absolute"):
            load_config(str(config_path))

    @pytest.mark.parametrize("remote_root", ["/roms/data", "/roms", "/"])
    def test_local_remote_data_cannot_overlap_rom_source(
        self, tmp_path: Path, remote_root: str
    ):
        config_path = tmp_path / "romcloud.toml"
        config_path.write_text(
            '[source]\nprovider = "local"\nrom_root = "/roms"\n\n'
            '[cache]\npath = "/cache"\n\n'
            '[data]\npath = "/state"\n\n'
            '[remote_data]\nprovider = "local"\n'
            f'root = "{remote_root}"\n'
        )

        with pytest.raises(ConfigurationError, match="must not overlap source"):
            load_config(str(config_path))

    def test_remote_data_cannot_overlap_local_save_source(self, tmp_path: Path):
        config_path = tmp_path / "romcloud.toml"
        config_path.write_text(
            '[source]\nprovider = "local"\nrom_root = "/roms"\n\n'
            '[cache]\npath = "/cache"\n\n'
            '[data]\npath = "/state"\n\n'
            '[remote_data]\nprovider = "local"\nroot = "/sync"\n\n'
            '[saves]\nlocal_path = "/sync/saves"\n'
        )

        with pytest.raises(ConfigurationError, match="saves.local_path"):
            load_config(str(config_path))
