"""Unit tests for `romcloud configure`'s source-type wording and behavior.

The wizard must ask for a *source type* (Local/USB vs SMB network share)
rather than exposing the internal provider abstraction, and must always
persist `provider = "local"` — even when the user selects the SMB source
type — while still writing the `[smb]` section and credentials.
"""

from __future__ import annotations

from pathlib import Path

from click.testing import CliRunner

from romcloud.cli.commands.configure import configure_cmd
from romcloud.infrastructure.config import load_config


def _run(cfg_path: Path, args: list[str]):
    runner = CliRunner()
    return runner.invoke(configure_cmd, args, obj={"config_path": str(cfg_path)})


class TestWordingDoesNotExposeProviderAbstraction:
    def test_help_text_uses_source_type_not_provider(self):
        result = CliRunner().invoke(configure_cmd, ["--help"])
        assert result.exit_code == 0
        assert "--source-type" in result.output
        assert "--provider" not in result.output
        assert "provider" not in result.output.lower()


class TestSourceTypeNonInteractive:
    def test_smb_source_type_persists_provider_local_and_smb_section(self, tmp_path):
        cfg_path = tmp_path / "romcloud.toml"
        result = _run(
            cfg_path,
            [
                "--non-interactive",
                "--source-type",
                "smb",
                "--rom-root",
                "/userdata/romcloud-source",
            ],
        )
        assert result.exit_code == 0, result.output

        config = load_config(str(cfg_path))
        assert config.source.provider == "local"
        assert config.source.rom_root == "/userdata/romcloud-source"
        assert config.smb is not None

    def test_local_source_type_has_no_smb_section(self, tmp_path):
        cfg_path = tmp_path / "romcloud.toml"
        result = _run(
            cfg_path,
            ["--non-interactive", "--source-type", "local", "--rom-root", "/mnt/roms"],
        )
        assert result.exit_code == 0, result.output

        config = load_config(str(cfg_path))
        assert config.source.provider == "local"
        assert config.smb is None

    def test_smb_source_type_defaults_rom_root_to_mount_point(self, tmp_path):
        """Without --rom-root, an SMB source type must default to a sensible
        local mount point, not a share-relative path."""
        cfg_path = tmp_path / "romcloud.toml"
        result = _run(cfg_path, ["--non-interactive", "--source-type", "smb"])
        assert result.exit_code == 0, result.output

        config = load_config(str(cfg_path))
        assert config.source.rom_root == "/userdata/romcloud-source"

    def test_default_source_type_is_local(self, tmp_path):
        cfg_path = tmp_path / "romcloud.toml"
        result = _run(cfg_path, ["--non-interactive"])
        assert result.exit_code == 0, result.output

        config = load_config(str(cfg_path))
        assert config.source.provider == "local"
        assert config.smb is None
