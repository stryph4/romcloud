"""Unit tests for `romcloud status` source-label presentation."""

from __future__ import annotations

from click.testing import CliRunner

from romcloud.cli.commands.status import status_cmd
from romcloud.infrastructure.config import AppConfig, CacheConfig, SMBConfig, SourceConfig


def _build_config(tmp_path, smb=None):
    source_root = tmp_path / "roms"
    source_root.mkdir()
    data_root = tmp_path / "data"
    data_root.mkdir()
    return AppConfig(
        source=SourceConfig(provider="local", rom_root=str(source_root)),
        cache=CacheConfig(path=str(tmp_path / "cache")),
        local_roms_path=str(tmp_path / "local_roms"),
        data_path=str(data_root),
        smb=smb,
    )


class TestSourcePresentation:
    def test_local_source_is_labeled_for_users(self, tmp_path):
        result = CliRunner().invoke(status_cmd, [], obj={"config": _build_config(tmp_path, smb=None)})

        assert result.exit_code == 0, result.output
        assert "Source:  Local filesystem" in result.output
        assert str(tmp_path / "roms") in result.output

    def test_smb_source_is_labeled_and_shows_safe_metadata(self, tmp_path):
        config = _build_config(tmp_path, smb=SMBConfig(server="nas.local", share="ROMs", username="alice"))
        result = CliRunner().invoke(status_cmd, [], obj={"config": config})

        assert result.exit_code == 0, result.output
        assert "Source:  SMB" in result.output
        assert "nas.local:ROMs" in result.output
        assert "alice" not in result.output
