"""EmulationStation `es_systems` override generation — pure logic.

No I/O in this module: all functions take the stock ``es_systems.cfg`` XML
as a string and return the generated override XML as a string. This keeps
the transformation logic (extension/command rewriting, managed-system
selection) fully unit-testable without a Batocera filesystem.

See :mod:`romcloud.integrations.batocera.es_config` for the I/O layer that
reads the real stock file and writes ROMCloud's own override file.

Format (verified on Batocera 42)
---------------------------------
``/usr/share/emulationstation/es_systems.cfg`` is an XML document::

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
      ...
    </systemList>

Transformation applied per managed system
------------------------------------------
- ``<extension>``: the existing space-separated token list is kept as-is;
    ``.romcloud`` is appended only if no case-insensitive match for it is
    already present.
- ``<command>``: the ROMCloud wrapper is prepended to the complete native
    command. The executable (including interpreter/script pairs), every
    argument, and their order are preserved. The argument count is never
    assumed or hardcoded.

- Each generated system contains only ``<name>``, ``<extension>``, and
  ``<command>``. Batocera merges those fields over the matching stock system,
  so all other metadata remains inherited from Batocera rather than copied.
- Systems not present in *managed_systems* are omitted entirely.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable
from xml.etree import ElementTree as ET

_ROMCLOUD_EXTENSION = ".romcloud"

_GENERATED_HEADER = (
    "ROMCloud-managed EmulationStation system overrides.\n"
    "    Auto-generated — do not edit by hand; changes will be overwritten by\n"
    "    `romcloud es refresh`. Remove with `romcloud es remove`.\n"
    "    Only systems actually managed by the ROMCloud catalog are listed here;\n"
    "    every other Batocera system is untouched."
)


@dataclass(frozen=True)
class GeneratedOverride:
    """Result of generating the ROMCloud ``es_systems`` override."""

    xml: str
    included_systems: list[str]
    """Managed systems that were found in the stock file and included."""

    missing_systems: list[str]
    """Managed systems that were requested but not found in the stock file
    (e.g. a system name that doesn't match any Batocera stock definition)."""


def _extension_tokens(extension_text: str) -> list[str]:
    return (extension_text or "").split()


def _ensure_romcloud_extension(extension_text: str) -> str:
    tokens = _extension_tokens(extension_text)
    if any(t.lower() == _ROMCLOUD_EXTENSION for t in tokens):
        return extension_text.strip() if extension_text else _ROMCLOUD_EXTENSION
    tokens.append(_ROMCLOUD_EXTENSION)
    return " ".join(tokens)


def _rewrite_command(command_text: str, wrapper_path: str) -> str:
    command = (command_text or "").strip()
    if not command:
        return wrapper_path
    first = command.split(maxsplit=1)[0]
    if first == wrapper_path:
        remainder = command[len(first) :].lstrip()
        if not remainder:
            return command
        legacy_first = remainder.split(maxsplit=1)[0].strip("\"'")
        # Older ROMCloud releases replaced the native executable. Repair the
        # generic legacy shapes whose missing executable is unambiguous.
        if legacy_first.casefold().endswith(".py"):
            return f"{wrapper_path} python {remainder}"
        if legacy_first.startswith(("%", "-")):
            return f"{wrapper_path} emulatorlauncher {remainder}"
        # Already routed through the wrapper (e.g. re-processing our own
        # previous output) — leave every token untouched.
        return command
    return f"{wrapper_path} {command}"


def _parse_stock_systems(stock_xml: str) -> dict[str, ET.Element]:
    """Return ``{system_name: <system> element}`` for every system in the stock file."""
    root = ET.fromstring(stock_xml)
    systems: dict[str, ET.Element] = {}
    for system_el in root.findall("system"):
        name_el = system_el.find("name")
        if name_el is None or not (name_el.text or "").strip():
            continue
        systems[name_el.text.strip()] = system_el
    return systems


def generate_override(
    stock_xml: str,
    managed_systems: Iterable[str],
    wrapper_path: str,
) -> GeneratedOverride:
    """Generate the ROMCloud ``es_systems`` override XML.

    Deterministic and idempotent: the same *stock_xml* + *managed_systems*
    always produces byte-identical output, and only *managed_systems* that
    exist in *stock_xml* are included.
    """
    stock_systems = _parse_stock_systems(stock_xml)

    # Sorted for deterministic output regardless of input ordering/set iteration.
    requested = sorted(set(managed_systems))

    included: list[str] = []
    missing: list[str] = []

    out_root = ET.Element("systemList")
    out_root.append(ET.Comment(f" {_GENERATED_HEADER} "))

    for name in requested:
        stock_el = stock_systems.get(name)
        if stock_el is None:
            missing.append(name)
            continue

        # Named Batocera overlays merge individual fields for a matching
        # <name>. Keep this minimal so all unrelated stock metadata remains
        # inherited and follows future Batocera updates.
        system_el = ET.Element("system")
        ET.SubElement(system_el, "name").text = name
        stock_ext_el = stock_el.find("extension")
        ET.SubElement(system_el, "extension").text = _ensure_romcloud_extension(
            stock_ext_el.text if stock_ext_el is not None else ""
        )
        stock_cmd_el = stock_el.find("command")
        ET.SubElement(system_el, "command").text = _rewrite_command(
            stock_cmd_el.text if stock_cmd_el is not None else "", wrapper_path
        )

        out_root.append(system_el)
        included.append(name)

    ET.indent(out_root, space="  ")
    xml_body = ET.tostring(out_root, encoding="unicode")
    xml_text = '<?xml version="1.0"?>\n' + xml_body + "\n"

    return GeneratedOverride(xml=xml_text, included_systems=included, missing_systems=missing)


def generate_override_from_registry(
    registry, managed_systems: Iterable[str], wrapper_path: str
) -> GeneratedOverride:
    """Generate from Batocera's effective native system definitions.

    ``registry`` is intentionally duck-typed to keep this pure XML module
    independent of the filesystem loader. Unlike stock-only generation, this
    includes user-added systems and user-overridden extensions/commands.
    """
    requested = sorted(set(managed_systems))
    included: list[str] = []
    missing: list[str] = []
    out_root = ET.Element("systemList")
    out_root.append(ET.Comment(f" {_GENERATED_HEADER} "))
    for name in requested:
        spec = registry.get(name)
        if spec is None:
            missing.append(name)
            continue
        system_el = ET.Element("system")
        ET.SubElement(system_el, "name").text = name
        native_extensions = " ".join(sorted(spec.extensions))
        ET.SubElement(system_el, "extension").text = _ensure_romcloud_extension(
            native_extensions
        )
        ET.SubElement(system_el, "command").text = _rewrite_command(
            spec.command, wrapper_path
        )
        out_root.append(system_el)
        included.append(name)
    ET.indent(out_root, space="  ")
    xml = '<?xml version="1.0"?>\n' + ET.tostring(
        out_root, encoding="unicode"
    ) + "\n"
    return GeneratedOverride(xml, included, missing)


def parse_override_systems(override_xml: str) -> list[str]:
    """Return the list of system names present in a generated override document."""
    try:
        root = ET.fromstring(override_xml)
    except ET.ParseError:
        return []
    names = []
    for system_el in root.findall("system"):
        name_el = system_el.find("name")
        if name_el is not None and (name_el.text or "").strip():
            names.append(name_el.text.strip())
    return names
