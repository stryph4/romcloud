"""Batocera-derived launch eligibility for ROM discovery.

This module mirrors the relevant parts of Batocera EmulationStation's
``SystemData`` configuration loading:

* choose one base ``es_systems.cfg`` using Batocera's precedence;
* apply ``es_systems_*.cfg`` overlays by system name and child tag; and
* use each effective system's ``<extension>`` values for both files and
  extension-bearing directory packages.

ROMCloud's own ``es_systems_romcloud.cfg`` overlay is deliberately excluded.
Its ``.romcloud`` extension describes ROMCloud's presentation layer, not a
native source-ROM type.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Mapping
from xml.etree import ElementTree as ET

from romcloud.core.exceptions import ROMCloudError
from romcloud.infrastructure.atomic_file import atomic_write_text
from romcloud.infrastructure.logging import get_logger

log = get_logger("batocera.system_registry")

USER_ES_CONFIG_DIR = Path("/userdata/system/configs/emulationstation")
SYSTEM_ES_CONFIG_DIR = Path("/usr/share/emulationstation")
LEGACY_ES_CONFIG_DIR = Path("/etc/emulationstation")
ROMCLOUD_OVERLAY_NAME = "es_systems_romcloud.cfg"
REGISTRY_CACHE_FILENAME = "batocera-system-registry.json"
_CACHE_VERSION = 2
_LIST_SEPARATOR = re.compile(r"[\s,]+")


class SystemRegistryError(ROMCloudError):
    """No trustworthy live or last-known-good system registry is available."""


@dataclass(frozen=True)
class SystemLaunchSpec:
    name: str
    extensions: frozenset[str]
    command: str = ""

    def accepts(self, name: str) -> bool:
        """Match Batocera's case-insensitive launch eligibility.

        Batocera rejects a system definition with an empty command even if
        it has extensions, so such a definition cannot positively authorize
        a source candidate.
        """
        suffix = Path(name).suffix.casefold()
        return bool(self.command and suffix and suffix in self.extensions)


@dataclass(frozen=True)
class EffectiveSystemRegistry:
    systems: Mapping[str, SystemLaunchSpec]
    from_last_known_good: bool = False

    def get(self, system: str) -> SystemLaunchSpec | None:
        return self.systems.get(system)

    @property
    def names(self) -> frozenset[str]:
        return frozenset(self.systems)

    @classmethod
    def from_extensions(
        cls, systems: Mapping[str, Iterable[str]]
    ) -> "EffectiveSystemRegistry":
        """Build an explicit registry, primarily for adapters and tests."""
        return cls(
            {
                name: SystemLaunchSpec(
                    name, _normalize_extensions(extensions), command="launch"
                )
                for name, extensions in systems.items()
            }
        )


def load_effective_system_registry(
    *,
    cache_path: Path,
    user_config_dir: Path = USER_ES_CONFIG_DIR,
    system_config_dir: Path = SYSTEM_ES_CONFIG_DIR,
    legacy_config_dir: Path = LEGACY_ES_CONFIG_DIR,
) -> EffectiveSystemRegistry:
    """Load Batocera's effective registry, falling back to persisted LKG.

    A malformed/unreadable live configuration never produces a partial
    registry. The previous complete snapshot is used instead; without one,
    discovery fails closed.
    """
    try:
        registry = _load_live_registry(
            user_config_dir=user_config_dir,
            system_config_dir=system_config_dir,
            legacy_config_dir=legacy_config_dir,
        )
    except (OSError, ET.ParseError, ValueError) as exc:
        log.warning("Could not load live Batocera system registry: %s", exc)
    else:
        try:
            _write_cache(cache_path, registry)
        except OSError as exc:
            # A valid live registry remains authoritative even if refreshing
            # its disaster-recovery snapshot is temporarily impossible.
            log.warning("Could not persist Batocera system registry LKG: %s", exc)
        return registry

    cached = _read_cache(cache_path)
    if cached is not None:
        log.warning("Using last-known-good Batocera system registry at %s", cache_path)
        return EffectiveSystemRegistry(cached.systems, from_last_known_good=True)
    raise SystemRegistryError(
        "Batocera launch configuration is unavailable and no last-known-good "
        "system registry exists; source discovery was skipped safely"
    )


def _load_live_registry(
    *, user_config_dir: Path, system_config_dir: Path, legacy_config_dir: Path
) -> EffectiveSystemRegistry:
    base_candidates = (
        user_config_dir / "es_systems_custom.cfg",
        user_config_dir / "es_systems.cfg",
        system_config_dir / "es_systems.cfg",
        legacy_config_dir / "es_systems.cfg",
    )
    base_path = next((path for path in base_candidates if path.is_file()), None)
    if base_path is None:
        raise FileNotFoundError("no Batocera es_systems.cfg base file was found")

    root = _parse_system_list(base_path)
    systems: dict[str, ET.Element] = {}
    order: list[str] = []
    for element in root.findall("system"):
        name = (element.findtext("name") or "").strip()
        if not name:
            continue
        systems[name] = _copy_element(element)
        order.append(name)

    # Match Batocera's root order (user, then shipped config) and preserve the
    # filesystem's enumeration order within each root. Batocera's
    # getDirContent() likewise returns readdir order rather than sorting.
    seen_roots: set[Path] = set()
    for config_dir in (user_config_dir, system_config_dir):
        resolved_dir = config_dir.resolve(strict=False)
        if resolved_dir in seen_roots or not config_dir.is_dir():
            continue
        seen_roots.add(resolved_dir)
        overlays = (
            path
            for path in config_dir.iterdir()
            if path.is_file()
            and path.name.startswith("es_systems_")
            and path.suffix == ".cfg"
            and path.name != ROMCLOUD_OVERLAY_NAME
        )
        for overlay_path in overlays:
            overlay_root = _parse_system_list(overlay_path)
            for patch in overlay_root.findall("system"):
                name = (patch.findtext("name") or "").strip()
                if not name:
                    continue
                if name not in systems:
                    systems[name] = _copy_element(patch)
                    order.append(name)
                    continue
                target = systems[name]
                for child in list(patch):
                    if child.tag == "name":
                        continue
                    for old in list(target.findall(child.tag)):
                        target.remove(old)
                    text = (child.text or "").strip()
                    if child.tag == "emulators" or text:
                        target.append(_copy_element(child))

    specs: dict[str, SystemLaunchSpec] = {}
    for name in order:
        element = systems[name]
        extensions = _normalize_extensions(
            _LIST_SEPARATOR.split((element.findtext("extension") or "").strip())
        )
        specs[name] = SystemLaunchSpec(
            name,
            extensions,
            (element.findtext("command") or "").strip(),
        )
    if not specs:
        raise ValueError(f"{base_path} contains no named systems")
    return EffectiveSystemRegistry(specs)


def _parse_system_list(path: Path) -> ET.Element:
    root = ET.fromstring(path.read_text(encoding="utf-8"))
    if root.tag != "systemList":
        raise ValueError(f"{path} is missing the <systemList> root")
    return root


def _copy_element(element: ET.Element) -> ET.Element:
    return ET.fromstring(ET.tostring(element, encoding="unicode"))


def _normalize_extensions(extensions: Iterable[str]) -> frozenset[str]:
    return frozenset(
        extension.casefold()
        for extension in extensions
        if extension and extension.startswith(".")
    )


def _write_cache(path: Path, registry: EffectiveSystemRegistry) -> None:
    payload = {
        "version": _CACHE_VERSION,
        "systems": {
            name: {
                "extensions": sorted(spec.extensions),
                "command": spec.command,
            }
            for name, spec in sorted(registry.systems.items())
        },
    }
    atomic_write_text(path, json.dumps(payload, indent=2, sort_keys=True) + "\n")


def _read_cache(path: Path) -> EffectiveSystemRegistry | None:
    if not path.is_file() or path.is_symlink():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        raw_systems = payload["systems"]
        if payload.get("version") != _CACHE_VERSION or not isinstance(raw_systems, dict):
            return None
        systems: dict[str, SystemLaunchSpec] = {}
        for name, raw_spec in raw_systems.items():
            if not isinstance(name, str) or not isinstance(raw_spec, dict):
                return None
            raw_extensions = raw_spec.get("extensions")
            if not all(isinstance(value, str) for value in raw_extensions):
                return None
            command = raw_spec.get("command", "")
            if not isinstance(command, str):
                return None
            systems[name] = SystemLaunchSpec(
                name, _normalize_extensions(raw_extensions), command
            )
        if not systems:
            return None
        return EffectiveSystemRegistry(systems, from_last_known_good=True)
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None
