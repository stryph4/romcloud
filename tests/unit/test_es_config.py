"""Unit tests for romcloud.integrations.batocera.es_config (I/O layer).

Covers: install/refresh/status/remove, idempotency, never touching the
stock file or unrelated override files, and clear errors when the stock
file is missing.
"""

from __future__ import annotations

from pathlib import Path
from xml.etree import ElementTree as ET

import pytest

from romcloud.integrations.batocera import es_config
from romcloud.integrations.batocera.system_registry import (
    EffectiveSystemRegistry,
    SystemLaunchSpec,
    load_effective_system_registry,
)

_WRAPPER = Path("/userdata/system/romcloud/bin/romcloud-run")

_STOCK_XML = """<?xml version="1.0"?>
<systemList>
  <system>
    <name>snes</name>
    <fullname>Super Nintendo</fullname>
    <extension>.smc .sfc</extension>
    <command>emulatorlauncher -system %SYSTEM% -rom %ROM%</command>
  </system>
  <system>
    <name>ps2</name>
    <fullname>Playstation 2</fullname>
    <extension>.iso</extension>
    <command>emulatorlauncher -system %SYSTEM% -rom %ROM%</command>
  </system>
</systemList>
"""


@pytest.fixture
def stock_path(tmp_path: Path) -> Path:
    p = tmp_path / "es_systems.cfg"
    p.write_text(_STOCK_XML, encoding="utf-8")
    return p


@pytest.fixture
def override_path(tmp_path: Path) -> Path:
    return tmp_path / "configs" / "emulationstation" / "es_systems_romcloud.cfg"


class TestInstall:
    def test_writes_override_file(self, stock_path, override_path):
        es_config.install(
            ["snes"], stock_path=stock_path, override_path=override_path, wrapper_path=_WRAPPER
        )
        assert override_path.exists()
        assert "snes" in override_path.read_text()

    def test_only_managed_systems_written(self, stock_path, override_path):
        result = es_config.install(
            ["snes"], stock_path=stock_path, override_path=override_path, wrapper_path=_WRAPPER
        )
        assert result.included_systems == ["snes"]
        assert "ps2" not in override_path.read_text()

    def test_missing_stock_file_raises_clear_error(self, tmp_path, override_path):
        missing_stock = tmp_path / "does_not_exist.cfg"
        with pytest.raises(es_config.ESConfigError, match="not found"):
            es_config.install(
                ["snes"],
                stock_path=missing_stock,
                override_path=override_path,
                wrapper_path=_WRAPPER,
            )
        assert not override_path.exists()

    def test_effective_registry_can_install_user_added_system(
        self, tmp_path, override_path
    ):
        registry = EffectiveSystemRegistry(
            {
                "custom": SystemLaunchSpec(
                    "custom", frozenset({".foo"}),
                    "custom-launch -system %SYSTEM% -rom %ROM%",
                )
            }
        )

        result = es_config.install(
            ["custom"],
            stock_path=tmp_path / "missing-stock.cfg",
            override_path=override_path,
            wrapper_path=_WRAPPER,
            system_registry=registry,
        )

        assert result.included_systems == ["custom"]
        assert ".foo .romcloud" in override_path.read_text()


class TestIdempotency:
    def test_reinstall_produces_identical_bytes(self, stock_path, override_path):
        es_config.install(
            ["snes", "ps2"], stock_path=stock_path, override_path=override_path, wrapper_path=_WRAPPER
        )
        first_content = override_path.read_text()

        es_config.install(
            ["snes", "ps2"], stock_path=stock_path, override_path=override_path, wrapper_path=_WRAPPER
        )
        second_content = override_path.read_text()

        assert first_content == second_content

    def test_refresh_is_alias_for_install(self, stock_path, override_path):
        installed = es_config.install(
            ["snes"], stock_path=stock_path, override_path=override_path, wrapper_path=_WRAPPER
        )
        refreshed = es_config.refresh(
            ["snes"], stock_path=stock_path, override_path=override_path, wrapper_path=_WRAPPER
        )
        assert installed.xml == refreshed.xml


class TestNeverTouchesUnrelatedFiles:
    def test_stock_file_untouched(self, stock_path, override_path):
        original_stock = stock_path.read_text()
        es_config.install(
            ["snes"], stock_path=stock_path, override_path=override_path, wrapper_path=_WRAPPER
        )
        assert stock_path.read_text() == original_stock

    def test_unrelated_override_file_in_same_directory_untouched(self, stock_path, override_path):
        override_path.parent.mkdir(parents=True, exist_ok=True)
        unrelated = override_path.parent / "es_systems_user_custom.cfg"
        unrelated.write_text("<systemList><!-- user's own override --></systemList>")

        es_config.install(
            ["snes"], stock_path=stock_path, override_path=override_path, wrapper_path=_WRAPPER
        )
        es_config.remove(override_path=override_path)

        assert unrelated.read_text() == "<systemList><!-- user's own override --></systemList>"

    def test_remove_only_deletes_romcloud_override(self, stock_path, override_path):
        override_path.parent.mkdir(parents=True, exist_ok=True)
        unrelated = override_path.parent / "es_systems_user_custom.cfg"
        unrelated.write_text("keep me")

        es_config.install(
            ["snes"], stock_path=stock_path, override_path=override_path, wrapper_path=_WRAPPER
        )
        removed = es_config.remove(override_path=override_path)

        assert removed is True
        assert not override_path.exists()
        assert unrelated.exists()

    def test_remove_when_nothing_installed_is_noop(self, override_path):
        assert es_config.remove(override_path=override_path) is False


class TestStatus:
    def test_status_before_install(self, stock_path, override_path):
        st = es_config.status(
            ["snes"], stock_path=stock_path, override_path=override_path, wrapper_path=_WRAPPER
        )
        assert st.override_exists is False
        assert st.managed_systems == ["snes"]
        assert st.up_to_date is False

    def test_status_after_install_is_up_to_date(self, stock_path, override_path):
        es_config.install(
            ["snes"], stock_path=stock_path, override_path=override_path, wrapper_path=_WRAPPER
        )
        st = es_config.status(
            ["snes"], stock_path=stock_path, override_path=override_path, wrapper_path=_WRAPPER
        )
        assert st.override_exists is True
        assert st.systems_in_override == ["snes"]
        assert st.up_to_date is True

    def test_status_stale_after_managed_systems_change(self, stock_path, override_path):
        es_config.install(
            ["snes"], stock_path=stock_path, override_path=override_path, wrapper_path=_WRAPPER
        )
        st = es_config.status(
            ["snes", "ps2"], stock_path=stock_path, override_path=override_path, wrapper_path=_WRAPPER
        )
        assert st.up_to_date is False

    def test_status_reflects_wrapper_installed(self, stock_path, override_path, tmp_path):
        wrapper = tmp_path / "romcloud-run"

        st_before = es_config.status(
            ["snes"], stock_path=stock_path, override_path=override_path, wrapper_path=wrapper
        )
        assert st_before.wrapper_installed is False

        wrapper.write_text("#!/usr/bin/env python3\n")
        wrapper.chmod(0o755)
        st_after = es_config.status(
            ["snes"], stock_path=stock_path, override_path=override_path, wrapper_path=wrapper
        )
        assert st_after.wrapper_installed is True


_SWITCH_COMMAND = (
    "python /userdata/system/switch/configgen/switchlauncher.py "
    "%CONTROLLERSCONFIG% -gameinfoxml %GAMEINFOXML% -system %SYSTEM% "
    "-rom %ROM% -emulator %EMULATOR% -systemname %SYSTEMNAME%"
)


def _switch_registry(tmp_path: Path, user_dir: Path, share_dir: Path):
    return load_effective_system_registry(
        cache_path=tmp_path / "data" / "registry.json",
        user_config_dir=user_dir,
        system_config_dir=share_dir,
        legacy_config_dir=tmp_path / "missing-legacy",
    )


class TestThirdPartyOverlayPrecedence:
    def _layout(self, tmp_path: Path):
        user = tmp_path / "user"
        share = tmp_path / "share"
        user.mkdir()
        share.mkdir()
        stock = share / "es_systems.cfg"
        stock.write_text(
            "<systemList><system><name>switch</name>"
            "<extension>.nro .xci</extension>"
            "<command>emulatorlauncher -system %SYSTEM% -rom %ROM%</command>"
            "</system></systemList>",
            encoding="utf-8",
        )
        bua = user / "es_systems_switch.cfg"
        bua.write_text(
            "<systemList><system><name>switch</name><path>/userdata/roms/switch</path>"
            "<extension>.nro .NRO .xci .XCI .xcz .XCZ .nsp .NSP .nsz .NSZ "
            ".xci_config</extension>"
            f"<command>{_SWITCH_COMMAND}</command>"
            "</system></systemList>",
            encoding="utf-8",
        )
        return user, share, stock, bua, user / "es_systems_romcloud.cfg"

    def test_refresh_repairs_later_custom_overlay_and_tracks_restore(self, tmp_path):
        user, share, stock, bua, override = self._layout(tmp_path)
        registry = _switch_registry(tmp_path, user, share)

        es_config.install(
            ["switch"],
            stock_path=stock,
            override_path=override,
            wrapper_path=_WRAPPER,
            system_registry=registry,
        )

        root = ET.fromstring(bua.read_text(encoding="utf-8"))
        switch = root.find("system")
        assert switch is not None
        assert ".romcloud" in (switch.findtext("extension") or "").split()
        assert switch.findtext("command") == f"{_WRAPPER} {_SWITCH_COMMAND}"
        assert switch.findtext("path") == "/userdata/roms/switch"
        assert (user / "es_systems_romcloud.patches.json").exists()

        # Discovery still observes the native launcher/extensions while the
        # reversible repair is active.
        native = _switch_registry(tmp_path, user, share).get("switch")
        assert native is not None
        assert native.command == _SWITCH_COMMAND
        assert ".romcloud" not in native.extensions

    def test_remove_restores_native_fields_without_deleting_third_party_file(
        self, tmp_path
    ):
        user, share, stock, bua, override = self._layout(tmp_path)
        es_config.install(
            ["switch"],
            stock_path=stock,
            override_path=override,
            wrapper_path=_WRAPPER,
            system_registry=_switch_registry(tmp_path, user, share),
        )

        assert es_config.remove(override_path=override) is True

        root = ET.fromstring(bua.read_text(encoding="utf-8"))
        assert root.findtext("system/command") == _SWITCH_COMMAND
        assert ".romcloud" not in (root.findtext("system/extension") or "").split()
        assert root.findtext("system/path") == "/userdata/roms/switch"
        assert bua.exists()
        assert not override.exists()
        assert not (user / "es_systems_romcloud.patches.json").exists()

    def test_refresh_rebases_after_third_party_reinstall(self, tmp_path):
        user, share, stock, bua, override = self._layout(tmp_path)
        es_config.install(
            ["switch"],
            stock_path=stock,
            override_path=override,
            wrapper_path=_WRAPPER,
            system_registry=_switch_registry(tmp_path, user, share),
        )
        updated_command = _SWITCH_COMMAND + " --bua-new %NEWARG%"
        bua.write_text(
            "<systemList><system><name>switch</name><path>/custom/updated path</path>"
            "<extension>.xci .nsp .updated</extension>"
            f"<command>{updated_command}</command>"
            "</system></systemList>",
            encoding="utf-8",
        )

        es_config.refresh(
            ["switch"],
            stock_path=stock,
            override_path=override,
            wrapper_path=_WRAPPER,
            system_registry=_switch_registry(tmp_path, user, share),
        )
        patched = ET.fromstring(bua.read_text(encoding="utf-8"))
        assert patched.findtext("system/command") == f"{_WRAPPER} {updated_command}"
        assert ".updated" in (patched.findtext("system/extension") or "").split()
        assert ".romcloud" in (patched.findtext("system/extension") or "").split()

        es_config.remove(override_path=override)
        restored = ET.fromstring(bua.read_text(encoding="utf-8"))
        assert restored.findtext("system/command") == updated_command
        assert restored.findtext("system/extension") == ".xci .nsp .updated"
        assert restored.findtext("system/path") == "/custom/updated path"

    def test_refresh_and_remove_are_idempotent(self, tmp_path):
        user, share, stock, bua, override = self._layout(tmp_path)
        for _ in range(2):
            es_config.refresh(
                ["switch"],
                stock_path=stock,
                override_path=override,
                wrapper_path=_WRAPPER,
                system_registry=_switch_registry(tmp_path, user, share),
            )
        once = bua.read_bytes()
        es_config.refresh(
            ["switch"],
            stock_path=stock,
            override_path=override,
            wrapper_path=_WRAPPER,
            system_registry=_switch_registry(tmp_path, user, share),
        )
        assert bua.read_bytes() == once
        assert es_config.remove(override_path=override) is True
        assert es_config.remove(override_path=override) is False

    def test_remove_preserves_third_party_field_changed_after_refresh(self, tmp_path):
        user, share, stock, bua, override = self._layout(tmp_path)
        es_config.install(
            ["switch"],
            stock_path=stock,
            override_path=override,
            wrapper_path=_WRAPPER,
            system_registry=_switch_registry(tmp_path, user, share),
        )
        root = ET.fromstring(bua.read_text(encoding="utf-8"))
        command = root.find("system/command")
        assert command is not None
        command.text = "third-party-new-launcher -rom %ROM%"
        bua.write_text(ET.tostring(root, encoding="unicode"), encoding="utf-8")

        es_config.remove(override_path=override)

        restored = ET.fromstring(bua.read_text(encoding="utf-8"))
        assert restored.findtext("system/command") == "third-party-new-launcher -rom %ROM%"
        assert ".romcloud" not in (restored.findtext("system/extension") or "").split()

    def test_status_detects_third_party_overlay_overwrite(self, tmp_path):
        user, share, stock, bua, override = self._layout(tmp_path)
        es_config.install(
            ["switch"],
            stock_path=stock,
            override_path=override,
            wrapper_path=_WRAPPER,
            system_registry=_switch_registry(tmp_path, user, share),
        )
        assert es_config.status(
            ["switch"],
            stock_path=stock,
            override_path=override,
            wrapper_path=_WRAPPER,
            system_registry=_switch_registry(tmp_path, user, share),
        ).up_to_date
        bua.write_text(
            "<systemList><system><name>switch</name><extension>.xci</extension>"
            f"<command>{_SWITCH_COMMAND}</command></system></systemList>",
            encoding="utf-8",
        )

        assert not es_config.status(
            ["switch"],
            stock_path=stock,
            override_path=override,
            wrapper_path=_WRAPPER,
            system_registry=_switch_registry(tmp_path, user, share),
        ).up_to_date

    def test_refresh_migrates_old_manual_wrapper_and_direct_restores_native(
        self, tmp_path
    ):
        user, share, stock, bua, override = self._layout(tmp_path)
        legacy = _SWITCH_COMMAND.removeprefix("python ")
        root = ET.fromstring(bua.read_text(encoding="utf-8"))
        command = root.find("system/command")
        assert command is not None
        command.text = f"{_WRAPPER} {legacy}"
        bua.write_text(ET.tostring(root, encoding="unicode"), encoding="utf-8")

        es_config.refresh(
            ["switch"],
            stock_path=stock,
            override_path=override,
            wrapper_path=_WRAPPER,
            system_registry=_switch_registry(tmp_path, user, share),
        )
        patched = ET.fromstring(bua.read_text(encoding="utf-8"))
        assert patched.findtext("system/command") == f"{_WRAPPER} {_SWITCH_COMMAND}"

        es_config.remove(override_path=override)
        restored = ET.fromstring(bua.read_text(encoding="utf-8"))
        assert restored.findtext("system/command") == _SWITCH_COMMAND
