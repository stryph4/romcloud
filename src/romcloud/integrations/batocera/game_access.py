"""Batocera-facing game access strategies.

Smart Cache owns signed ``.romcloud`` proxy files. Direct/NAS owns one
directory symlink named ``ROMCloud`` inside each existing Batocera system
directory. The system directories and their other contents always remain
user-owned.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from romcloud.bootstrap.container import Container
from romcloud.infrastructure.atomic_file import atomic_write_text
from romcloud.infrastructure.config import AppConfig, DIRECT_NAS_MODE
from romcloud.integrations.batocera.systems import BATOCERA_SYSTEMS

MANIFEST_FILENAME = "direct-links.json"
LINK_NAME = "ROMCloud"
MANIFEST_VERSION = 1


class DirectLinkConflictError(RuntimeError):
    """The reserved Direct/NAS path is not a verified ROMCloud link."""


@dataclass(frozen=True)
class DirectLinkReport:
    created: int = 0
    removed: int = 0


@dataclass(frozen=True)
class GameAccessReport:
    created: int = 0
    removed: int = 0
    es_included_systems: tuple[str, ...] = ()
    es_missing_systems: tuple[str, ...] = ()


@dataclass(frozen=True)
class LibraryPresentationReport:
    offline: bool
    removed: int = 0
    restored: int = 0
    visible: int = 0


def _manifest_path(config: AppConfig) -> Path:
    return Path(config.data_path) / MANIFEST_FILENAME


def _load_manifest(config: AppConfig) -> dict[str, str]:
    path = _manifest_path(config)
    if not path.is_file() or path.is_symlink():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if payload.get("version") != MANIFEST_VERSION or not isinstance(payload.get("links"), list):
        return {}
    records: dict[str, str] = {}
    for item in payload["links"]:
        if isinstance(item, dict) and isinstance(item.get("path"), str) and isinstance(item.get("target"), str):
            records[item["path"]] = item["target"]
    return records


def _write_manifest(config: AppConfig, records: dict[str, str]) -> None:
    path = _manifest_path(config)
    if not records:
        path.unlink(missing_ok=True)
        return
    payload = {
        "version": MANIFEST_VERSION,
        "links": [
            {"path": link, "target": records[link]}
            for link in sorted(records)
        ],
    }
    atomic_write_text(path, json.dumps(payload, indent=2) + "\n")


def _lexical_absolute(path: Path) -> str:
    return os.path.abspath(os.fspath(path))


def _is_verified_link(path: Path, target: str, records: dict[str, str]) -> bool:
    key = _lexical_absolute(path)
    if records.get(key) != target or not path.is_symlink():
        return False
    try:
        actual = os.readlink(path)
    except OSError:
        return False
    if not os.path.isabs(actual):
        actual = os.path.abspath(os.path.join(os.fspath(path.parent), actual))
    return os.path.normpath(actual) == os.path.normpath(target)


def remove_direct_links(config: AppConfig) -> DirectLinkReport:
    """Unlink only symlinks whose path and target match ROMCloud's manifest."""
    records = _load_manifest(config)
    removed = 0
    remaining: dict[str, str] = {}
    for raw_path, target in records.items():
        path = Path(raw_path)
        if _is_verified_link(path, target, records):
            path.unlink()
            removed += 1
        elif path.is_symlink() or path.exists():
            remaining[raw_path] = target
    _write_manifest(config, remaining)
    return DirectLinkReport(removed=removed)


def reconcile_direct_links(config: AppConfig, systems: Iterable[str]) -> DirectLinkReport:
    """Expose source system folders without taking ownership of ROM directories."""
    local_root = Path(config.local_roms_path)
    source_root = Path(config.source.rom_root)
    records = _load_manifest(config)
    desired: dict[str, str] = {}

    # Preflight every path before changing anything, so one conflict cannot
    # leave a partially switched library.
    for system in sorted(set(systems)):
        system_dir = local_root / system
        if not system_dir.is_dir() or system_dir.is_symlink():
            raise DirectLinkConflictError(
                f"Direct/NAS requires an existing user-owned Batocera directory: {system_dir}"
            )
        target_path = source_root / system
        if not target_path.is_dir():
            raise DirectLinkConflictError(
                f"Direct/NAS source system directory is unavailable: {target_path}"
            )
        link = system_dir / LINK_NAME
        key = _lexical_absolute(link)
        target = _lexical_absolute(target_path)
        desired[key] = target
        if link.is_symlink():
            recorded_target = records.get(key)
            if recorded_target is None or not _is_verified_link(
                link, recorded_target, records
            ):
                raise DirectLinkConflictError(
                    f"Direct/NAS path already exists and is not a verified ROMCloud-owned symlink: {link}"
                )
        elif link.exists():
            raise DirectLinkConflictError(
                f"Direct/NAS path already exists and is not a ROMCloud-owned symlink: {link}"
            )

    removed = 0
    for raw_path, target in records.items():
        if raw_path in desired:
            continue
        path = Path(raw_path)
        if _is_verified_link(path, target, records):
            path.unlink()
            removed += 1
        elif path.is_symlink() or path.exists():
            raise DirectLinkConflictError(
                f"Previously managed Direct/NAS path changed and was left untouched: {path}"
            )

    created = 0
    active: dict[str, str] = {}
    for raw_path, target in desired.items():
        link = Path(raw_path)
        recorded_target = records.get(raw_path)
        if (
            link.is_symlink()
            and recorded_target is not None
            and os.path.normpath(recorded_target) != os.path.normpath(target)
        ):
            # Preflight already verified this exact old path/target pair.
            link.unlink()
            removed += 1
        if not link.is_symlink():
            os.symlink(target, link, target_is_directory=True)
            created += 1
        active[raw_path] = target
    _write_manifest(config, active)
    return DirectLinkReport(created=created, removed=removed)


def reconcile_game_access(
    config: AppConfig, *, refresh_es: bool = True
) -> GameAccessReport:
    """Apply the configured strategy to catalog-owned Batocera artifacts."""
    # Imported lazily to avoid a lifecycle/container import cycle.
    from romcloud.infrastructure.library_view import (
        offline_library_enabled,
        write_offline_library_state,
    )
    from romcloud.lifecycle.manage import remove_owned_proxies, restore_owned_proxies

    container = Container(config)
    if config.game_access_mode == DIRECT_NAS_MODE:
        systems = [
            system
            for system in container.provider.list_systems(config.source.rom_root)
            if system in BATOCERA_SYSTEMS
        ]
        report = reconcile_direct_links(config, systems)
        remove_owned_proxies(config)
        # Offline Library Mode is Smart Cache-only. A successful Direct/NAS
        # transition deliberately resets the next Smart Cache presentation.
        write_offline_library_state(config, False)
        if getattr(getattr(config, "library_sync", None), "enabled", False):
            container.library_sync.render_local()
        if refresh_es:
            _refresh_emulationstation(config, container.game_repo.list_systems())
        return GameAccessReport(created=report.created, removed=report.removed)
    report = remove_direct_links(config)
    if offline_library_enabled(config):
        reconcile_library_presentation(config, offline=True)
    else:
        restore_owned_proxies(config)
    if getattr(getattr(config, "library_sync", None), "enabled", False):
        container.library_sync.render_local()
    es_result = (
        _refresh_emulationstation(config, container.game_repo.list_systems())
        if refresh_es
        else None
    )
    if es_result is None:
        return GameAccessReport(created=report.created, removed=report.removed)
    return GameAccessReport(
        created=report.created,
        removed=report.removed,
        es_included_systems=tuple(es_result.included_systems),
        es_missing_systems=tuple(es_result.missing_systems),
    )


def _refresh_emulationstation(config: AppConfig, systems: Iterable[str]):  # noqa: ANN202
    from romcloud.integrations.batocera.presentation import refresh_emulationstation

    return refresh_emulationstation(config, systems)


def _valid_cached_game_ids(config: AppConfig) -> set[str]:
    container = Container(config)
    return {
        record.game_id
        for record in container.proxy_repo.list_all()
        if container.cache.has_valid_cached_assets(record.game_id)
    }


def reconcile_library_presentation(
    config: AppConfig, *, offline: bool
) -> LibraryPresentationReport:
    """Replace only owned proxies with the requested Smart Cache view."""
    from romcloud.core.capabilities import Capability
    from romcloud.infrastructure.capabilities import capability_policy

    capability_policy(config).require(Capability.OFFLINE_MODE, "Change operating mode")
    from romcloud.lifecycle.manage import remove_owned_proxies, restore_owned_proxies

    visible_ids = _valid_cached_game_ids(config) if offline else None
    removed = remove_owned_proxies(config)
    restored = restore_owned_proxies(config, game_ids=visible_ids)
    return LibraryPresentationReport(
        offline=offline,
        removed=removed,
        restored=restored,
        visible=len(visible_ids) if visible_ids is not None else len(
            Container(config).proxy_repo.list_all()
        ),
    )


def set_offline_library_mode(
    config: AppConfig, enabled: bool
) -> LibraryPresentationReport:
    """Transactionally change persisted presentation state where practical."""
    from romcloud.infrastructure.library_view import (
        offline_library_enabled,
        write_offline_library_state,
    )

    from romcloud.core.capabilities import Capability
    from romcloud.infrastructure.capabilities import capability_policy

    capability_policy(config).require(Capability.OFFLINE_MODE, "Change operating mode")
    previous = offline_library_enabled(config)
    if previous == enabled:
        report = reconcile_library_presentation(config, offline=enabled)
        container = Container(config)
        if getattr(getattr(config, "library_sync", None), "enabled", False):
            container.library_sync.render_local()
        _refresh_emulationstation(config, container.game_repo.list_systems())
        return report
    try:
        report = reconcile_library_presentation(config, offline=enabled)
        write_offline_library_state(config, enabled)
        container = Container(config)
        if getattr(getattr(config, "library_sync", None), "enabled", False):
            container.library_sync.render_local()
        _refresh_emulationstation(config, container.game_repo.list_systems())
        return report
    except Exception:
        # Best-effort rollback restores the prior usable proxy presentation.
        try:
            reconcile_library_presentation(config, offline=previous)
            write_offline_library_state(config, previous)
            container = Container(config)
            if getattr(getattr(config, "library_sync", None), "enabled", False):
                container.library_sync.render_local()
            _refresh_emulationstation(config, container.game_repo.list_systems())
        except Exception:
            pass
        raise
