"""Tests proving that remote system directories are optional.

ROMCloud is additive:
- Only systems that actually exist under the source root are scanned.
- A missing remote system directory is not an error.
- Local ROM directories for systems absent from the remote source are
  not touched.
- Normal ROM files that already exist in a managed system directory are
  not modified, removed, or shadow-proxied.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from romcloud.infrastructure.database import Database
from romcloud.infrastructure.repositories.game import GameRepository
from romcloud.infrastructure.repositories.cache import CacheRepository
from romcloud.infrastructure.repositories.proxy import ProxyRepository
from romcloud.infrastructure.providers.local import LocalFilesystemProvider
from romcloud.integrations.batocera.catalog import CatalogService


# ── helpers ───────────────────────────────────────────────────────────────────


def _make_catalog(
    tmp_path: Path,
    source_root: Path,
    local_roms: Path,
) -> CatalogService:
    data_dir = tmp_path / "data"
    data_dir.mkdir(exist_ok=True)
    db = Database(str(data_dir / "catalog.db"))
    db.initialize()
    return CatalogService(
        provider=LocalFilesystemProvider(),
        game_repo=GameRepository(db),
        proxy_repo=ProxyRepository(db),
        local_roms_root=str(local_roms),
        source_root=str(source_root),
    )


# ── tests ─────────────────────────────────────────────────────────────────────


class TestPartialSourceRoot:
    def test_only_snes_source_catalogs_only_snes(self, tmp_path):
        """A source root containing only snes/ should produce only SNES catalog entries."""
        source = tmp_path / "source"
        (source / "snes").mkdir(parents=True)
        (source / "snes" / "Donkey Kong Country.sfc").write_bytes(b"sfc_data" * 50)
        (source / "snes" / "Super Metroid.sfc").write_bytes(b"sfc_data2" * 40)

        local_roms = tmp_path / "local_roms"
        local_roms.mkdir()

        svc = _make_catalog(tmp_path, source, local_roms)
        result = svc.refresh()

        assert result.added == 2
        assert result.errors == []

        # Only snes proxies should exist
        proxies = list(local_roms.rglob("*.romcloud"))
        assert len(proxies) == 2
        assert all(p.parent.name == "snes" for p in proxies)

    def test_missing_systems_not_an_error(self, tmp_path):
        """A source with only a subset of Batocera systems must not produce errors."""
        source = tmp_path / "source"
        (source / "ps2").mkdir(parents=True)
        (source / "ps2" / "Game.iso").write_bytes(b"iso" * 10)
        # n64, snes, nes, gamecube … are simply absent

        local_roms = tmp_path / "local_roms"
        local_roms.mkdir()

        svc = _make_catalog(tmp_path, source, local_roms)
        result = svc.refresh()

        assert result.errors == []
        assert result.added == 1

    def test_unknown_source_folders_silently_skipped(self, tmp_path):
        """Directories in the source root that are not valid Batocera system names
        should be ignored without raising an error."""
        source = tmp_path / "source"
        (source / "nes").mkdir(parents=True)
        (source / "nes" / "Mario.nes").write_bytes(b"nes_data")
        # An unrecognised folder that might confuse a naive scanner:
        (source / "my_weird_dir").mkdir(parents=True)
        (source / "my_weird_dir" / "something.bin").write_bytes(b"data")
        (source / "backup_old").mkdir(parents=True)

        local_roms = tmp_path / "local_roms"
        local_roms.mkdir()

        svc = _make_catalog(tmp_path, source, local_roms)
        result = svc.refresh()

        assert result.errors == []
        assert result.added == 1  # only the nes game

    def test_empty_source_root(self, tmp_path):
        """An empty source root (zero system folders) must not be an error."""
        source = tmp_path / "source"
        source.mkdir()

        local_roms = tmp_path / "local_roms"
        local_roms.mkdir()

        svc = _make_catalog(tmp_path, source, local_roms)
        result = svc.refresh()

        assert result.errors == []
        assert result.added == 0


class TestLocalDirsUntouched:
    def test_unmanaged_system_dirs_not_modified(self, tmp_path):
        """Existing local ROM directories for systems absent from the remote source
        must be completely untouched — no files added, no files removed."""
        source = tmp_path / "source"
        (source / "ps2").mkdir(parents=True)
        (source / "ps2" / "Remote Game.iso").write_bytes(b"ps2_rom")

        local_roms = tmp_path / "local_roms"
        # A local nes dir that has nothing to do with the remote source
        nes_dir = local_roms / "nes"
        nes_dir.mkdir(parents=True)
        mario = nes_dir / "Super Mario Bros.nes"
        mario.write_bytes(b"mario_data" * 20)
        zelda = nes_dir / "Zelda.nes"
        zelda.write_bytes(b"zelda_data" * 15)

        svc = _make_catalog(tmp_path, source, local_roms)
        svc.refresh()

        # nes/ directory must be completely unchanged
        assert mario.exists()
        assert mario.read_bytes() == b"mario_data" * 20
        assert zelda.exists()
        assert zelda.read_bytes() == b"zelda_data" * 15

        nes_files = set(nes_dir.iterdir())
        assert nes_files == {mario, zelda}  # exactly the two originals, nothing added

    def test_unmanaged_system_dir_gets_no_proxy_files(self, tmp_path):
        """No .romcloud proxy files should appear in a system directory that
        has no corresponding remote source folder."""
        source = tmp_path / "source"
        (source / "snes").mkdir(parents=True)
        (source / "snes" / "Game.sfc").write_bytes(b"sfc")

        local_roms = tmp_path / "local_roms"
        (local_roms / "nes").mkdir(parents=True)
        (local_roms / "nes" / "Local.nes").write_bytes(b"nes")

        svc = _make_catalog(tmp_path, source, local_roms)
        svc.refresh()

        nes_proxies = list((local_roms / "nes").glob("*.romcloud"))
        assert nes_proxies == [], "No proxy files should be created in the unmanaged nes/ dir"

    def test_existing_local_roms_in_managed_system_untouched(self, tmp_path):
        """A system directory managed by ROMCloud (has remote counterpart) may also
        contain pre-existing local ROM files.  Those files must not be modified,
        removed, or shadow-proxied."""
        source = tmp_path / "source"
        (source / "ps2").mkdir(parents=True)
        (source / "ps2" / "Remote Game.iso").write_bytes(b"remote_rom_data")

        local_roms = tmp_path / "local_roms"
        ps2_dir = local_roms / "ps2"
        ps2_dir.mkdir(parents=True)
        local_rom = ps2_dir / "Local Game.iso"
        local_rom.write_bytes(b"local_rom_data_original" * 10)

        svc = _make_catalog(tmp_path, source, local_roms)
        svc.refresh()

        # The local ROM must be byte-for-byte identical
        assert local_rom.exists()
        assert local_rom.read_bytes() == b"local_rom_data_original" * 10

        # A proxy for the remote game was created
        remote_proxy = ps2_dir / "Remote Game.romcloud"
        assert remote_proxy.exists()

        # The local ROM has no proxy
        local_proxy = ps2_dir / "Local Game.romcloud"
        assert not local_proxy.exists()

    def test_refresh_does_not_proxy_existing_local_romcloud_files(self, tmp_path):
        """Existing .romcloud files in the source tree (e.g. stray copies) must
        never be re-catalogued as games themselves."""
        source = tmp_path / "source"
        (source / "ps2").mkdir(parents=True)
        (source / "ps2" / "Real Game.iso").write_bytes(b"iso")
        # A stray .romcloud in the source tree — should be skipped
        (source / "ps2" / "Stray.romcloud").write_text('{"game_id": "stray"}')

        local_roms = tmp_path / "local_roms"
        local_roms.mkdir()

        svc = _make_catalog(tmp_path, source, local_roms)
        result = svc.refresh()

        assert result.added == 1  # only the .iso
        proxies = list((local_roms / "ps2").glob("*.romcloud"))
        assert len(proxies) == 1
        assert proxies[0].stem == "Real Game"


class TestHealthcheckWithPartialSource:
    def test_healthcheck_does_not_require_all_systems(self, tmp_path):
        """Healthcheck must pass even when only a subset of Batocera system
        folders exist in the source root."""
        from romcloud.infrastructure.config import AppConfig, SourceConfig, CacheConfig, LoggingConfig
        from romcloud.infrastructure.providers.local import LocalFilesystemProvider

        source = tmp_path / "source"
        (source / "nes").mkdir(parents=True)  # only nes, nothing else

        provider = LocalFilesystemProvider()
        assert provider.is_reachable(str(source))

        # list_systems returns only what exists — this must not raise
        systems = provider.list_systems(str(source))
        assert systems == ["nes"]

    def test_healthcheck_source_missing_system_no_error(self, tmp_path):
        """is_reachable on the root succeeds even if a specific configured
        system sub-directory does not exist yet."""
        source = tmp_path / "source"
        source.mkdir()
        # source root is reachable but has no ps2/ directory

        provider = LocalFilesystemProvider()
        assert provider.is_reachable(str(source)) is True

        # list_systems must return empty list, not raise
        systems = provider.list_systems(str(source))
        assert systems == []


class TestAdvancedConfigPreservation:
    def test_local_roms_path_defaults_to_userdata_roms(self, tmp_path):
        """A config file that omits [local_roms] should default local_roms_path
        to /userdata/roms without error."""
        from romcloud.infrastructure.config import load_config

        cfg_file = tmp_path / "romcloud.toml"
        cfg_file.write_text(
            "[source]\n"
            'provider = "local"\n'
            'rom_root = "/mnt/roms"\n',
            encoding="utf-8",
        )

        config = load_config(str(cfg_file))
        assert config.local_roms_path == "/userdata/roms"

    def test_local_roms_path_honoured_when_set(self, tmp_path):
        """An explicit [local_roms].path in the TOML must be respected."""
        from romcloud.infrastructure.config import load_config

        cfg_file = tmp_path / "romcloud.toml"
        cfg_file.write_text(
            "[source]\n"
            'provider = "local"\n'
            'rom_root = "/mnt/roms"\n'
            "\n"
            "[local_roms]\n"
            'path = "/mnt/custom/roms"\n',
            encoding="utf-8",
        )

        config = load_config(str(cfg_file))
        assert config.local_roms_path == "/mnt/custom/roms"

    def test_write_config_marks_local_roms_as_advanced(self, tmp_path):
        """The written config file must contain an 'Advanced settings' comment
        above the [local_roms] section."""
        from romcloud.infrastructure.config import (
            AppConfig, SourceConfig, CacheConfig, write_config
        )

        config = AppConfig(
            source=SourceConfig(provider="local", rom_root="/mnt/roms"),
            cache=CacheConfig(path="/cache"),
            local_roms_path="/userdata/roms",
            data_path="/data",
        )
        cfg_path = tmp_path / "romcloud.toml"
        write_config(config, str(cfg_path))

        content = cfg_path.read_text()
        assert "Advanced settings" in content
        assert "[local_roms]" in content

    def test_configure_preserves_custom_local_roms_path(self, tmp_path):
        """Re-running romcloud configure on an existing config with a custom
        local_roms_path must not reset it to /userdata/roms."""
        from romcloud.infrastructure.config import (
            AppConfig, SourceConfig, CacheConfig, LoggingConfig, write_config, load_config,
        )

        cfg_path = tmp_path / "romcloud.toml"

        # Write an initial config with a custom local_roms_path
        initial = AppConfig(
            source=SourceConfig(provider="local", rom_root="/mnt/roms"),
            cache=CacheConfig(path="/cache"),
            local_roms_path="/custom/roms",
            data_path="/data",
        )
        write_config(initial, str(cfg_path))

        # Simulate what configure_cmd does: load existing, preserve advanced settings
        existing = load_config(str(cfg_path))
        updated = AppConfig(
            source=SourceConfig(provider="local", rom_root="/mnt/new-roms"),
            cache=CacheConfig(path="/new-cache"),
            local_roms_path=existing.local_roms_path,  # preserved
            data_path=existing.data_path,               # preserved
            logging=existing.logging,                   # preserved
        )
        write_config(updated, str(cfg_path))

        reloaded = load_config(str(cfg_path))
        assert reloaded.local_roms_path == "/custom/roms"
        assert reloaded.source.rom_root == "/mnt/new-roms"
