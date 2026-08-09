"""Unit tests for romcloud.integrations.batocera.ports_gamelist_config — the
I/O layer that reads/writes the real Ports `gamelist.xml` file.

Covers the four required scenarios: missing gamelist, existing gamelist
with unrelated games, an existing ROMCloud entry getting updated, and
idempotent repeated reconciliation.
"""

from __future__ import annotations

from pathlib import Path

from romcloud.integrations.batocera import ports_gamelist_config as cfg

_ICON = "/userdata/system/romcloud/ports-gfx/ports_gfx/assets/icon.png"


class TestMissingGamelist:
    def test_creates_file_and_returns_true(self, tmp_path: Path) -> None:
        gamelist_path = tmp_path / "ports" / "gamelist.xml"

        changed = cfg.reconcile(image_path=Path(_ICON), gamelist_path=gamelist_path)

        assert changed is True
        assert gamelist_path.exists()
        content = gamelist_path.read_text()
        assert "<path>./ROMCloud.sh</path>" in content
        assert f"<image>{_ICON}</image>" in content


class TestExistingGamelistWithUnrelatedGames:
    def test_unrelated_entry_preserved_and_romcloud_added(self, tmp_path: Path) -> None:
        gamelist_path = tmp_path / "ports" / "gamelist.xml"
        gamelist_path.parent.mkdir(parents=True)
        gamelist_path.write_text(
            """<?xml version="1.0"?>
<gameList>
  <game>
    <path>./SomeOtherPort.sh</path>
    <name>Some Other Port</name>
    <image>./images/some-other-port.png</image>
    <favorite>true</favorite>
  </game>
</gameList>
"""
        )

        changed = cfg.reconcile(image_path=Path(_ICON), gamelist_path=gamelist_path)

        assert changed is True
        content = gamelist_path.read_text()
        assert "<path>./SomeOtherPort.sh</path>" in content
        assert "<favorite>true</favorite>" in content
        assert "<path>./ROMCloud.sh</path>" in content
        assert f"<image>{_ICON}</image>" in content


class TestExistingRomcloudEntryUpdated:
    def test_stale_image_replaced_other_fields_preserved(self, tmp_path: Path) -> None:
        gamelist_path = tmp_path / "ports" / "gamelist.xml"
        gamelist_path.parent.mkdir(parents=True)
        gamelist_path.write_text(
            """<?xml version="1.0"?>
<gameList>
  <game>
    <path>./ROMCloud.sh</path>
    <name>ROMCloud</name>
    <image>/old/stale/icon.png</image>
    <playcount>3</playcount>
  </game>
</gameList>
"""
        )

        changed = cfg.reconcile(image_path=Path(_ICON), gamelist_path=gamelist_path)

        assert changed is True
        content = gamelist_path.read_text()
        assert "/old/stale/icon.png" not in content
        assert f"<image>{_ICON}</image>" in content
        assert "<playcount>3</playcount>" in content
        assert content.count("<game>") == 1


class TestRepeatedReconcileIsIdempotent:
    def test_second_call_reports_no_change(self, tmp_path: Path) -> None:
        gamelist_path = tmp_path / "ports" / "gamelist.xml"

        first = cfg.reconcile(image_path=Path(_ICON), gamelist_path=gamelist_path)
        content_after_first = gamelist_path.read_text()
        second = cfg.reconcile(image_path=Path(_ICON), gamelist_path=gamelist_path)

        assert first is True
        assert second is False
        assert gamelist_path.read_text() == content_after_first

    def test_idempotent_with_preexisting_unrelated_entries(self, tmp_path: Path) -> None:
        gamelist_path = tmp_path / "ports" / "gamelist.xml"
        gamelist_path.parent.mkdir(parents=True)
        gamelist_path.write_text(
            """<?xml version="1.0"?>
<gameList>
  <game>
    <path>./SomeOtherPort.sh</path>
    <name>Some Other Port</name>
    <image>./images/some-other-port.png</image>
  </game>
</gameList>
"""
        )

        cfg.reconcile(image_path=Path(_ICON), gamelist_path=gamelist_path)
        content_after_first = gamelist_path.read_text()
        second = cfg.reconcile(image_path=Path(_ICON), gamelist_path=gamelist_path)

        assert second is False
        assert gamelist_path.read_text() == content_after_first
