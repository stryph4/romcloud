"""Regression tests for the SMB-provider hardware bug.

Real Batocera hardware crash reproduced here: choosing an SMB source in
`romcloud configure` used to set `source.provider = "smb"`, which made
`Container.provider` construct the still-unimplemented `SMBProvider` stub,
crashing `romcloud healthcheck` (and every other command) with
`NotImplementedError: SMB provider not yet implemented`.

The fix: mounted-SMB sources always resolve to `LocalFilesystemProvider`
(reading from wherever the share is mounted, at `source.rom_root`), while
the `[smb]` section is still persisted/loaded for the mount manager. The
native `SMBProvider` stub is never selected through this path.
"""

from __future__ import annotations

import pytest

from romcloud.bootstrap.container import Container
from romcloud.core.exceptions import ConfigurationError
from romcloud.core.providers.local import LocalFilesystemProvider
from romcloud.core.providers.smb import SMBProvider
from romcloud.infrastructure.config import (
    AppConfig,
    CacheConfig,
    SMBConfig,
    SourceConfig,
    load_config,
    write_config,
)


def _base_config(**overrides) -> AppConfig:
    defaults: dict = dict(
        source=SourceConfig(provider="local", rom_root="/mnt/roms"),
        cache=CacheConfig(path="/cache"),
        local_roms_path="/userdata/roms",
        data_path="/data",
    )
    defaults.update(overrides)
    return AppConfig(**defaults)


class TestConfigureNeverSelectsSmbProvider:
    """Step 1-2 of the reproduction: configure an SMB source; [smb] retained."""

    def test_smb_source_writes_provider_local_and_smb_section(self, tmp_path):
        config = _base_config(
            source=SourceConfig(provider="local", rom_root="/userdata/romcloud-source"),
            smb=SMBConfig(server="nas.local", share="ROMs", username="alice"),
        )
        cfg_path = tmp_path / "romcloud.toml"
        write_config(config, str(cfg_path))

        content = cfg_path.read_text()
        assert 'provider = "local"' in content
        assert "[smb]" in content
        assert 'server = "nas.local"' in content

        reloaded = load_config(str(cfg_path))
        assert reloaded.source.provider == "local"
        assert reloaded.smb is not None
        assert reloaded.smb.server == "nas.local"


class TestContainerResolvesLocalProvider:
    """Step 3 of the reproduction: the container must resolve LocalFilesystemProvider."""

    def test_smb_configured_source_resolves_to_local_provider(self):
        config = _base_config(
            source=SourceConfig(provider="local", rom_root="/userdata/romcloud-source"),
            smb=SMBConfig(server="nas.local", share="ROMs"),
        )
        container = Container(config)
        assert isinstance(container.provider, LocalFilesystemProvider)
        assert not isinstance(container.provider, SMBProvider)


class TestLegacySmbProviderMigration:
    """Requirement: existing provider="smb" configs must migrate or fail clearly."""

    def test_legacy_provider_smb_with_smb_section_migrates_to_local(self, tmp_path):
        cfg_path = tmp_path / "romcloud.toml"
        cfg_path.write_text(
            "[source]\n"
            'provider = "smb"\n'
            'rom_root = "/userdata/romcloud-source"\n'
            "\n"
            "[smb]\n"
            'server = "nas.local"\n'
            'share = "ROMs"\n',
            encoding="utf-8",
        )

        config = load_config(str(cfg_path))
        assert config.source.provider == "local"
        assert config.smb is not None
        assert config.smb.server == "nas.local"
        assert isinstance(Container(config).provider, LocalFilesystemProvider)

    def test_legacy_provider_smb_without_smb_section_fails_clearly(self, tmp_path):
        cfg_path = tmp_path / "romcloud.toml"
        cfg_path.write_text(
            "[source]\n"
            'provider = "smb"\n'
            'rom_root = "/userdata/romcloud-source"\n',
            encoding="utf-8",
        )
        with pytest.raises(ConfigurationError, match="smb"):
            load_config(str(cfg_path))

    def test_migration_reproduces_no_traceback_where_hardware_used_to_crash(self, tmp_path):
        """This exact shape of config used to crash with NotImplementedError."""
        cfg_path = tmp_path / "romcloud.toml"
        cfg_path.write_text(
            "[source]\n"
            'provider = "smb"\n'
            'rom_root = "/userdata/romcloud-source"\n'
            "\n"
            "[smb]\n"
            'server = "nas.local"\n'
            'share = "ROMs"\n',
            encoding="utf-8",
        )
        config = load_config(str(cfg_path))
        container = Container(config)
        # Must not raise NotImplementedError.
        assert container.provider.is_reachable(config.source.rom_root) in (True, False)


class TestHealthcheckDoesNotInvokeSmbProvider:
    """Step 4 of the reproduction."""

    def test_reachability_check_never_touches_smb_provider(self, tmp_path, monkeypatch):
        source_root = tmp_path / "roms"
        source_root.mkdir()

        config = _base_config(
            source=SourceConfig(provider="local", rom_root=str(source_root)),
            smb=SMBConfig(server="nas.local", share="ROMs"),
        )
        container = Container(config)

        def _boom(*args, **kwargs):
            raise AssertionError("SMBProvider must never be constructed")

        monkeypatch.setattr("romcloud.core.providers.smb.SMBProvider.__init__", _boom)

        # This is exactly what `romcloud healthcheck` calls.
        assert container.provider.is_reachable(str(source_root)) is True


class TestMountCommandsStillSeeSmbConfig:
    """Step 5 of the reproduction: mount commands must still see [smb]."""

    def test_smb_section_survives_round_trip_with_local_provider(self, tmp_path):
        config = _base_config(
            source=SourceConfig(provider="local", rom_root="/userdata/romcloud-source"),
            smb=SMBConfig(server="nas.local", share="ROMs", username="alice", port=445),
        )
        cfg_path = tmp_path / "romcloud.toml"
        write_config(config, str(cfg_path))
        reloaded = load_config(str(cfg_path))

        # Exactly what `romcloud mount status/start/stop` rely on.
        assert reloaded.smb is not None
        assert reloaded.smb.server == "nas.local"
        assert reloaded.smb.share == "ROMs"
        assert reloaded.smb.username == "alice"
        assert reloaded.source.rom_root == "/userdata/romcloud-source"

    def test_legacy_migrated_config_also_retains_smb_for_mount(self, tmp_path):
        cfg_path = tmp_path / "romcloud.toml"
        cfg_path.write_text(
            "[source]\n"
            'provider = "smb"\n'
            'rom_root = "/userdata/romcloud-source"\n'
            "\n"
            "[smb]\n"
            'server = "nas.local"\n'
            'share = "ROMs"\n'
            'username = "alice"\n',
            encoding="utf-8",
        )
        config = load_config(str(cfg_path))
        assert config.smb is not None
        assert config.smb.server == "nas.local"
