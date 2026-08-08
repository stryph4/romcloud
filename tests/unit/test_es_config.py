"""Unit tests for romcloud.integrations.batocera.es_config (I/O layer).

Covers: install/refresh/status/remove, idempotency, never touching the
stock file or unrelated override files, and clear errors when the stock
file is missing.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from romcloud.integrations.batocera import es_config

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
