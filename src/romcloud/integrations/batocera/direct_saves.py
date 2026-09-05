"""Owned, reversible Direct Mode routing for audited save directories.

Only layouts explicitly marked ``direct_save_capable`` participate.  Their
complete static directory is moved into an owned local shadow tree and the
remote directory is bind-mounted at the emulator's original path.  The move
is a same-filesystem rename and never copies or redirects ``/userdata/saves``
as a whole.
"""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from romcloud.core.exceptions import ModeTransitionError
from romcloud.core.save_selection import SaveSelectionPolicy
from romcloud.infrastructure.atomic_file import atomic_write_text
from romcloud.infrastructure.config import AppConfig

MANIFEST_FILENAME = "direct-save-routes.json"
MANIFEST_VERSION = 2
LEGACY_MANIFEST_VERSION = 1
SHADOW_DIRECTORY = "direct-save-local"

_LEGACY_DIRECT_LAYOUT_IDS = frozenset(
    {
        "mame-nvram",
        "mame-state",
        "pcsx2-legacy-states",
        "pcsx2-states",
        "ppsspp-savedata",
        "ppsspp-states",
    }
)


@dataclass(frozen=True)
class DirectSaveRoute:
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


class BindMountOperations:
    """Small injectable boundary around Linux bind-mount ownership checks."""

    def bind(self, source: Path, target: Path) -> None:
        subprocess.run(
            ["mount", "--bind", str(source), str(target)],
            check=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )

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


class DirectSaveRouting:
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
    def available(self) -> bool:
        return self._remote_root is not None and bool(self.planned_routes())

    @property
    def layout_ids(self) -> frozenset[str]:
        # An active manifest may be a safe subset after a ROMCloud upgrade
        # adds newly audited layouts. Those new routes are intentionally not
        # adopted until a later Cache -> Direct handoff reconciles them first.
        active = self._load_manifest()
        routes = active or self.planned_routes()
        return frozenset(route.layout_id for route in routes)

    @property
    def shadow_root(self) -> Path:
        return self._shadow_root

    @property
    def active(self) -> bool:
        return bool(self._load_manifest())

    def planned_routes(self) -> tuple[DirectSaveRoute, ...]:
        return self._planned_routes(selected_only=True)

    def _planned_routes(
        self, *, selected_only: bool
    ) -> tuple[DirectSaveRoute, ...]:
        if self._remote_root is None:
            return ()
        local_root = Path(self._config.saves.local_path)
        selected_systems = self._config.source.selected_systems
        routes = []
        for layout in self._policy.layouts:
            if not layout.direct_save_capable:
                continue
            lifecycle_systems = layout.lifecycle_systems or (layout.system,)
            if (
                selected_only
                and selected_systems is not None
                and not set(lifecycle_systems).intersection(selected_systems)
            ):
                continue
            relative = Path(*layout.direct_route_root.split("/"))
            canonical = relative.as_posix()
            routes.append(
                DirectSaveRoute(
                    layout.layout_id,
                    canonical,
                    local_root / relative,
                    self._remote_root / relative,
                    self._shadow_root / relative,
                )
            )
        return tuple(sorted(routes, key=lambda route: route.canonical_root))

    def activate(self) -> tuple[DirectSaveRoute, ...]:
        routes = self.planned_routes()
        if not routes:
            return ()
        existing = self._load_manifest()
        if existing:
            self._ensure_active(existing)
            return existing
        self._preflight_new(routes)
        self._write_manifest("preparing", routes)
        activated: list[DirectSaveRoute] = []
        try:
            for route in routes:
                route.local_path.mkdir(parents=True, exist_ok=True)
                route.remote_path.mkdir(parents=True, exist_ok=True)
                route.shadow_path.parent.mkdir(parents=True, exist_ok=True)
                os.replace(route.local_path, route.shadow_path)
                route.local_path.mkdir(parents=True, exist_ok=False)
                self._mounts.bind(route.remote_path, route.local_path)
                activated.append(route)
            self._write_manifest("active", routes)
            return routes
        except Exception as exc:
            rollback_error = self._rollback_activation(routes, activated)
            if rollback_error is not None:
                raise ModeTransitionError(
                    "Direct save routing failed and local ownership could not be "
                    "fully restored; the owned routing manifest was preserved for recovery."
                ) from rollback_error
            raise ModeTransitionError(
                "Direct save routing could not be activated; local save ownership was restored."
            ) from exc

    def deactivate(self) -> tuple[DirectSaveRoute, ...]:
        routes = self._load_manifest()
        if not routes:
            return ()
        try:
            for route in reversed(routes):
                self._restore_route_to_local(route)
            self._manifest_path.unlink(missing_ok=True)
            self._remove_empty_shadow_parents()
            return routes
        except Exception as exc:
            rollback_error = self._rollback_deactivation(routes)
            if rollback_error is not None:
                raise ModeTransitionError(
                    "Direct save routing could not be removed or fully restored; "
                    "the owned routing manifest was preserved for recovery."
                ) from rollback_error
            raise ModeTransitionError(
                "Direct save routing could not be removed; remote save ownership remains active."
            ) from exc

    def recover_for_mode(self, *, direct: bool) -> None:
        routes = self._load_manifest()
        if direct:
            if routes:
                self._ensure_active(routes)
        elif routes:
            self.deactivate()

    def _preflight_new(self, routes: Iterable[DirectSaveRoute]) -> None:
        routes = tuple(routes)
        local_root = Path(self._config.saves.local_path)
        data_root = Path(self._config.data_path)
        assert self._remote_root is not None
        local_root.mkdir(parents=True, exist_ok=True)
        data_root.mkdir(parents=True, exist_ok=True)
        if local_root.is_symlink() or data_root.is_symlink():
            raise ModeTransitionError(
                "Direct save routing refuses symlinked local save or ROMCloud data roots."
            )
        if self._shadow_root.exists():
            raise ModeTransitionError(
                f"Unowned Direct save shadow path already exists: {self._shadow_root}"
            )
        data_device = data_root.stat().st_dev
        if local_root.stat().st_dev != data_device:
            raise ModeTransitionError(
                "Direct save routing requires ROMCloud data and Batocera saves "
                "on the same filesystem."
            )
        self._require_no_symlink_components(
            self._shadow_root, data_root, "save shadow"
        )
        for index, route in enumerate(routes):
            for other in routes[index + 1 :]:
                if self._paths_overlap(route.local_path, other.local_path):
                    raise ModeTransitionError(
                        "Direct-save-capable layouts must not overlap: "
                        f"{route.layout_id}, {other.layout_id}"
                    )
        for route in routes:
            self._require_within(route.local_path, local_root, "local save")
            self._require_within(route.remote_path, self._remote_root, "remote save")
            self._require_within(route.shadow_path, self._shadow_root, "save shadow")
            self._require_no_symlink_components(
                route.local_path, local_root, "local save"
            )
            self._require_no_symlink_components(
                route.remote_path, self._remote_root, "remote save"
            )
            self._require_ordinary_tree(route.local_path, "local save")
            self._require_ordinary_tree(route.remote_path, "remote save")
            local_ancestor = route.local_path
            while not local_ancestor.exists() and local_ancestor != local_root:
                local_ancestor = local_ancestor.parent
            if local_ancestor.stat().st_dev != data_device:
                raise ModeTransitionError(
                    "Direct save routing requires each local save directory and "
                    "its ROMCloud shadow to share a filesystem: "
                    f"{route.canonical_root}"
                )
            if self._mounts.is_mount(route.local_path):
                raise ModeTransitionError(
                    "Direct save routing found an existing mount without its owned "
                    f"manifest: {route.local_path}"
                )
            if route.shadow_path.exists():
                raise ModeTransitionError(
                    f"Direct save shadow already exists: {route.shadow_path}"
                )

    def _ensure_active(self, routes: tuple[DirectSaveRoute, ...]) -> None:
        for route in routes:
            if not route.remote_path.is_dir() or route.remote_path.is_symlink():
                raise ModeTransitionError(
                    f"Direct save remote directory is unavailable: {route.remote_path}"
                )
            self._require_ordinary_tree(route.remote_path, "remote save")
            if route.shadow_path.exists():
                self._require_ordinary_tree(route.shadow_path, "local save shadow")
            if self._mounts.is_owned(route.remote_path, route.local_path):
                if not route.shadow_path.is_dir() or route.shadow_path.is_symlink():
                    raise ModeTransitionError(
                        f"Direct save local shadow is unavailable: {route.shadow_path}"
                    )
                continue
            if self._mounts.is_mount(route.local_path):
                raise ModeTransitionError(
                    f"Direct save mount source changed for {route.local_path}; "
                    "refusing to touch it."
                )
            if route.local_path.exists() and any(route.local_path.iterdir()):
                if route.shadow_path.exists():
                    raise ModeTransitionError(
                        f"Direct save mount point contains unowned data: {route.local_path}"
                    )
            if not route.shadow_path.exists():
                route.local_path.mkdir(parents=True, exist_ok=True)
                route.shadow_path.parent.mkdir(parents=True, exist_ok=True)
                os.replace(route.local_path, route.shadow_path)
            elif not route.shadow_path.is_dir() or route.shadow_path.is_symlink():
                raise ModeTransitionError(
                    f"Direct save local shadow is unavailable: {route.shadow_path}"
                )
            route.local_path.mkdir(parents=True, exist_ok=True)
            self._mounts.bind(route.remote_path, route.local_path)
        self._write_manifest(
            "active",
            routes,
            version=self._loaded_manifest_version or MANIFEST_VERSION,
        )

    def _rollback_activation(
        self,
        routes: tuple[DirectSaveRoute, ...],
        activated: list[DirectSaveRoute],
    ) -> Exception | None:
        errors: list[Exception] = []
        for route in reversed(routes):
            try:
                if route in activated or self._mounts.is_owned(
                    route.remote_path, route.local_path
                ):
                    self._mounts.unbind(route.local_path)
                elif self._mounts.is_mount(route.local_path):
                    raise ModeTransitionError(
                        f"Unowned mount blocks Direct save rollback: {route.local_path}"
                    )
                if route.shadow_path.exists():
                    if (
                        route.local_path.is_dir()
                        and not any(route.local_path.iterdir())
                    ):
                        route.local_path.rmdir()
                    if route.local_path.exists():
                        raise ModeTransitionError(
                            "Direct save rollback found unexpected local data: "
                            f"{route.local_path}"
                        )
                    os.replace(route.shadow_path, route.local_path)
            except Exception as exc:
                errors.append(exc)
        if errors:
            self._write_manifest("recovery-required", routes)
            return errors[0]
        self._manifest_path.unlink(missing_ok=True)
        self._remove_empty_shadow_parents()
        return None

    def _rollback_deactivation(
        self,
        routes: tuple[DirectSaveRoute, ...],
    ) -> Exception | None:
        try:
            self._ensure_active(routes)
        except Exception as exc:
            self._write_manifest(
                "recovery-required",
                routes,
                version=self._loaded_manifest_version or MANIFEST_VERSION,
            )
            return exc
        return None

    def _restore_route_to_local(self, route: DirectSaveRoute) -> None:
        if self._mounts.is_owned(route.remote_path, route.local_path):
            self._mounts.unbind(route.local_path)
        elif self._mounts.is_mount(route.local_path):
            raise ModeTransitionError(
                f"Direct save mount source changed for {route.local_path}; refusing to touch it."
            )
        if route.shadow_path.exists():
            if not route.shadow_path.is_dir() or route.shadow_path.is_symlink():
                raise ModeTransitionError(
                    f"Direct save local shadow is unavailable: {route.shadow_path}"
                )
            if route.local_path.exists():
                if (
                    not route.local_path.is_dir()
                    or route.local_path.is_symlink()
                    or any(route.local_path.iterdir())
                ):
                    raise ModeTransitionError(
                        f"Direct save mount point contains unowned data: {route.local_path}"
                    )
                route.local_path.rmdir()
            route.local_path.parent.mkdir(parents=True, exist_ok=True)
            os.replace(route.shadow_path, route.local_path)
        elif route.local_path.exists() and (
            not route.local_path.is_dir() or route.local_path.is_symlink()
        ):
            raise ModeTransitionError(
                f"Direct save local working directory is unavailable: {route.local_path}"
            )

    def _load_manifest(self) -> tuple[DirectSaveRoute, ...]:
        if not os.path.lexists(self._manifest_path):
            self._loaded_manifest_version = None
            return ()
        if self._manifest_path.is_symlink() or not self._manifest_path.is_file():
            raise ModeTransitionError(
                f"Direct save routing manifest is invalid: {self._manifest_path}"
            )
        try:
            payload = json.loads(self._manifest_path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("routing manifest must be an object")
            version = payload.get("version")
            if version not in {LEGACY_MANIFEST_VERSION, MANIFEST_VERSION}:
                raise ValueError("unsupported version")
            if payload.get("state") not in {
                "preparing",
                "active",
                "recovery-required",
            }:
                raise ValueError("unsupported routing state")
            records = tuple(
                DirectSaveRoute(
                    str(item["layout_id"]),
                    str(item["canonical_root"]),
                    Path(item["local_path"]),
                    Path(item["remote_path"]),
                    Path(item["shadow_path"]),
                )
                for item in payload["routes"]
            )
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ModeTransitionError(
                f"Direct save routing manifest is invalid: {self._manifest_path}"
            ) from exc
        planned = {
            route.layout_id: route
            for route in self._planned_routes(selected_only=False)
        }
        record_ids = [route.layout_id for route in records]
        if (
            not records
            or len(record_ids) != len(set(record_ids))
            or any(planned.get(route.layout_id) != route for route in records)
            or (
                version == LEGACY_MANIFEST_VERSION
                and set(record_ids) != set(_LEGACY_DIRECT_LAYOUT_IDS)
            )
            or (
                version == MANIFEST_VERSION
                and set(record_ids)
                != {route.layout_id for route in self.planned_routes()}
            )
        ):
            raise ModeTransitionError(
                "Direct save routing configuration changed; refusing to touch existing mounts."
            )
        self._loaded_manifest_version = int(version)
        return records

    def _write_manifest(
        self,
        state: str,
        routes: Iterable[DirectSaveRoute],
        *,
        version: int = MANIFEST_VERSION,
    ) -> None:
        atomic_write_text(
            self._manifest_path,
            json.dumps(
                {
                    "version": version,
                    "state": state,
                    "routes": [route.to_dict() for route in routes],
                },
                indent=2,
            )
            + "\n",
        )
        self._loaded_manifest_version = version

    def _remove_empty_shadow_parents(self) -> None:
        if not self._shadow_root.exists():
            return
        for path in sorted(
            (item for item in self._shadow_root.rglob("*") if item.is_dir()),
            key=lambda item: len(item.parts),
            reverse=True,
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
    def _require_within(path: Path, root: Path, label: str) -> None:
        try:
            path.absolute().relative_to(root.absolute())
        except ValueError as exc:
            raise ModeTransitionError(f"Unsafe {label} route outside {root}: {path}") from exc

    @staticmethod
    def _require_no_symlink_components(path: Path, root: Path, label: str) -> None:
        absolute_root = root.absolute()
        relative = path.absolute().relative_to(absolute_root)
        root_parts = absolute_root.parts
        current = Path(root_parts[0])
        for part in root_parts[1:]:
            current = current / part
            if current.is_symlink():
                raise ModeTransitionError(
                    f"Direct save routing refuses symlinked {label}: {current}"
                )
        for part in relative.parts:
            current = current / part
            if current.is_symlink():
                raise ModeTransitionError(
                    f"Direct save routing refuses symlinked {label}: {current}"
                )

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
        """Reject links/devices without following anything outside the route."""
        if not root.exists():
            return
        pending = [root]
        try:
            while pending:
                current = pending.pop()
                with os.scandir(current) as entries:
                    for entry in entries:
                        if entry.is_symlink():
                            raise ModeTransitionError(
                                f"Direct save routing refuses symlinked {label}: "
                                f"{entry.path}"
                            )
                        if entry.is_dir(follow_symlinks=False):
                            pending.append(Path(entry.path))
                        elif not entry.is_file(follow_symlinks=False):
                            raise ModeTransitionError(
                                f"Direct save routing refuses non-file {label} entry: "
                                f"{entry.path}"
                            )
        except ModeTransitionError:
            raise
        except OSError as exc:
            raise ModeTransitionError(
                f"Direct save routing could not audit {label} tree: {root}"
            ) from exc
