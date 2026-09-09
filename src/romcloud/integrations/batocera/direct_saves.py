"""Compatibility migration for retired Direct Save Storage installations.

ROMCloud no longer creates save bind mounts. This module only recognizes
manifests written by releases that did, reconciles the preserved local shadow
with normal SaveSync semantics, and restores Batocera's canonical local path.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Iterable

from romcloud.core.exceptions import ModeTransitionError
from romcloud.core.save_selection import SaveSelectionPolicy
from romcloud.infrastructure.atomic_file import atomic_write_text
from romcloud.infrastructure.config import AppConfig
from romcloud.infrastructure.diagnostics import correlated_operation, event as diagnostic_event

if TYPE_CHECKING:
    from romcloud.core.progress import ProgressSink
    from romcloud.services.saves import SaveSyncService

MANIFEST_FILENAME = "direct-save-routes.json"
MANIFEST_VERSION = 2
LEGACY_MANIFEST_VERSION = 1
SHADOW_DIRECTORY = "direct-save-local"

# Exact routes emitted by manifest versions 1 and 2. Keeping the table here
# prevents compatibility metadata from becoming an active runtime capability.
_LEGACY_ROUTE_ROOTS = {
    **{
        f"retroarch-root-{system}": system
        for system in (
            "atari2600", "atari5200", "atari7800", "colecovision", "fds",
            "gamegear", "gb", "gb2players", "gba", "gbc", "gbc2players",
            "intellivision", "jaguar", "jaguarcd", "lynx", "mastersystem",
            "megacd", "megadrive", "megadrive-msu", "neogeo", "neogeocd",
            "nes", "ngp", "ngpc", "odyssey2", "pcengine", "pcenginecd",
            "psx", "satellaview", "sega32x", "sg1000", "sgb", "sgb-msu1",
            "snes", "snes-msu1", "sufami", "supergrafx", "supervision",
            "vectrex", "virtualboy", "wswan", "wswanc",
        )
    },
    "mame-nvram": "mame/nvram",
    "mame-state": "mame/state",
    "pcsx2-legacy-states": "pcsx2/sstates",
    "pcsx2-states": "ps2/pcsx2/sstates",
    "ppsspp-savedata": "ppsspp/PSP/SAVEDATA",
    "ppsspp-states": "ppsspp/PPSSPP_STATE",
}


@dataclass(frozen=True)
class DirectSaveRoute:
    """One validated route read from a legacy manifest."""

    layout_id: str
    canonical_root: str
    local_path: Path
    remote_path: Path
    shadow_path: Path

    def to_dict(self) -> dict[str, str]:
        return {
            "layout_id": self.layout_id,
            "canonical_root": self.canonical_root,
            "local_path": str(self.local_path),
            "remote_path": str(self.remote_path),
            "shadow_path": str(self.shadow_path),
        }


@dataclass(frozen=True)
class DirectSaveMigrationReport:
    status: str
    routes: int = 0
    localized: int = 0
    conflict_ids: tuple[str, ...] = ()


class BindMountOperations:
    """Injectable boundary used only to identify and remove legacy mounts."""

    def unbind(self, target: Path) -> None:
        subprocess.run(
            ["umount", str(target)],
            check=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )

    def is_mount(self, target: Path) -> bool:
        return os.path.ismount(target)

    def is_owned(self, source: Path, target: Path) -> bool:
        try:
            return self.is_mount(target) and os.path.samefile(source, target)
        except OSError:
            return False


class LegacyDirectSaveMigration:
    """Idempotently retire manifests and materialize gameplay saves locally."""

    def __init__(
        self,
        config: AppConfig,
        policy: SaveSelectionPolicy,
        remote_root: Path | None,
        *,
        mount_operations: BindMountOperations | None = None,
    ) -> None:
        self._config = config
        self._policy = policy
        self._remote_root = Path(remote_root) if remote_root is not None else None
        self._mounts = mount_operations or BindMountOperations()
        self._manifest_path = Path(config.data_path) / MANIFEST_FILENAME
        self._shadow_root = Path(config.data_path) / SHADOW_DIRECTORY
        self._loaded_manifest_version: int | None = None

    @property
    def pending(self) -> bool:
        return os.path.lexists(self._manifest_path)

    @property
    def layout_ids(self) -> frozenset[str]:
        return frozenset(route.layout_id for route in self._load_manifest())

    @property
    def shadow_root(self) -> Path:
        return self._shadow_root

    @correlated_operation(
        "Legacy Direct Save migration",
        subsystem="savesync",
        source="legacy Direct Save compatibility",
    )
    def migrate(
        self,
        saves: SaveSyncService,
        *,
        progress: ProgressSink = None,
        allow_remote: bool = True,
    ) -> DirectSaveMigrationReport:
        if not self.pending:
            return DirectSaveMigrationReport("not-needed")
        diagnostic_event(
            "savesync", "direct_save_migration_started",
            "Legacy Direct Save migration started",
        )
        try:
            routes = self._load_manifest()
            diagnostic_event(
                "savesync", "direct_save_manifest_found",
                "Legacy Direct Save manifest found",
                metadata={"version": self._loaded_manifest_version, "routes": len(routes)},
            )
            return self._migrate_routes(
                saves, routes, progress=progress, allow_remote=allow_remote
            )
        except BaseException as exc:
            diagnostic_event(
                "savesync", "direct_save_migration_failed",
                "Legacy Direct Save migration stopped conservatively",
                level="ERROR",
                metadata={"error_type": type(exc).__name__},
            )
            raise

    def _migrate_routes(
        self,
        saves: SaveSyncService,
        routes: tuple[DirectSaveRoute, ...],
        *,
        progress: ProgressSink,
        allow_remote: bool,
    ) -> DirectSaveMigrationReport:
        from romcloud.services.auto_savesync import ActiveSessionStore

        active = ActiveSessionStore(Path(self._config.data_path)).active_layout_ids(
            self._policy
        )
        in_use = active.intersection(route.layout_id for route in routes)
        if in_use:
            raise ModeTransitionError(
                "Legacy Direct Save migration cannot run while a game is using: "
                + ", ".join(sorted(in_use))
            )

        remote_available = allow_remote and saves.is_remote_reachable()
        localized = 0
        conflict_ids: set[str] = set()
        for route in routes:
            owned_mount = self._mounts.is_owned(route.remote_path, route.local_path)
            mounted = self._mounts.is_mount(route.local_path)
            diagnostic_event(
                "savesync", "direct_save_route_inspected",
                "Legacy Direct Save route inspected",
                metadata={"layout_id": route.layout_id, "canonical_root": route.canonical_root},
            )
            diagnostic_event(
                "savesync", "direct_save_mount_state",
                "Legacy Direct Save mount state observed",
                metadata={"layout_id": route.layout_id, "mounted": mounted, "owned": owned_mount},
            )
            if mounted and not owned_mount:
                raise ModeTransitionError(
                    f"An unowned mount blocks legacy Direct Save migration: {route.local_path}"
                )

            shadow_exists = os.path.lexists(route.shadow_path)
            local_exists = os.path.lexists(route.local_path)
            diagnostic_event(
                "savesync", "direct_save_shadow_observed",
                "Legacy local save shadow observed",
                metadata={"layout_id": route.layout_id, "present": shadow_exists},
            )
            diagnostic_event(
                "savesync", "direct_save_local_observed",
                "Canonical local save path observed",
                metadata={"layout_id": route.layout_id, "present": local_exists, "mounted": mounted},
            )
            self._require_ordinary_tree(route.shadow_path, "legacy local save shadow")
            if not mounted:
                self._require_ordinary_tree(route.local_path, "canonical local save")
            if remote_available:
                self._require_ordinary_tree(route.remote_path, "legacy remote save")
            diagnostic_event(
                "savesync", "direct_save_remote_observed",
                "Legacy remote save availability observed",
                metadata={"layout_id": route.layout_id, "available": remote_available},
            )

            shadow_has_data = self._tree_has_data(route.shadow_path)
            local_has_data = not mounted and self._tree_has_data(route.local_path)
            if (
                shadow_has_data
                and local_has_data
                and self._tree_manifest(route.shadow_path)
                != self._tree_manifest(route.local_path)
            ):
                diagnostic_event(
                    "savesync", "direct_save_migration_conflict",
                    "Divergent canonical and shadow save trees were preserved",
                    level="WARNING",
                    metadata={"layout_id": route.layout_id, "kind": "local-shadow-divergence"},
                )
                raise ModeTransitionError(
                    "Legacy Direct Save migration found divergent canonical and "
                    f"shadow data for {route.layout_id}; both were preserved."
                )

            local_service = saves.with_local_root(
                self._shadow_root if shadow_exists else Path(self._config.saves.local_path)
            )
            if remote_available:
                if shadow_exists or (not mounted and local_has_data):
                    local_service.quick_sync(
                        progress=progress,
                        force_current_state=True,
                        include_layout_ids=frozenset({route.layout_id}),
                    )
                else:
                    # A partial activation with no shadow has no local side.
                    # Copy the sole known physical source without mutating it.
                    shadow_service = saves.with_local_root(self._shadow_root)
                    preview = shadow_service.preview_download(
                        layout_ids=frozenset({route.layout_id})
                    )
                    shadow_service.commit_download(
                        preview,
                        layout_ids=frozenset({route.layout_id}),
                        progress=progress,
                    )
                    shadow_exists = os.path.lexists(route.shadow_path)

                conflict_ids.update(
                    conflict.conflict_id
                    for conflict in saves.get_state().active_conflicts
                    if conflict.layout_id == route.layout_id
                )
            elif not shadow_exists and mounted:
                raise ModeTransitionError(
                    "Legacy Direct Save provider is unavailable and no local shadow "
                    f"exists for {route.layout_id}; routing state was preserved."
                )

            if owned_mount:
                self._mounts.unbind(route.local_path)
                diagnostic_event(
                    "savesync", "direct_save_mount_removed",
                    "Legacy Direct Save mount removed",
                    metadata={"layout_id": route.layout_id},
                )
            self._materialize_local(route)
            localized += 1
            diagnostic_event(
                "savesync", "direct_save_local_materialized",
                "Canonical local save namespace materialized",
                metadata={"layout_id": route.layout_id},
            )
            # A crash before this write is also safe: path inspection derives
            # the same phase from mount/shadow/canonical state.
            self._write_manifest("migration-localized", routes)

        if conflict_ids or not remote_available:
            if conflict_ids:
                diagnostic_event(
                    "savesync", "direct_save_migration_conflict",
                    "Legacy Direct Save divergence requires normal conflict resolution",
                    level="WARNING",
                    metadata={"conflicts": len(conflict_ids)},
                )
            return DirectSaveMigrationReport(
                "conflict" if conflict_ids else "provider-unavailable",
                routes=len(routes), localized=localized,
                conflict_ids=tuple(sorted(conflict_ids)),
            )

        result = saves.quick_sync(
            progress=progress,
            force_current_state=True,
            include_layout_ids=frozenset(route.layout_id for route in routes),
        )
        route_layout_ids = {route.layout_id for route in routes}
        final_conflicts = tuple(sorted(
            conflict.conflict_id
            for conflict in saves.get_state().active_conflicts
            if conflict.layout_id in route_layout_ids
        ))
        if final_conflicts or result.status != "reconciled":
            return DirectSaveMigrationReport(
                "conflict" if final_conflicts else result.status,
                routes=len(routes), localized=localized,
                conflict_ids=final_conflicts,
            )
        self._manifest_path.unlink()
        self._remove_empty_shadow_parents()
        diagnostic_event(
            "savesync", "direct_save_manifest_retired",
            "Legacy Direct Save manifest retired",
            metadata={"routes": len(routes)},
        )
        diagnostic_event(
            "savesync", "direct_save_migration_completed",
            "Legacy Direct Save migration completed",
            metadata={"routes": len(routes)},
        )
        return DirectSaveMigrationReport("completed", len(routes), localized)

    def _materialize_local(self, route: DirectSaveRoute) -> None:
        if self._mounts.is_mount(route.local_path):
            raise ModeTransitionError(
                f"Legacy Direct Save mount remained active: {route.local_path}"
            )
        if os.path.lexists(route.shadow_path):
            self._require_ordinary_tree(route.shadow_path, "legacy local save shadow")
            if os.path.lexists(route.local_path):
                self._require_ordinary_tree(route.local_path, "canonical local save")
                if self._tree_has_data(route.local_path):
                    if (
                        self._tree_manifest(route.local_path)
                        != self._tree_manifest(route.shadow_path)
                    ):
                        raise ModeTransitionError(
                            f"Canonical save path changed during migration: {route.local_path}"
                        )
                    return
                route.local_path.rmdir()
            route.local_path.parent.mkdir(parents=True, exist_ok=True)
            os.replace(route.shadow_path, route.local_path)
        else:
            route.local_path.mkdir(parents=True, exist_ok=True)
        self._require_ordinary_tree(route.local_path, "canonical local save")

    def _load_manifest(self) -> tuple[DirectSaveRoute, ...]:
        if not self.pending:
            return ()
        if self._manifest_path.is_symlink() or not self._manifest_path.is_file():
            raise ModeTransitionError(
                f"Legacy Direct Save manifest is invalid: {self._manifest_path}"
            )
        if self._remote_root is None:
            raise ModeTransitionError(
                "Legacy Direct Save manifest cannot be safely validated without its "
                "configured filesystem remote-data root."
            )
        try:
            payload = json.loads(self._manifest_path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("manifest must be an object")
            version = payload.get("version")
            if version not in {LEGACY_MANIFEST_VERSION, MANIFEST_VERSION}:
                raise ValueError("unsupported version")
            if payload.get("state") not in {
                "preparing", "active", "recovery-required", "migration-localized"
            }:
                raise ValueError("unsupported state")
            raw_routes = payload.get("routes")
            if not isinstance(raw_routes, list) or not raw_routes:
                raise ValueError("routes must be a non-empty list")
            routes = tuple(self._parse_route(item) for item in raw_routes)
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ModeTransitionError(
                f"Legacy Direct Save manifest is invalid: {self._manifest_path}"
            ) from exc
        ids = [route.layout_id for route in routes]
        if len(ids) != len(set(ids)):
            raise ModeTransitionError("Legacy Direct Save manifest contains duplicate routes.")
        for index, route in enumerate(routes):
            for other in routes[index + 1:]:
                if self._paths_overlap(route.local_path, other.local_path):
                    raise ModeTransitionError(
                        "Legacy Direct Save manifest contains overlapping routes: "
                        f"{route.layout_id}, {other.layout_id}"
                    )
        self._loaded_manifest_version = int(version)
        return routes

    def _parse_route(self, item: object) -> DirectSaveRoute:
        if not isinstance(item, dict):
            raise ValueError("route must be an object")
        keys = (
            "layout_id", "canonical_root", "local_path", "remote_path", "shadow_path"
        )
        values = {key: item[key] for key in keys}
        if not all(isinstance(value, str) and value for value in values.values()):
            raise ValueError("route fields must be non-empty strings")
        layout_id = values["layout_id"]
        canonical = values["canonical_root"]
        if _LEGACY_ROUTE_ROOTS.get(layout_id) != canonical:
            raise ValueError("unknown legacy route")
        self._policy.layout(layout_id)
        relative = Path(*canonical.split("/"))
        assert self._remote_root is not None
        expected = DirectSaveRoute(
            layout_id, canonical,
            Path(self._config.saves.local_path) / relative,
            self._remote_root / relative,
            self._shadow_root / relative,
        )
        actual = DirectSaveRoute(
            layout_id, canonical,
            Path(values["local_path"]), Path(values["remote_path"]),
            Path(values["shadow_path"]),
        )
        if actual != expected:
            raise ValueError("route ownership/path identity changed")
        return actual

    def _write_manifest(self, state: str, routes: Iterable[DirectSaveRoute]) -> None:
        atomic_write_text(
            self._manifest_path,
            json.dumps({
                "version": self._loaded_manifest_version or MANIFEST_VERSION,
                "state": state,
                "routes": [route.to_dict() for route in routes],
            }, indent=2) + "\n",
        )

    def _remove_empty_shadow_parents(self) -> None:
        if not self._shadow_root.exists():
            return
        for path in sorted(
            (item for item in self._shadow_root.rglob("*") if item.is_dir()),
            key=lambda item: len(item.parts), reverse=True,
        ):
            try:
                path.rmdir()
            except OSError:
                pass
        try:
            self._shadow_root.rmdir()
        except OSError:
            pass

    @staticmethod
    def _tree_has_data(root: Path) -> bool:
        return root.is_dir() and any(root.iterdir())

    @classmethod
    def _tree_manifest(cls, root: Path) -> dict[str, tuple[int, str]]:
        cls._require_ordinary_tree(root, "save comparison")
        result: dict[str, tuple[int, str]] = {}
        if not root.exists():
            return result
        for path in sorted(item for item in root.rglob("*") if item.is_file()):
            digest = hashlib.sha256()
            with path.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(chunk)
            result[path.relative_to(root).as_posix()] = (
                path.stat().st_size, digest.hexdigest()
            )
        return result

    @staticmethod
    def _paths_overlap(left: Path, right: Path) -> bool:
        try:
            left.absolute().relative_to(right.absolute())
            return True
        except ValueError:
            pass
        try:
            right.absolute().relative_to(left.absolute())
            return True
        except ValueError:
            return False

    @staticmethod
    def _require_ordinary_tree(root: Path, label: str) -> None:
        """Reject symlinks and special files without following them."""
        if not os.path.lexists(root):
            return
        if root.is_symlink() or not root.is_dir():
            raise ModeTransitionError(f"Legacy Direct Save refuses invalid {label}: {root}")
        pending = [root]
        try:
            while pending:
                current = pending.pop()
                with os.scandir(current) as entries:
                    for entry in entries:
                        if entry.is_symlink():
                            raise ModeTransitionError(
                                f"Legacy Direct Save refuses symlinked {label}: {entry.path}"
                            )
                        if entry.is_dir(follow_symlinks=False):
                            pending.append(Path(entry.path))
                        elif not entry.is_file(follow_symlinks=False):
                            raise ModeTransitionError(
                                f"Legacy Direct Save refuses non-file {label}: {entry.path}"
                            )
        except ModeTransitionError:
            raise
        except OSError as exc:
            raise ModeTransitionError(
                f"Legacy Direct Save could not audit {label}: {root}"
            ) from exc
