"""Reversible ownership for ROMCloud fields in third-party ES overlays.

Batocera applies ``es_systems_*.cfg`` files in filesystem enumeration order,
so a third-party overlay encountered after ROMCloud's file can replace the
managed ``extension`` and ``command`` fields.  ROMCloud repairs those two
fields in matching user overlays and records their prior XML here.  Removal
restores a field only while its current value still equals ROMCloud's applied
value, preserving third-party updates made in the meantime.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Mapping
from xml.etree import ElementTree as ET

from romcloud.infrastructure.atomic_file import atomic_write_text

PATCH_STATE_NAME = "es_systems_romcloud.patches.json"
_STATE_VERSION = 1
_OWNED_TAGS = ("extension", "command")


def patch_state_path(config_dir: Path) -> Path:
    return config_dir / PATCH_STATE_NAME


def read_patch_state(path: Path) -> dict | None:
    if not path.is_file() or path.is_symlink():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    if payload.get("version") != _STATE_VERSION or not isinstance(
        payload.get("files"), dict
    ):
        return None
    return payload


def project_native_root(path: Path, root: ET.Element, state: dict | None) -> ET.Element:
    """Return an in-memory view with ROMCloud-owned fields projected away."""
    if state is None:
        return root
    file_state = state["files"].get(path.name)
    if not isinstance(file_state, dict):
        return root
    projected = _copy_element(root)
    systems = file_state.get("systems", {})
    if not isinstance(systems, dict):
        return root
    for system in projected.findall("system"):
        name = (system.findtext("name") or "").strip()
        system_state = systems.get(name)
        if not isinstance(system_state, dict):
            continue
        for tag in _OWNED_TAGS:
            field_state = system_state.get(tag)
            if isinstance(field_state, dict):
                _restore_field(system, tag, field_state)
    return projected


def restore_owned_patches(config_dir: Path, *, state_path: Path | None = None) -> bool:
    """Restore tracked fields without overwriting third-party modifications."""
    state_path = state_path or patch_state_path(config_dir)
    state = read_patch_state(state_path)
    if state is None:
        return False
    changed = False
    for filename, file_state in state["files"].items():
        if not isinstance(filename, str) or not isinstance(file_state, dict):
            continue
        path = config_dir / filename
        if not path.is_file() or path.is_symlink():
            continue
        try:
            root = ET.fromstring(path.read_text(encoding="utf-8"))
        except (OSError, ET.ParseError):
            continue
        file_changed = False
        systems = file_state.get("systems", {})
        for system in root.findall("system"):
            name = (system.findtext("name") or "").strip()
            system_state = systems.get(name) if isinstance(systems, dict) else None
            if not isinstance(system_state, dict):
                continue
            for tag in _OWNED_TAGS:
                field_state = system_state.get(tag)
                if isinstance(field_state, dict) and _restore_field(
                    system, tag, field_state
                ):
                    file_changed = True
        if file_changed:
            atomic_write_text(
                path, _serialize(root), mode=path.stat().st_mode & 0o777
            )
            changed = True
    state_path.unlink(missing_ok=True)
    return changed


def patch_user_overlays(
    config_dir: Path,
    *,
    override_path: Path,
    desired: Mapping[str, Mapping[str, str]],
    state_path: Path | None = None,
) -> int:
    """Patch conflicting user overlays and persist field-level restore data."""
    state_path = state_path or patch_state_path(config_dir)
    file_roots: dict[Path, ET.Element] = {}
    files_state: dict[str, dict] = {}
    if not config_dir.is_dir():
        return 0

    for path in config_dir.iterdir():
        if (
            not path.is_file()
            or path.is_symlink()
            or path == override_path
            or not path.name.startswith("es_systems_")
            or path.suffix != ".cfg"
        ):
            continue
        try:
            root = ET.fromstring(path.read_text(encoding="utf-8"))
        except (OSError, ET.ParseError):
            continue
        systems_state: dict[str, dict] = {}
        for system in root.findall("system"):
            name = (system.findtext("name") or "").strip()
            desired_fields = desired.get(name)
            if desired_fields is None:
                continue
            field_states: dict[str, dict] = {}
            for tag in _OWNED_TAGS:
                applied = desired_fields[tag]
                existing = list(system.findall(tag))
                originals = [
                    {
                        "xml": ET.tostring(element, encoding="unicode"),
                        "index": list(system).index(element),
                    }
                    for element in existing
                ]
                if tag == "command" and len(existing) == 1:
                    migrated_native = _legacy_native_baseline(
                        (existing[0].text or "").strip(), applied
                    )
                    if migrated_native is not None:
                        migrated = _copy_element(existing[0])
                        migrated.text = migrated_native
                        originals[0]["xml"] = ET.tostring(
                            migrated, encoding="unicode"
                        )
                index = originals[0]["index"] if originals else len(system)
                field_states[tag] = {
                    "originals": originals,
                    "applied": applied,
                }
                _set_field(system, tag, applied, index=index)
            systems_state[name] = field_states
        if systems_state:
            file_roots[path] = root
            files_state[path.name] = {"systems": systems_state}

    if not file_roots:
        state_path.unlink(missing_ok=True)
        return 0

    payload = {"version": _STATE_VERSION, "files": files_state}
    # Record ownership before modifying third-party files so an interrupted
    # refresh never leaves an untracked mutation.
    atomic_write_text(state_path, json.dumps(payload, indent=2, sort_keys=True) + "\n")
    for path, root in file_roots.items():
        atomic_write_text(path, _serialize(root), mode=path.stat().st_mode & 0o777)
    return len(file_roots)


def overlays_match(
    config_dir: Path,
    *,
    override_path: Path,
    desired: Mapping[str, Mapping[str, str]],
) -> bool:
    """Return whether every matching third-party overlay has owned values."""
    if not config_dir.is_dir():
        return True
    for path in config_dir.iterdir():
        if (
            not path.is_file()
            or path.is_symlink()
            or path == override_path
            or not path.name.startswith("es_systems_")
            or path.suffix != ".cfg"
        ):
            continue
        try:
            root = ET.fromstring(path.read_text(encoding="utf-8"))
        except (OSError, ET.ParseError):
            continue
        for system in root.findall("system"):
            desired_fields = desired.get((system.findtext("name") or "").strip())
            if desired_fields is None:
                continue
            if any(
                (system.findtext(tag) or "").strip() != desired_fields[tag]
                for tag in _OWNED_TAGS
            ):
                return False
    return True


def _restore_field(system: ET.Element, tag: str, state: dict) -> bool:
    current_fields = list(system.findall(tag))
    applied = state.get("applied")
    if len(current_fields) != 1 or not isinstance(applied, str):
        return False
    current = current_fields[0]
    if (current.text or "").strip() != applied:
        return False
    for element in list(system.findall(tag)):
        system.remove(element)
    originals = state.get("originals", [])
    if not isinstance(originals, list):
        return True
    for original in sorted(
        (value for value in originals if isinstance(value, dict)),
        key=lambda value: value.get("index", len(system)),
    ):
        original_xml = original.get("xml")
        if not isinstance(original_xml, str):
            continue
        try:
            restored = ET.fromstring(original_xml)
        except ET.ParseError:
            continue
        index = original.get("index", len(system))
        if not isinstance(index, int):
            index = len(system)
        system.insert(max(0, min(index, len(system))), restored)
    return True


def _set_field(system: ET.Element, tag: str, text: str, *, index: int) -> None:
    for element in list(system.findall(tag)):
        system.remove(element)
    element = ET.Element(tag)
    element.text = text
    system.insert(max(0, min(index, len(system))), element)


def _copy_element(element: ET.Element) -> ET.Element:
    return ET.fromstring(ET.tostring(element, encoding="unicode"))


def _legacy_native_baseline(current: str, applied: str) -> str | None:
    """Recover native argv when *applied* repaired an old wrapper command."""
    current_parts = current.split(maxsplit=1)
    applied_parts = applied.split(maxsplit=1)
    if len(current_parts) != 2 or len(applied_parts) != 2:
        return None
    if current_parts[0] != applied_parts[0]:
        return None
    inserted = applied_parts[1].split(maxsplit=1)
    if len(inserted) != 2 or inserted[1] != current_parts[1]:
        return None
    if inserted[0] not in {"python", "python3", "emulatorlauncher"}:
        return None
    return applied_parts[1]


def _serialize(root: ET.Element) -> str:
    ET.indent(root, space="  ")
    return '<?xml version="1.0"?>\n' + ET.tostring(root, encoding="unicode") + "\n"
