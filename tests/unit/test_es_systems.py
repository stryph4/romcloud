"""Unit tests for romcloud.integrations.batocera.es_systems (pure XML logic).

Covers: managed-system selection, extension-list preservation (+ idempotent
`.romcloud` append), dynamic command/argv preservation (no hardcoded arg
count), determinism/idempotency, and round-tripping the generated file.
"""

from __future__ import annotations

from romcloud.integrations.batocera.es_systems import (
    generate_override,
    parse_override_systems,
)

_WRAPPER = "/userdata/system/romcloud/bin/romcloud-run"

_STOCK_XML = """<?xml version="1.0"?>
<systemList>
  <system>
    <name>snes</name>
    <fullname>Super Nintendo</fullname>
    <path>/userdata/roms/snes</path>
    <extension>.smc .sfc .SMC .SFC .zip .7z</extension>
    <command>emulatorlauncher %CONTROLLERSCONFIG% -system %SYSTEM% -rom %ROM% -gameinfoxml %GAMEINFOXML% -systemname %SYSTEMNAME%</command>
    <platform>snes</platform>
    <theme>snes</theme>
  </system>
  <system>
    <name>ps2</name>
    <fullname>Playstation 2</fullname>
    <path>/userdata/roms/ps2</path>
    <extension>.iso .ISO .cso .CSO</extension>
    <command>emulatorlauncher %CONTROLLERSCONFIG% -system %SYSTEM% -rom %ROM% -gameinfoxml %GAMEINFOXML% -systemname %SYSTEMNAME% -extra %EXTRA%</command>
    <platform>ps2</platform>
    <theme>ps2</theme>
  </system>
  <system>
    <name>gba</name>
    <fullname>Game Boy Advance</fullname>
    <path>/userdata/roms/gba</path>
    <extension>.gba .GBA</extension>
    <command>emulatorlauncher -system %SYSTEM% -rom %ROM%</command>
    <platform>gba</platform>
    <theme>gba</theme>
  </system>
  <system>
    <name>already_has_romcloud</name>
    <fullname>Already Has Extension</fullname>
    <path>/userdata/roms/already_has_romcloud</path>
    <extension>.foo .ROMCLOUD</extension>
    <command>emulatorlauncher -system %SYSTEM% -rom %ROM%</command>
  </system>
</systemList>
"""


class TestManagedSystemSelection:
    def test_only_managed_systems_are_included(self):
        result = generate_override(_STOCK_XML, ["snes"], _WRAPPER)
        assert result.included_systems == ["snes"]
        assert "ps2" not in result.xml
        assert "gba" not in result.xml

    def test_multiple_managed_systems_included_and_sorted(self):
        result = generate_override(_STOCK_XML, ["ps2", "gba"], _WRAPPER)
        assert result.included_systems == ["gba", "ps2"]

    def test_unmanaged_system_not_present_at_all(self):
        """Non-ROMCloud systems must not appear in the override, so Batocera's
        stock definition (and normal ROM passthrough) governs them unchanged."""
        result = generate_override(_STOCK_XML, ["snes"], _WRAPPER)
        systems = parse_override_systems(result.xml)
        assert systems == ["snes"]

    def test_managed_system_missing_from_stock_is_reported_not_crashed(self):
        result = generate_override(_STOCK_XML, ["snes", "nonexistent_system"], _WRAPPER)
        assert result.included_systems == ["snes"]
        assert result.missing_systems == ["nonexistent_system"]

    def test_no_managed_systems_produces_empty_but_valid_document(self):
        result = generate_override(_STOCK_XML, [], _WRAPPER)
        assert result.included_systems == []
        assert parse_override_systems(result.xml) == []


class TestExtensionPreservation:
    def test_stock_extensions_preserved_verbatim(self):
        result = generate_override(_STOCK_XML, ["snes"], _WRAPPER)
        for ext in (".smc", ".sfc", ".SMC", ".SFC", ".zip", ".7z"):
            assert ext in result.xml

    def test_romcloud_extension_appended(self):
        result = generate_override(_STOCK_XML, ["snes"], _WRAPPER)
        assert ".romcloud" in result.xml

    def test_romcloud_extension_not_duplicated_if_already_present(self):
        result = generate_override(_STOCK_XML, ["already_has_romcloud"], _WRAPPER)
        extension_line = [
            line for line in result.xml.splitlines() if "<extension>" in line
        ][0]
        assert extension_line.lower().count(".romcloud") == 1


class TestCommandArgvPreservation:
    def test_executable_replaced_with_wrapper(self):
        result = generate_override(_STOCK_XML, ["snes"], _WRAPPER)
        assert f"<command>{_WRAPPER} %CONTROLLERSCONFIG%" in result.xml

    def test_all_original_arguments_preserved_in_order(self):
        result = generate_override(_STOCK_XML, ["snes"], _WRAPPER)
        expected_args = (
            "%CONTROLLERSCONFIG% -system %SYSTEM% -rom %ROM% "
            "-gameinfoxml %GAMEINFOXML% -systemname %SYSTEMNAME%"
        )
        assert expected_args in result.xml

    def test_does_not_hardcode_argument_count_shorter_command(self):
        """gba's stock command has fewer arguments than snes's — both must
        just have their leading token swapped, whatever the length."""
        result = generate_override(_STOCK_XML, ["gba"], _WRAPPER)
        assert f"<command>{_WRAPPER} -system %SYSTEM% -rom %ROM%</command>" in result.xml

    def test_does_not_hardcode_argument_count_longer_command(self):
        """ps2's stock command has an extra trailing argument — it must be
        preserved even though it doesn't appear in any other system."""
        result = generate_override(_STOCK_XML, ["ps2"], _WRAPPER)
        assert (
            f"<command>{_WRAPPER} %CONTROLLERSCONFIG% -system %SYSTEM% -rom %ROM% "
            "-gameinfoxml %GAMEINFOXML% -systemname %SYSTEMNAME% "
            "-extra %EXTRA%</command>" in result.xml
        )

    def test_non_command_fields_preserved(self):
        result = generate_override(_STOCK_XML, ["snes"], _WRAPPER)
        assert "<fullname>Super Nintendo</fullname>" in result.xml
        assert "<platform>snes</platform>" in result.xml
        assert "<theme>snes</theme>" in result.xml
        assert "<path>/userdata/roms/snes</path>" in result.xml


class TestDeterminismAndIdempotency:
    def test_generation_is_deterministic(self):
        first = generate_override(_STOCK_XML, ["snes", "ps2"], _WRAPPER)
        second = generate_override(_STOCK_XML, ["snes", "ps2"], _WRAPPER)
        assert first.xml == second.xml

    def test_generation_order_independent_of_input_order(self):
        a = generate_override(_STOCK_XML, ["ps2", "snes"], _WRAPPER)
        b = generate_override(_STOCK_XML, ["snes", "ps2"], _WRAPPER)
        assert a.xml == b.xml

    def test_regenerating_already_wrapped_command_does_not_double_wrap(self):
        """Feeding output that already routes through the wrapper back in
        (defensive — should never happen with the real stock file, which
        Batocera controls) must not prepend the wrapper twice."""
        once = generate_override(_STOCK_XML, ["gba"], _WRAPPER)
        twice = generate_override(once.xml, ["gba"], _WRAPPER)
        assert twice.included_systems == ["gba"]
        assert once.xml.split("<command>")[1] == twice.xml.split("<command>")[1]


class TestParseOverrideSystems:
    def test_round_trip(self):
        result = generate_override(_STOCK_XML, ["snes", "ps2", "gba"], _WRAPPER)
        assert set(parse_override_systems(result.xml)) == {"snes", "ps2", "gba"}

    def test_invalid_xml_returns_empty_list(self):
        assert parse_override_systems("<not valid xml") == []
