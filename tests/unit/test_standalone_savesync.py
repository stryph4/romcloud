from __future__ import annotations

from pathlib import Path

from romcloud.bootstrap.container import Container
from romcloud.infrastructure.config import (
    AppConfig,
    CacheConfig,
    RemoteDataConfig,
    SavesConfig,
    SourceConfig,
    load_config,
    write_config,
)


def _config(tmp_path: Path) -> AppConfig:
    local_roms = tmp_path / "local-roms"
    local_roms.mkdir()
    local_saves = tmp_path / "local-saves"
    local_saves.mkdir()
    remote = tmp_path / "remote-data"
    remote.mkdir()
    return AppConfig(
        source=SourceConfig("none", "", selected_systems=()),
        cache=CacheConfig(str(tmp_path / "unused-cache")),
        local_roms_path=str(local_roms),
        data_path=str(tmp_path / "data"),
        remote_data=RemoteDataConfig("local", str(remote)),
        saves=SavesConfig(local_path=str(local_saves)),
    )


def test_standalone_configuration_round_trips_without_fake_source(tmp_path: Path) -> None:
    path = tmp_path / "config" / "romcloud.toml"
    write_config(_config(tmp_path), str(path))

    loaded = load_config(str(path))

    assert loaded.game_management_enabled is False
    assert loaded.source.provider == "none"
    assert loaded.source.rom_root == ""
    assert "rom_root" not in path.read_text(encoding="utf-8").split("[game_access]")[0]


def test_standalone_container_upload_and_download_without_source_provider(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    service = Container(config).saves
    local_file = Path(config.saves.local_path) / "psx" / "Game.srm"
    local_file.parent.mkdir()
    local_file.write_bytes(b"local")

    service.commit_upload(service.preview_upload())
    remote_file = Path(config.remote_data.root) / "saves" / "psx" / "Game.srm"
    assert remote_file.read_bytes() == b"local"

    remote_file.write_bytes(b"remote")
    service.commit_download(service.preview_download())
    assert local_file.read_bytes() == b"remote"
