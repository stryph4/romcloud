"""Unit tests for romcloud.integrations.batocera.ports_gamelist_config — the
I/O layer that reads/writes the real Ports `gamelist.xml` file and copies
ROMCloud's icon into the Ports artwork directory.

Covers the four required gamelist scenarios (missing gamelist, existing
gamelist with unrelated games, an existing ROMCloud entry getting updated,
idempotent repeated reconciliation) plus icon-sync behavior: writes when
missing, idempotent no-op when unchanged, updates when the source changes,
and preserves unrelated files already in the images/ folder.
"""

from __future__ import annotations

from pathlib import Path

from romcloud.integrations.batocera import ports_gamelist_config as cfg

_ICON = cfg.ROMCLOUD_IMAGE_RELATIVE_PATH


class TestMissingGamelist:
    def test_creates_file_and_returns_true(self, tmp_path: Path) -> None:
        gamelist_path = tmp_path / "ports" / "gamelist.xml"

        changed = cfg.reconcile(image=_ICON, gamelist_path=gamelist_path)

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

        changed = cfg.reconcile(image=_ICON, gamelist_path=gamelist_path)

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

        changed = cfg.reconcile(image=_ICON, gamelist_path=gamelist_path)

        assert changed is True
        content = gamelist_path.read_text()
        assert "/old/stale/icon.png" not in content
        assert f"<image>{_ICON}</image>" in content
        assert "<playcount>3</playcount>" in content
        assert content.count("<game>") == 1

    def test_old_absolute_ports_gfx_path_migrated(self, tmp_path: Path) -> None:
        gamelist_path = tmp_path / "ports" / "gamelist.xml"
        gamelist_path.parent.mkdir(parents=True)
        gamelist_path.write_text(
            """<?xml version="1.0"?>
<gameList>
  <game>
    <path>./ROMCloud.sh</path>
    <name>ROMCloud</name>
    <image>/userdata/system/romcloud/ports-gfx/ports_gfx/assets/icon.png</image>
  </game>
</gameList>
"""
        )

        changed = cfg.reconcile(image=_ICON, gamelist_path=gamelist_path)

        assert changed is True
        content = gamelist_path.read_text()
        assert "/userdata/system/romcloud" not in content
        assert f"<image>{_ICON}</image>" in content


class TestRepeatedReconcileIsIdempotent:
    def test_second_call_reports_no_change(self, tmp_path: Path) -> None:
        gamelist_path = tmp_path / "ports" / "gamelist.xml"

        first = cfg.reconcile(image=_ICON, gamelist_path=gamelist_path)
        content_after_first = gamelist_path.read_text()
        second = cfg.reconcile(image=_ICON, gamelist_path=gamelist_path)

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

        cfg.reconcile(image=_ICON, gamelist_path=gamelist_path)
        content_after_first = gamelist_path.read_text()
        second = cfg.reconcile(image=_ICON, gamelist_path=gamelist_path)

        assert second is False
        assert gamelist_path.read_text() == content_after_first


class TestSyncIcon:
    def test_writes_icon_into_ports_images_folder(self, tmp_path: Path) -> None:
        source_icon = tmp_path / "source" / "icon.png"
        source_icon.parent.mkdir(parents=True)
        source_icon.write_bytes(b"fake-png-bytes")
        ports_dir = tmp_path / "ports"

        changed = cfg.sync_icon(source_icon=source_icon, ports_dir=ports_dir)

        assert changed is True
        dest = ports_dir / "images" / cfg.ROMCLOUD_IMAGE_FILENAME
        assert dest.read_bytes() == b"fake-png-bytes"

    def test_idempotent_when_content_unchanged(self, tmp_path: Path) -> None:
        source_icon = tmp_path / "source" / "icon.png"
        source_icon.parent.mkdir(parents=True)
        source_icon.write_bytes(b"fake-png-bytes")
        ports_dir = tmp_path / "ports"

        cfg.sync_icon(source_icon=source_icon, ports_dir=ports_dir)
        second = cfg.sync_icon(source_icon=source_icon, ports_dir=ports_dir)

        assert second is False

    def test_updates_when_source_content_changes(self, tmp_path: Path) -> None:
        source_icon = tmp_path / "source" / "icon.png"
        source_icon.parent.mkdir(parents=True)
        source_icon.write_bytes(b"old-bytes")
        ports_dir = tmp_path / "ports"
        cfg.sync_icon(source_icon=source_icon, ports_dir=ports_dir)

        source_icon.write_bytes(b"new-bytes")
        changed = cfg.sync_icon(source_icon=source_icon, ports_dir=ports_dir)

        assert changed is True
        dest = ports_dir / "images" / cfg.ROMCLOUD_IMAGE_FILENAME
        assert dest.read_bytes() == b"new-bytes"

    def test_unrelated_images_preserved(self, tmp_path: Path) -> None:
        source_icon = tmp_path / "source" / "icon.png"
        source_icon.parent.mkdir(parents=True)
        source_icon.write_bytes(b"fake-png-bytes")
        ports_dir = tmp_path / "ports"
        images_dir = ports_dir / "images"
        images_dir.mkdir(parents=True)
        (images_dir / "some-other-port.png").write_bytes(b"unrelated-art")

        cfg.sync_icon(source_icon=source_icon, ports_dir=ports_dir)

        assert (images_dir / "some-other-port.png").read_bytes() == b"unrelated-art"

