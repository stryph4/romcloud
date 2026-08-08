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


# ── SMB discovery wizard integration ──────────────────────────────────────────
#
# `configure` must delegate all SMB discovery/authentication/validation to the
# reusable `SMBDiscoveryService` / `run_smb_setup_wizard` — never embed that
# logic directly in the Click command handler. These tests mock the wizard
# entry point exactly like a caller would (the wizard itself is covered in
# depth by test_smb_setup_wizard.py / test_smb_discovery_service.py).

from types import SimpleNamespace  # noqa: E402

import romcloud.cli.commands.configure as configure_cmd_module  # noqa: E402
from romcloud.infrastructure.credentials import load_smb_password  # noqa: E402


def _fake_setup_result(**overrides):
    defaults = dict(
        server="omnivault",
        port=445,
        share="Roms",
        username="stryph",
        password="hunter2",
        detected_systems=("psx", "dreamcast"),
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


class TestConfigureDelegatesToReusableSmbService:
    def test_successful_wizard_persists_config_and_credentials(self, tmp_path, monkeypatch):
        cfg_path = tmp_path / "romcloud.toml"

        monkeypatch.setattr(configure_cmd_module, "build_default_smb_discovery_service", lambda: "fake-discovery")
        monkeypatch.setattr(
            configure_cmd_module, "run_smb_setup_wizard", lambda discovery: _fake_setup_result()
        )

        runner = CliRunner()
        result = runner.invoke(
            configure_cmd,
            ["--source-type", "smb", "--rom-root", "/userdata/romcloud-source"],
            obj={"config_path": str(cfg_path)},
            input="/userdata/romcloud-cache\n50\n5\n",
        )

        assert result.exit_code == 0, result.output
        config = load_config(str(cfg_path))
        assert config.smb.server == "omnivault"
        assert config.smb.share == "Roms"
        assert config.smb.username == "stryph"

        creds_path = cfg_path.parent / "credentials.toml"
        assert load_smb_password(creds_path) == "hunter2"

    def test_wizard_receives_service_from_factory(self, tmp_path, monkeypatch):
        """The CLI must not construct discovery logic itself — it must use
        the shared factory and pass its result straight to the wizard."""
        cfg_path = tmp_path / "romcloud.toml"
        sentinel = object()
        received = {}

        monkeypatch.setattr(configure_cmd_module, "build_default_smb_discovery_service", lambda: sentinel)

        def fake_wizard(discovery):
            received["discovery"] = discovery
            return _fake_setup_result()

        monkeypatch.setattr(configure_cmd_module, "run_smb_setup_wizard", fake_wizard)

        CliRunner().invoke(
            configure_cmd,
            ["--source-type", "smb", "--rom-root", "/userdata/romcloud-source"],
            obj={"config_path": str(cfg_path)},
            input="/userdata/romcloud-cache\n50\n5\n",
        )

        assert received["discovery"] is sentinel


class TestConfigurePreservesExistingStateOnCancellation:
    def test_cancelled_wizard_leaves_existing_config_untouched(self, tmp_path, monkeypatch):
        cfg_path = tmp_path / "romcloud.toml"
        # Seed an existing, working local config.
        _run(cfg_path, ["--non-interactive", "--source-type", "local", "--rom-root", "/mnt/roms"])
        original_bytes = cfg_path.read_bytes()

        monkeypatch.setattr(configure_cmd_module, "build_default_smb_discovery_service", lambda: "fake-discovery")
        monkeypatch.setattr(configure_cmd_module, "run_smb_setup_wizard", lambda discovery: None)

        runner = CliRunner()
        result = runner.invoke(
            configure_cmd,
            ["--source-type", "smb", "--rom-root", "/userdata/romcloud-source"],
            obj={"config_path": str(cfg_path)},
            input="y\n",  # "Update it?" confirm, since an existing config was found
        )

        assert result.exit_code != 0
        assert cfg_path.read_bytes() == original_bytes
        assert not (cfg_path.parent / "credentials.toml").exists()

    def test_cancelled_wizard_never_writes_credentials(self, tmp_path, monkeypatch):
        cfg_path = tmp_path / "romcloud.toml"
        monkeypatch.setattr(configure_cmd_module, "build_default_smb_discovery_service", lambda: "fake-discovery")
        monkeypatch.setattr(configure_cmd_module, "run_smb_setup_wizard", lambda discovery: None)

        CliRunner().invoke(
            configure_cmd,
            ["--source-type", "smb", "--rom-root", "/userdata/romcloud-source"],
            obj={"config_path": str(cfg_path)},
        )

        assert not cfg_path.exists()
        assert not (cfg_path.parent / "credentials.toml").exists()


class TestConfigureTransactionalPersistence:
    def test_credentials_only_written_after_config_write_succeeds(self, tmp_path, monkeypatch):
        cfg_path = tmp_path / "romcloud.toml"
        monkeypatch.setattr(configure_cmd_module, "build_default_smb_discovery_service", lambda: "fake-discovery")
        monkeypatch.setattr(
            configure_cmd_module, "run_smb_setup_wizard", lambda discovery: _fake_setup_result()
        )

        write_calls = []
        monkeypatch.setattr(
            configure_cmd_module,
            "write_smb_password",
            lambda path, password: write_calls.append((path, password)),
        )

        def failing_write_config(config, path=None):
            raise RuntimeError("disk full")

        monkeypatch.setattr(configure_cmd_module, "write_config", failing_write_config)

        runner = CliRunner()
        result = runner.invoke(
            configure_cmd,
            ["--source-type", "smb", "--rom-root", "/userdata/romcloud-source"],
            obj={"config_path": str(cfg_path)},
            input="/userdata/romcloud-cache\n50\n5\n",
        )

        assert result.exit_code != 0
        assert write_calls == []  # credentials must never be written if config write failed

    def test_config_written_before_credentials(self, tmp_path, monkeypatch):
        cfg_path = tmp_path / "romcloud.toml"
        monkeypatch.setattr(configure_cmd_module, "build_default_smb_discovery_service", lambda: "fake-discovery")
        monkeypatch.setattr(
            configure_cmd_module, "run_smb_setup_wizard", lambda discovery: _fake_setup_result()
        )

        order = []
        real_write_config = configure_cmd_module.write_config

        def tracking_write_config(config, path=None):
            order.append("config")
            return real_write_config(config, path)

        def tracking_write_password(path, password):
            order.append("credentials")

        monkeypatch.setattr(configure_cmd_module, "write_config", tracking_write_config)
        monkeypatch.setattr(configure_cmd_module, "write_smb_password", tracking_write_password)

        CliRunner().invoke(
            configure_cmd,
            ["--source-type", "smb", "--rom-root", "/userdata/romcloud-source"],
            obj={"config_path": str(cfg_path)},
            input="/userdata/romcloud-cache\n50\n5\n",
        )

        assert order == ["config", "credentials"]

