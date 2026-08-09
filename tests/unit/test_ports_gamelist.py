"""Unit tests for romcloud.integrations.batocera.ports_gamelist (pure XML logic).

Covers: creating a new gamelist.xml from scratch, preserving unrelated
`<game>` entries, updating an existing ROMCloud entry in place (matching by
path basename regardless of relative/absolute form), and determinism/
idempotency of reapplying the same inputs.
"""

from __future__ import annotations

from romcloud.integrations.batocera.ports_gamelist import (
    ROMCLOUD_GAME_NAME,
    ROMCLOUD_ROM_PATH,
    upsert_romcloud_entry,
)

_ICON = "/userdata/system/romcloud/ports-gfx/ports_gfx/assets/icon.png"

_EXISTING_WITH_UNRELATED = """<?xml version="1.0"?>
<gameList>
  <game>
    <path>./SomeOtherPort.sh</path>
    <name>Some Other Port</name>
    <image>./images/some-other-port.png</image>
    <favorite>true</favorite>
    <lastplayed>2026-01-01T00:00:00</lastplayed>
  </game>
</gameList>
"""


class TestMissingGamelist:
    def test_creates_new_document_with_romcloud_entry(self):
        result = upsert_romcloud_entry(None, image=_ICON)

        assert result.created is True
        assert "<gameList>" in result.xml
        assert f"<path>{ROMCLOUD_ROM_PATH}</path>" in result.xml
        assert f"<name>{ROMCLOUD_GAME_NAME}</name>" in result.xml
        assert f"<image>{_ICON}</image>" in result.xml

    def test_blank_string_treated_same_as_missing(self):
        result = upsert_romcloud_entry("", image=_ICON)
        assert result.created is True
        assert ROMCLOUD_ROM_PATH in result.xml


class TestUnrelatedEntriesPreserved:
    def test_existing_unrelated_game_untouched(self):
        result = upsert_romcloud_entry(_EXISTING_WITH_UNRELATED, image=_ICON)

        assert "<path>./SomeOtherPort.sh</path>" in result.xml
        assert "<name>Some Other Port</name>" in result.xml
        assert "<image>./images/some-other-port.png</image>" in result.xml
        assert "<favorite>true</favorite>" in result.xml
        assert "<lastplayed>2026-01-01T00:00:00</lastplayed>" in result.xml

    def test_romcloud_entry_appended_alongside_unrelated(self):
        result = upsert_romcloud_entry(_EXISTING_WITH_UNRELATED, image=_ICON)

        assert result.created is True
        assert result.xml.count("<game>") == 2
        assert f"<path>{ROMCLOUD_ROM_PATH}</path>" in result.xml
        assert f"<image>{_ICON}</image>" in result.xml


class TestExistingRomcloudEntryUpdated:
    def test_updates_image_and_preserves_other_fields(self):
        existing_xml = """<?xml version="1.0"?>
<gameList>
  <game>
    <path>./ROMCloud.sh</path>
    <name>ROMCloud</name>
    <image>/old/stale/icon.png</image>
    <favorite>true</favorite>
    <playcount>5</playcount>
  </game>
</gameList>
"""
        result = upsert_romcloud_entry(existing_xml, image=_ICON)

        assert result.created is False
        assert f"<image>{_ICON}</image>" in result.xml
        assert "/old/stale/icon.png" not in result.xml
        assert "<favorite>true</favorite>" in result.xml
        assert "<playcount>5</playcount>" in result.xml
        assert result.xml.count("<game>") == 1

    def test_matches_absolute_path_form_by_basename(self):
        existing_xml = """<?xml version="1.0"?>
<gameList>
  <game>
    <path>/userdata/roms/ports/ROMCloud.sh</path>
    <name>ROMCloud</name>
    <image>/old/icon.png</image>
  </game>
</gameList>
"""
        result = upsert_romcloud_entry(existing_xml, image=_ICON)

        assert result.created is False
        # Existing <path> form is preserved untouched; only name/image are written.
        assert "<path>/userdata/roms/ports/ROMCloud.sh</path>" in result.xml
        assert f"<image>{_ICON}</image>" in result.xml

    def test_unrelated_game_untouched_when_updating_romcloud_entry(self):
        existing_xml = """<?xml version="1.0"?>
<gameList>
  <game>
    <path>./SomeOtherPort.sh</path>
    <name>Some Other Port</name>
    <image>./images/some-other-port.png</image>
  </game>
  <game>
    <path>./ROMCloud.sh</path>
    <name>ROMCloud</name>
    <image>/old/icon.png</image>
  </game>
</gameList>
"""
        result = upsert_romcloud_entry(existing_xml, image=_ICON)

        assert result.created is False
        assert result.xml.count("<game>") == 2
        assert "<path>./SomeOtherPort.sh</path>" in result.xml
        assert "./images/some-other-port.png" in result.xml
        assert f"<image>{_ICON}</image>" in result.xml


class TestIdempotency:
    def test_reapplying_same_inputs_is_a_no_op(self):
        first = upsert_romcloud_entry(None, image=_ICON)
        second = upsert_romcloud_entry(first.xml, image=_ICON)

        assert second.xml == first.xml
        assert second.created is False

    def test_reapplying_after_unrelated_entry_stable(self):
        first = upsert_romcloud_entry(_EXISTING_WITH_UNRELATED, image=_ICON)
        second = upsert_romcloud_entry(first.xml, image=_ICON)

        assert second.xml == first.xml
