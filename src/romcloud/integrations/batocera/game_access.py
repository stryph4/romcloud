"""Batocera-facing game access strategies.

Cache Mode owns signed ``.romcloud`` proxy files. Connected Mode owns one
directory symlink named ``ROMCloud`` inside each existing Batocera system
directory. The system directories and their other contents always remain
user-owned.
"""

from __future__ import annotations

import fcntl
import json
import os
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Iterable

from romcloud.bootstrap.container import Container
from romcloud.core.capabilities import Capability, CapabilityPolicy, OperatingMode
from romcloud.core.exceptions import (
    ConfigurationError,
    ModeTransitionError,
    ProviderNotReachableError,
)
from romcloud.core.progress import ProgressSink, emit_progress
from romcloud.infrastructure.atomic_file import atomic_write_text
from romcloud.infrastructure.config import AppConfig, paths_overlap
from romcloud.integrations.batocera.systems import BATOCERA_SYSTEMS

MANIFEST_FILENAME = "direct-links.json"
LINK_NAME = "ROMCloud"
MANIFEST_VERSION = 1


class DirectLinkConflictError(RuntimeError):
    """The reserved Connected Mode path is not a verified ROMCloud link."""


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
    save_sync_available: bool = False
    save_reconcile: dict | None = None
    # True only when this transition actually asked Batocera to restart ES
    # (a real mode change, never a same-mode re-entry). `--restart` is a
    # fire-and-forget external tool with no readiness signal ROMCloud can
    # poll, so callers must treat this as "a restart was requested" rather
    # than "ES has already finished reloading and is launch-ready".
    es_restarted: bool = False


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


def _verified_direct_link_snapshot(config: AppConfig) -> dict[str, str]:
    """Capture only links whose manifest ownership is currently provable."""
    records = _load_manifest(config)
    return {
        raw_path: target
        for raw_path, target in records.items()
        if _is_verified_link(Path(raw_path), target, records)
    }


def _restore_direct_link_snapshot(
    config: AppConfig, records: dict[str, str]
) -> None:
    """Restore a pre-transition verified link set without probing its target."""
    local_root = Path(_lexical_absolute(Path(config.local_roms_path)))
    for raw_path, target in records.items():
        path = Path(raw_path)
        try:
            Path(_lexical_absolute(path)).relative_to(local_root)
        except (OSError, ValueError) as exc:
            raise DirectLinkConflictError(
                f"Refusing to restore a Connected Mode link outside {local_root}: {path}"
            ) from exc
        if path.is_symlink():
            actual = os.readlink(path)
            if not os.path.isabs(actual):
                actual = os.path.abspath(os.path.join(os.fspath(path.parent), actual))
            if os.path.normpath(actual) != os.path.normpath(target):
                raise DirectLinkConflictError(
                    f"Connected Mode path changed during transition: {path}"
                )
            continue
        if path.exists():
            raise DirectLinkConflictError(
                f"Connected Mode path was replaced during transition: {path}"
            )
        os.symlink(target, path, target_is_directory=True)
    _write_manifest(config, records)


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


def reconcile_direct_links(
    config: AppConfig,
    systems: Iterable[str],
    *,
    progress: ProgressSink = None,
) -> DirectLinkReport:
    """Expose source system folders without taking ownership of ROM directories."""
    local_root = Path(config.local_roms_path)
    source_root = Path(config.source.rom_root)
    records = _load_manifest(config)
    desired: dict[str, str] = {}

    selected_systems = sorted(set(systems))
    emit_progress(
        progress,
        "operating_mode",
        "managed_entries",
        "running",
        "Restoring direct source access",
        current=0,
        total=len(selected_systems),
    )

    # Preflight every path before changing anything, so one conflict cannot
    # leave a partially switched library.
    for system in selected_systems:
        system_dir = local_root / system
        if not system_dir.is_dir() or system_dir.is_symlink():
            raise DirectLinkConflictError(
                f"Connected Mode requires an existing user-owned Batocera directory: {system_dir}"
            )
        target_path = source_root / system
        if not target_path.is_dir():
            raise DirectLinkConflictError(
                f"Connected Mode source system directory is unavailable: {target_path}"
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
                    f"Connected Mode path already exists and is not a verified ROMCloud-owned symlink: {link}"
                )
        elif link.exists():
            raise DirectLinkConflictError(
                f"Connected Mode path already exists and is not a ROMCloud-owned symlink: {link}"
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
                f"Previously managed Connected Mode path changed and was left untouched: {path}"
            )

    created = 0
    active: dict[str, str] = {}
    for index, (raw_path, target) in enumerate(desired.items(), start=1):
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
        emit_progress(
            progress,
            "operating_mode",
            "managed_entries",
            "running",
            f"Updating {link.parent.name}: {index:,} / {len(desired):,} systems",
            current=index,
            total=len(desired),
            metadata={"system": link.parent.name},
        )
    _write_manifest(config, active)
    emit_progress(
        progress,
        "operating_mode",
        "managed_entries",
        "success",
        "Direct source access restored",
        current=len(desired),
        total=len(desired),
        metadata={"created_links": created, "removed_links": removed},
    )
    return DirectLinkReport(created=created, removed=removed)


def reconcile_game_access(
    config: AppConfig,
    *,
    refresh_es: bool = True,
    render_library_metadata: bool = True,
) -> GameAccessReport:
    """Restore catalog-owned artifacts for the authoritative operating mode."""
    # Imported lazily to avoid a lifecycle/container import cycle.
    from romcloud.infrastructure.library_view import operating_mode
    from romcloud.lifecycle.manage import remove_owned_proxies, restore_owned_proxies

    mode = operating_mode(config)
    container = Container(
        config,
        operating_policy=CapabilityPolicy(config.game_access_mode, mode),
    )
    systems = [
        system
        for system in container.game_repo.list_systems()
        if system in BATOCERA_SYSTEMS
    ]
    if mode is OperatingMode.CONNECTED:
        report = reconcile_direct_links(config, systems)
        remove_owned_proxies(config)
        if (
            render_library_metadata
            and getattr(getattr(config, "library_sync", None), "enabled", False)
        ):
            container.library_sync.render_local()
        if refresh_es:
            _refresh_emulationstation(config, systems, mode=mode)
        return GameAccessReport(created=report.created, removed=report.removed)
    report = remove_direct_links(config)
    if mode is OperatingMode.OFFLINE:
        reconcile_library_presentation(config, offline=True)
    else:
        restore_owned_proxies(config)
    if (
        render_library_metadata
        and getattr(getattr(config, "library_sync", None), "enabled", False)
    ):
        container.library_sync.render_local()
    es_result = (
        _refresh_emulationstation(config, systems, mode=mode)
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


def _refresh_emulationstation(
    config: AppConfig,
    systems: Iterable[str],
    *,
    mode: OperatingMode | str | None = None,
):  # noqa: ANN202
    from romcloud.integrations.batocera.presentation import refresh_emulationstation

    return refresh_emulationstation(config, systems, mode=mode)


def _reload_emulationstation() -> bool:
    from romcloud.integrations.batocera.presentation import reload_emulationstation

    return reload_emulationstation()


def _valid_cached_game_ids(
    config: AppConfig, progress: ProgressSink = None
) -> set[str]:
    """Return game_ids that are actually locally playable right now.

    Driven entirely by complete cache entries (bulk-loaded once), never by
    which games happen to already have a ``.romcloud`` proxy registration —
    a cache-complete game with no prior proxy must still be considered
    playable. Both cache entries and catalog games are loaded in a single
    bulk query each; validating an individual game's resolved launch asset
    only ever touches the filesystem, never the database, so this scales
    with the number of *cached* games, not the whole catalog.
    """
    container = Container(config)
    entries = container.cache_repo.list_complete()
    games = {game.id: game for game in container.game_repo.list_all()}
    valid: set[str] = set()
    total = len(entries)
    emit_progress(
        progress,
        "operating_mode",
        "local_games",
        "running",
        "Finding games available on this device",
        current=0,
        total=total,
    )
    interval = max(1, total // 100) if total else 1
    for index, entry in enumerate(entries, start=1):
        if container.cache.is_valid_cached_entry(entry, games.get(entry.game_id)):
            valid.add(entry.game_id)
        if index == total or index % interval == 0:
            emit_progress(
                progress,
                "operating_mode",
                "local_games",
                "running",
                f"Checking local games: {index:,} / {total:,}",
                current=index,
                total=total,
                metadata={"playable": len(valid)},
            )
    emit_progress(
        progress,
        "operating_mode",
        "local_games",
        "success",
        f"{len(valid):,} games available on this device",
        current=total,
        total=total,
        metadata={"playable": len(valid)},
    )
    return valid


def reconcile_library_presentation(
    config: AppConfig, *, offline: bool
) -> LibraryPresentationReport:
    """Replace only owned proxies with the requested cache-backed view."""
    from romcloud.core.capabilities import Capability
    from romcloud.infrastructure.capabilities import capability_policy

    capability_policy(config).require(Capability.OFFLINE_MODE, "Change operating mode")
    from romcloud.lifecycle.manage import remove_owned_proxies, restore_owned_proxies

    visible_ids = (
        _valid_cached_game_ids(config)
        if offline
        # Full catalog, not just already-registered proxies — same fix as
        # `_apply_mode_presentation`'s Cache Mode branch, kept consistent.
        else {game.id for game in Container(config).game_repo.list_all()}
    )
    # Materialize the desired set before removing anything else, so an
    # already-correct proxy is never unlinked-then-recreated on a no-op
    # re-entry into the same presentation.
    restored = restore_owned_proxies(config, game_ids=visible_ids)
    removed = remove_owned_proxies(config, keep_game_ids=visible_ids)
    return LibraryPresentationReport(
        offline=offline,
        removed=removed,
        restored=restored,
        visible=len(visible_ids),
    )


def set_offline_library_mode(
    config: AppConfig, enabled: bool, progress: ProgressSink = None
) -> LibraryPresentationReport:
    """Compatibility adapter for cached-only presentation callers."""
    return set_operating_mode(
        config,
        OperatingMode.OFFLINE if enabled else OperatingMode.CACHE,
        progress=progress,
    )


@contextmanager
def _operating_mode_lock(config: AppConfig):  # noqa: ANN202
    path = Path(config.data_path) / ".operating-mode.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _render_library_metadata(config: AppConfig, container: Container) -> None:
    if getattr(getattr(config, "library_sync", None), "enabled", False):
        container.library_sync.render_local()


def _prepare_connected_source(config: AppConfig, progress: ProgressSink) -> Container:
    """Make only the configured primary source ready; never rediscover it."""
    from romcloud.infrastructure import mount_worker
    from romcloud.services.connections import mount_connections

    if paths_overlap(Path(config.source.rom_root), Path(config.local_roms_path)):
        raise ConfigurationError(
            "Connected Mode requires the configured source to be separate from "
            "Batocera's local ROM directory."
        )

    emit_progress(
        progress,
        "operating_mode",
        "connect",
        "running",
        "Reconnecting to the configured source",
    )
    source_mounts = tuple(
        target
        for target in mount_worker.configured_mounts(config)
        if target.credential_kind == "source"
    )
    if source_mounts:
        mount_connections(
            config,
            progress=progress,
            credential_kinds=frozenset({"source"}),
        )

    transition_policy = CapabilityPolicy(
        config.game_access_mode, OperatingMode.CONNECTED
    )
    container = Container(config, operating_policy=transition_policy)
    validate_source = getattr(container.provider, "validate_access", None)
    if validate_source is not None:
        source_probe = validate_source(config.source.rom_root)
        if not source_probe.ok:
            raise ProviderNotReachableError(
                source_probe.detail or "The configured ROM source is unavailable."
            )
    elif not container.provider.is_reachable(config.source.rom_root):
        raise ProviderNotReachableError("The configured ROM source is unavailable.")

    emit_progress(
        progress,
        "operating_mode",
        "connect",
        "success",
        "Configured source is available",
    )
    return container


def _apply_mode_presentation(
    config: AppConfig,
    mode: OperatingMode,
    *,
    progress: ProgressSink = None,
) -> tuple[LibraryPresentationReport, Container]:
    """Apply one database-backed representation without scanning the source."""
    from romcloud.lifecycle.manage import remove_owned_proxies, restore_owned_proxies

    container = Container(
        config,
        operating_policy=CapabilityPolicy(config.game_access_mode, mode),
    )
    systems = [
        system
        for system in container.game_repo.list_systems()
        if system in BATOCERA_SYSTEMS
    ]
    if mode is OperatingMode.CONNECTED:
        reconcile_direct_links(config, systems, progress=progress)
        removed = remove_owned_proxies(config)
        report = LibraryPresentationReport(
            offline=False,
            removed=removed,
            visible=container.game_repo.count(),
            save_sync_available=config.remote_data is not None,
        )
    else:
        visible_ids = (
            _valid_cached_game_ids(config, progress=progress)
            if mode is OperatingMode.OFFLINE
            # Cache Mode exposes the complete catalog, not just games that
            # already happen to have a proxy registration — a game with no
            # prior registration (e.g. an interrupted catalog refresh) must
            # still be created, not silently excluded from the visible set.
            else {game.id for game in container.game_repo.list_all()}
        )
        restored = restore_owned_proxies(
            config,
            game_ids=visible_ids,
            progress=progress,
        )
        # Desired proxies are materialized before direct links are removed, so
        # a failed write cannot strand a previously Connected presentation.
        remove_direct_links(config)
        removed = remove_owned_proxies(config, keep_game_ids=visible_ids)
        report = LibraryPresentationReport(
            offline=mode is OperatingMode.OFFLINE,
            removed=removed,
            restored=restored,
            visible=len(visible_ids),
            save_sync_available=(
                mode is not OperatingMode.OFFLINE and config.remote_data is not None
            ),
        )
    _render_library_metadata(config, container)
    return report, container


def _update_emulationstation(
    config: AppConfig,
    container: Container,
    mode: OperatingMode,
    progress: ProgressSink,
    *,
    restart: bool = True,
) -> None:
    """Regenerate the owned ES override, restarting ES only when *restart*.

    A restart is a visible, disruptive interruption — it must only happen
    when the managed presentation actually changed (a real mode
    transition, or a rollback reverting one), never for an idempotent
    re-entry into the mode that is already active.
    """
    emit_progress(
        progress,
        "operating_mode",
        "emulationstation",
        "running",
        "Updating EmulationStation",
    )
    _refresh_emulationstation(
        config,
        container.game_repo.list_systems(),
        mode=mode,
    )
    if restart:
        _reload_emulationstation()
    emit_progress(
        progress,
        "operating_mode",
        "emulationstation",
        "success",
        "EmulationStation updated",
    )


def set_operating_mode(
    config: AppConfig,
    mode: OperatingMode | str,
    *,
    progress: ProgressSink = None,
) -> LibraryPresentationReport:
    """Transactionally select Connected, Cache, or Offline Mode."""
    from romcloud.infrastructure.library_view import operating_mode, write_operating_mode

    requested = OperatingMode(mode)
    with _operating_mode_lock(config):
        previous = operating_mode(config)
        previous_links = (
            _verified_direct_link_snapshot(config)
            if previous is OperatingMode.CONNECTED
            else {}
        )
        emit_progress(
            progress,
            "operating_mode",
            "prepare",
            "running",
            f"Preparing {requested.value.title()} Mode",
        )
        presentation_attempted = False
        try:
            if requested is OperatingMode.CONNECTED:
                _prepare_connected_source(config, progress)
            presentation_attempted = True
            report, container = _apply_mode_presentation(
                config, requested, progress=progress
            )
            restart_requested = requested is not previous
            _update_emulationstation(
                config, container, requested, progress, restart=restart_requested
            )
            report = replace(report, es_restarted=restart_requested)
            if previous is not requested:
                emit_progress(
                    progress,
                    "operating_mode",
                    "finalize",
                    "running",
                    "Finalizing mode",
                )
                # Atomic state persistence is the transition commit point.
                write_operating_mode(config, requested)
            emit_progress(
                progress,
                "operating_mode",
                "complete",
                "success",
                f"{requested.value.title()} Mode is active",
            )
            return report
        except Exception as exc:
            if presentation_attempted:
                try:
                    if previous is OperatingMode.CONNECTED:
                        from romcloud.lifecycle.manage import remove_owned_proxies

                        remove_owned_proxies(config)
                        _restore_direct_link_snapshot(config, previous_links)
                        rollback_container = Container(
                            config,
                            operating_policy=CapabilityPolicy(
                                config.game_access_mode, previous
                            ),
                        )
                        _render_library_metadata(config, rollback_container)
                    else:
                        _rollback, rollback_container = _apply_mode_presentation(
                            config, previous
                        )
                    _update_emulationstation(
                        config, rollback_container, previous, None
                    )
                except Exception:
                    pass
            raise ModeTransitionError(
                f"ROMCloud could not enter {requested.value.title()} Mode and remains "
                f"in {previous.value.title()} Mode. Check the configured source and retry."
            ) from exc
