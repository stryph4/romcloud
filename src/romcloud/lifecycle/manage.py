"""Repair, uninstall, and purge orchestration for ROMCloud-owned artifacts."""

from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import stat
import tempfile
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable, Optional

from romcloud.bootstrap.container import Container
from romcloud.infrastructure import mount as mountlib
from romcloud.infrastructure import mount_worker
from romcloud.infrastructure.config import AppConfig
from romcloud.infrastructure.credentials import (
    cifs_credentials_path,
    remote_data_cifs_credentials_path,
)
from romcloud.infrastructure.ownership import root_is_owned
from romcloud.core.progress import ProgressSink, emit_progress
from romcloud.integrations.batocera import (
    auto_savesync,
    es_config,
    mount_service,
    ports_gamelist_config,
)
from romcloud.integrations.batocera.proxy_ownership import (
    is_within as _is_within,
    remove_owned_proxy_files,
)
from romcloud.lifecycle import install
from romcloud.troubleshoot import ActivitySnapshot, inspect_activity


@dataclass(frozen=True)
class LifecycleReport:
    proxies_removed: int = 0
    proxies_restored: int = 0
    direct_links_removed: int = 0
    library_entries_removed: int = 0
    library_media_removed: int = 0
    stages: tuple["LifecycleStageResult", ...] = ()
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class LifecycleStageResult:
    name: str
    status: str
    detail: str = ""


@dataclass(frozen=True)
class PathIdentity:
    path: Path
    resolved: Path
    exists: bool
    device: int | None = None
    inode: int | None = None
    mode: int | None = None
    ownership_kind: str = ""
    owned: bool = False
    ownership_detail: str = ""


@dataclass(frozen=True)
class MountIdentity:
    label: str
    mount_point: Path
    server: str
    share: str
    remote_path: str
    read_only: bool
    mounted: bool
    matches: bool


@dataclass(frozen=True)
class LifecyclePreflight:
    operation: str
    owned_roots: tuple[PathIdentity, ...]
    protected_roots: tuple[Path, ...]
    activity: ActivitySnapshot
    mounts: tuple[MountIdentity, ...] = ()
    ownership_findings: tuple[str, ...] = ()
    blockers: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    config_trusted: bool = True


class LifecycleFailure(RuntimeError):
    def __init__(self, message: str, report: LifecycleReport) -> None:
        super().__init__(message)
        self.report = report


def _manifest_records(config: AppConfig) -> list[tuple[str, Path]]:
    db_path = Path(config.data_path) / "catalog.db"
    if not db_path.is_file():
        return []
    try:
        with sqlite3.connect(db_path) as conn:
            rows = conn.execute("SELECT game_id, proxy_path FROM proxy_records").fetchall()
    except sqlite3.Error:
        return []
    return [(str(game_id), Path(str(proxy_path))) for game_id, proxy_path in rows]


def remove_owned_proxies(
    config: AppConfig, *, keep_game_ids: Optional[set[str]] = None
) -> int:
    """Remove only manifest-owned or strictly signed ROMCloud proxy files."""
    return remove_owned_proxy_files(
        Path(config.local_roms_path),
        manifest_records=_manifest_records(config),
        keep_game_ids=keep_game_ids,
    )


def restore_owned_proxies(
    config: AppConfig,
    *,
    game_ids: Optional[set[str]] = None,
    progress: ProgressSink = None,
) -> int:
    """Recreate selected missing proxies from retained catalog games.

    ``game_ids=None`` restores the full catalog. An explicit set supports
    cached-only presentation without changing catalog or proxy ownership rows.

    A ``game_id`` selected for exposure that has no proxy registration at
    all (e.g. an interrupted catalog refresh left a cache-complete game
    without one) is registered and materialized here too — selection for
    exposure must not silently no-op just because no prior record exists.
    """
    container = Container(config)
    restored = 0
    all_records = container.proxy_repo.list_all()
    known_ids = {record.game_id for record in all_records}
    records = [
        record
        for record in all_records
        if game_ids is None or record.game_id in game_ids
    ]
    games = {game.id: game for game in container.game_repo.list_all()}
    unregistered_ids = sorted(
        (set(games) if game_ids is None else game_ids) - known_ids
    )
    total = len(records) + len(unregistered_ids)
    emit_progress(
        progress,
        "operating_mode",
        "managed_entries",
        "running",
        "Restoring ROMCloud entries",
        current=0,
        total=total,
    )
    interval = max(1, total // 100) if total else 1
    for index, record in enumerate(records, start=1):
        path = Path(record.proxy_path)
        if not path.exists() and _is_within(path, Path(config.local_roms_path)):
            game = games.get(record.game_id)
            if game is not None:
                payload = {
                    "romcloud_version": "1",
                    "game_id": game.id,
                    "title": game.title,
                    "system": game.system,
                    "source_provider": game.source_provider,
                    "source_root": game.source_root,
                    "assets": [
                        {
                            "filename": asset.filename,
                            "relative_path": asset.relative_path,
                            "is_primary": asset.is_primary,
                        }
                        for asset in game.assets
                    ],
                }
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(
                    json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8"
                )
                restored += 1
        if index == total or index % interval == 0:
            emit_progress(
                progress,
                "operating_mode",
                "managed_entries",
                "running",
                f"Restoring ROMCloud entries: {index:,} / {total:,} games",
                current=index,
                total=total,
                metadata={"restored": restored},
            )
    for offset, game_id in enumerate(unregistered_ids, start=1):
        game = games.get(game_id)
        if game is not None:
            container.catalog.ensure_proxy(game)
            restored += 1
        index = len(records) + offset
        if index == total or index % interval == 0:
            emit_progress(
                progress,
                "operating_mode",
                "managed_entries",
                "running",
                f"Restoring ROMCloud entries: {index:,} / {total:,} games",
                current=index,
                total=total,
                metadata={"restored": restored},
            )
    emit_progress(
        progress,
        "operating_mode",
        "managed_entries",
        "success",
        "ROMCloud entries restored",
        current=total,
        total=total,
        metadata={"restored": restored},
    )
    return restored


def repair(
    *,
    config: AppConfig,
    romcloud_home: Path,
    project_root: Path,
    ports_dir: Optional[Path] = None,
    system_python: Optional[str] = None,
) -> tuple[install.ReconcileReport, LifecycleReport]:
    venv_python = romcloud_home / "venv" / "bin" / "python"
    if not venv_python.is_file():
        raise RuntimeError(
            f"The ROMCloud virtual environment is missing at {venv_python}; "
            "rerun the bootstrap installer to recreate it."
        )
    from romcloud.web.lifecycle import manager_status, start_manager, stop_manager

    manager_was_running = bool(manager_status(config.data_path).get("running"))
    if manager_was_running:
        stop_manager(config.data_path)
    try:
        installed_payload = romcloud_home / "ports-gfx" / "ports_gfx"
        if not (project_root / "ports_gfx").is_dir() and installed_payload.is_dir():
            with tempfile.TemporaryDirectory(prefix="romcloud-repair-") as tmp:
                staged_root = Path(tmp)
                shutil.copytree(installed_payload, staged_root / "ports_gfx")
                reconcile_report = install.reconcile_install(
                    romcloud_home=romcloud_home,
                    project_root=staged_root,
                    ports_dir=ports_dir,
                    system_python=system_python,
                )
        else:
            reconcile_report = install.reconcile_install(
                romcloud_home=romcloud_home,
                project_root=project_root,
                ports_dir=ports_dir,
                system_python=system_python,
            )
    finally:
        if manager_was_running:
            start_manager(romcloud_home / "bin" / "romcloud", config.data_path)
    return reconcile_report, LifecycleReport(
        proxies_restored=reconcile_report.proxies_restored
    )


_LEGACY_CREDENTIALS_FILENAME = "smb.credentials"


def _is_ephemeral_cifs_credential(path: Path) -> bool:
    if not re.fullmatch(r"\.romcloud-cifs-(?:source|remote-data)-[A-Za-z0-9_]{6,}", path.name):
        return False
    try:
        info = path.lstat()
        if (
            not stat.S_ISREG(info.st_mode)
            or stat.S_IMODE(info.st_mode) != 0o600
            or info.st_nlink != 1
            or info.st_uid != os.geteuid()
            or info.st_size > 16 * 1024
        ):
            return False
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError):
        return False
    keys = [line.partition("=")[0] for line in lines if "=" in line]
    return len(keys) == len(lines) and set(keys) in (
        {"username", "password"},
        {"username", "password", "domain"},
    ) and len(keys) == len(set(keys))


def _remove_credential_files(config: AppConfig) -> None:
    """Remove every ROMCloud-owned local credential copy during Purge.

    Covers: the canonical (encrypted or, on very old installs, plaintext)
    ``credentials.toml``; the pre-migration legacy ``smb.credentials`` file;
    the now-retired permanent ``mount.cifs`` credential files older ROMCloud
    versions left on disk; and any ephemeral CIFS credential temp file that
    a crash mid-mount could have left behind (normally cleaned up in
    ``finally`` — this is defensive, not the primary cleanup path).
    """
    credentials_path = config.credentials_path
    credentials_path.unlink(missing_ok=True)
    credentials_path.with_name(_LEGACY_CREDENTIALS_FILENAME).unlink(missing_ok=True)
    cifs_credentials_path(credentials_path).unlink(missing_ok=True)
    remote_data_cifs_credentials_path(credentials_path).unlink(missing_ok=True)
    (credentials_path.parent / "setup-state.json").unlink(missing_ok=True)
    for stale in credentials_path.parent.glob(".romcloud-cifs-*"):
        if _is_ephemeral_cifs_credential(stale):
            stale.unlink(missing_ok=True)


def _paths_overlap(first: Path, second: Path) -> bool:
    return _is_within(first, second) or _is_within(second, first)


def _path_identity(path: Path) -> PathIdentity:
    raw = os.fspath(path)
    if not raw or not path.is_absolute() or any(part in {".", ".."} for part in path.parts):
        raise RuntimeError(f"Refusing unsafe lifecycle target: {path}")
    if path.is_symlink():
        raise RuntimeError(f"Refusing top-level symlink lifecycle target: {path}")
    try:
        resolved = path.resolve(strict=False)
    except (OSError, RuntimeError) as exc:
        raise RuntimeError(f"Cannot canonicalize lifecycle target {path}: {exc}") from exc
    forbidden = {Path("/"), Path("/userdata"), Path("/userdata/system")}
    if resolved in forbidden:
        raise RuntimeError(f"Refusing unsafe lifecycle target: {path}")
    try:
        info = path.lstat()
    except FileNotFoundError:
        return PathIdentity(path, resolved, False)
    if stat.S_ISLNK(info.st_mode):
        raise RuntimeError(f"Refusing top-level symlink lifecycle target: {path}")
    return PathIdentity(path, resolved, True, info.st_dev, info.st_ino, info.st_mode)


def _external_key_paths(config: AppConfig) -> tuple[Path, ...]:
    result: list[Path] = []
    for candidate in (
        config.sftp.private_key_path if config.sftp is not None else "",
        config.remote_data.sftp.private_key_path
        if config.remote_data is not None and config.remote_data.sftp is not None
        else "",
    ):
        if candidate:
            result.append(Path(candidate))
    return tuple(result)


def _protected_roots(config: AppConfig) -> tuple[Path, ...]:
    local_saves = Path(config.saves.local_path)
    roots: list[Path] = [
        Path(config.local_roms_path),
        local_saves,
        local_saves.with_name(local_saves.name + ".previous"),
        Path("/userdata/roms"),
        Path("/userdata/saves"),
        Path("/userdata/saves.previous"),
    ]
    if config.source.enabled and config.source.provider != "sftp":
        roots.append(Path(config.source.rom_root))
    if config.remote_data is not None and config.remote_data.provider in {"local", "smb"}:
        roots.append(Path(config.remote_data.root))
    roots.extend(_external_key_paths(config))
    canonical: list[Path] = []
    for root in roots:
        if not root.is_absolute():
            raise RuntimeError(f"Protected path is not absolute: {root}")
        try:
            canonical.append(root.resolve(strict=False))
        except (OSError, RuntimeError) as exc:
            raise RuntimeError(f"Cannot canonicalize protected path {root}: {exc}") from exc
    return tuple(dict.fromkeys(canonical))


def _presentation_root_blockers(
    config: AppConfig, *, mount_points: tuple[Path, ...]
) -> tuple[str, ...]:
    """Reject presentation cleanup when its traversal boundary is unsafe.

    Proxy discovery recursively walks the local ROM presentation root and
    Direct/Library cleanup mutates narrowly owned entries beneath it. The
    configured path therefore needs its own safety proof even though the root
    itself is deliberately preserved.
    """

    identity = _path_identity(Path(config.local_roms_path))
    save_root = Path(config.saves.local_path)
    comparison_roots: list[tuple[str, Path]] = [
        ("save root", save_root),
        ("save recovery root", save_root.with_name(save_root.name + ".previous")),
        ("Batocera system root", Path("/userdata/system")),
        ("Batocera save root", Path("/userdata/saves")),
        ("Batocera save recovery root", Path("/userdata/saves.previous")),
    ]
    if config.source.enabled and config.source.provider in {"local", "smb"}:
        comparison_roots.append(("ROM source root", Path(config.source.rom_root)))
    if config.remote_data is not None and config.remote_data.provider in {"local", "smb"}:
        comparison_roots.append(("remote data root", Path(config.remote_data.root)))
    comparison_roots.extend(
        ("external private key", path) for path in _external_key_paths(config)
    )

    blockers: list[str] = []
    for label, root in comparison_roots:
        if not root.is_absolute():
            blockers.append(f"{label} is not absolute: {root}")
            continue
        try:
            resolved = root.resolve(strict=False)
        except (OSError, RuntimeError):
            blockers.append(f"cannot canonicalize {label}: {root}")
            continue
        if _paths_overlap(identity.resolved, resolved):
            blockers.append(
                f"local ROM presentation root overlaps {label}: {identity.path} ({root})"
            )

    for mount_point in mount_points:
        try:
            mounted = mount_point.resolve(strict=False)
        except (OSError, RuntimeError):
            mounted = mount_point
        if mounted != Path("/") and (
            mounted == identity.resolved or _is_within(mounted, identity.resolved)
        ):
            blockers.append(
                f"local ROM presentation root contains a mount boundary: "
                f"{identity.path} ({mounted})"
            )
    return tuple(blockers)


def _mount_points(path: Path = Path("/proc/self/mountinfo")) -> tuple[Path, ...]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise RuntimeError(f"Cannot inspect filesystem mount boundaries: {exc}") from exc
    result: list[Path] = []
    for line in lines:
        before, separator, _after = line.partition(" - ")
        fields = before.split()
        if not separator or len(fields) < 5:
            raise RuntimeError("Cannot parse filesystem mount boundaries")
        decoded = fields[4].replace("\\040", " ").replace("\\011", "\t").replace("\\134", "\\")
        result.append(Path(decoded))
    return tuple(result)


def _validate_owned_tree(
    path: Path,
    *,
    protected: tuple[Path, ...],
    mount_points: tuple[Path, ...] | None = None,
    ownership_kind: str = "",
    owned: bool = False,
    ownership_detail: str = "",
) -> PathIdentity:
    identity = _path_identity(path)
    if any(_paths_overlap(identity.resolved, item) for item in protected):
        raise RuntimeError(
            f"Refusing lifecycle target that overlaps protected user/provider data: {path}"
        )
    for mount_point in mount_points if mount_points is not None else _mount_points():
        try:
            mounted = mount_point.resolve(strict=False)
        except (OSError, RuntimeError):
            mounted = mount_point
        if mounted == Path("/"):
            continue
        if mounted == identity.resolved or _is_within(mounted, identity.resolved):
            raise RuntimeError(
                f"Refusing lifecycle target containing a mount boundary: {path} ({mounted})"
            )
    return replace(
        identity,
        ownership_kind=ownership_kind,
        owned=owned,
        ownership_detail=ownership_detail,
    )


def _assert_path_identity(expected: PathIdentity) -> None:
    current = _path_identity(expected.path)
    if (
        current.path,
        current.resolved,
        current.exists,
        current.device,
        current.inode,
        current.mode,
    ) != (
        expected.path,
        expected.resolved,
        expected.exists,
        expected.device,
        expected.inode,
        expected.mode,
    ):
        raise RuntimeError(f"Lifecycle target identity changed after preflight: {expected.path}")


def _verified_mount_worker_pid(romcloud_home: Path) -> int | None:
    """Read the worker identity without stale-lock cleanup or other mutation."""

    path = mount_worker.lock_path(romcloud_home)
    try:
        pid = int(path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None
    return pid if mount_worker._pid_alive(pid) and mount_worker._worker_cmdline_matches(  # noqa: SLF001
        pid, proc_root=Path("/proc")
    ) else None


def _menu_loop_identity(data_root: Path) -> tuple[str, int | None]:
    path = auto_savesync.menu_loop_pid_path(data_root)
    if not path.exists():
        return "inactive", None
    try:
        pid = int(path.read_text(encoding="ascii").strip())
    except (OSError, ValueError):
        return "unknown", None
    if not auto_savesync._pid_alive(pid):  # noqa: SLF001
        return "inactive", None
    if auto_savesync._menu_loop_cmdline_matches(pid, proc_root=Path("/proc")):  # noqa: SLF001
        return "active", pid
    return "unknown", pid


def _activity_blockers(
    snapshot: ActivitySnapshot, *, romcloud_home: Path, data_root: Path
) -> tuple[str, ...]:
    blocked: list[str] = []
    for name in ("game", "download", "savesync", "library_sync", "graphical_ui"):
        state = getattr(snapshot, name)
        if state.state != "inactive":
            blocked.append(f"{name}:{state.state} ({state.detail or 'no detail'})")

    manager = snapshot.browser_manager
    manager_owned = (
        manager.state == "active" and manager.detail == "Owned manager endpoint is reachable."
    )
    if manager.state == "unknown" or (manager.state == "active" and not manager_owned):
        blocked.append(
            f"browser_manager:{manager.state} ({manager.detail or 'unverified process ownership'})"
        )

    worker = snapshot.mount_worker
    if worker.state == "unknown" or (
        worker.state == "active" and _verified_mount_worker_pid(romcloud_home) is None
    ):
        blocked.append(
            f"mount_worker:{worker.state} ({worker.detail or 'unverified process ownership'})"
        )

    menu_state, menu_pid = _menu_loop_identity(data_root)
    if menu_state == "unknown":
        blocked.append(f"menu_loop:unknown (unverified PID {menu_pid or 'marker'})")
    return tuple(blocked)


def _mount_preflight(config: AppConfig) -> tuple[tuple[MountIdentity, ...], tuple[str, ...]]:
    identities: list[MountIdentity] = []
    blockers: list[str] = []
    for target in mount_worker.configured_mounts(config):
        mounted = mountlib.is_target_mounted(target.mount_point)
        matches = not mounted or mountlib.is_target_mounted_cifs(
            target.mount_point,
            server=target.smb.server,
            share=target.smb.share,
            read_only=target.read_only,
            remote_path=target.smb.remote_path,
        )
        identities.append(
            MountIdentity(
                target.label,
                Path(target.mount_point),
                target.smb.server,
                target.smb.share,
                target.smb.remote_path,
                target.read_only,
                mounted,
                matches,
            )
        )
        if mounted and not matches:
            blockers.append(
                f"{target.label}:foreign mount at configured point {target.mount_point}"
            )
    return tuple(identities), tuple(blockers)


def _root_ownership(
    *, romcloud_home: Path, path: Path, kind: str, home_owned: bool
) -> tuple[bool, str]:
    if kind == "runtime":
        owned = home_owned and path.parent == romcloud_home and path.name in {
            "bin",
            "venv",
            "ports-gfx",
            "runtime",
        }
        return owned, "inherited from the path-bound ROMCloud home ledger" if owned else "home ownership is unproven"
    owned = root_is_owned(romcloud_home, kind, path)
    return owned, "exact path-bound ownership ledger match" if owned else f"no exact {kind} ownership ledger entry"


def lifecycle_preflight(
    *,
    operation: str,
    config: AppConfig,
    romcloud_home: Path,
    config_trusted: bool = True,
    activity: ActivitySnapshot | None = None,
) -> LifecyclePreflight:
    """Finish all mutation authorization before stopping a process or writing."""
    if operation not in {"uninstall", "purge"}:
        raise ValueError(f"Unknown lifecycle operation: {operation}")
    snapshot = activity or inspect_activity(
        config, catalog_path=Path(config.data_path) / "catalog.db"
    )
    blockers = list(
        _activity_blockers(
            snapshot,
            romcloud_home=romcloud_home,
            data_root=Path(config.data_path),
        )
    )
    warnings: list[str] = []
    findings: list[str] = []
    mounts: tuple[MountIdentity, ...] = ()
    identities: list[PathIdentity] = []

    if not config_trusted:
        warnings.append(
            "Configuration is missing; config-derived runtime, data, cache, and presentation paths were preserved."
        )
    else:
        protected = _protected_roots(config)
        mount_points = _mount_points()
        blockers.extend(_presentation_root_blockers(config, mount_points=mount_points))
        from romcloud.web.browser_runtime import runtime_root

        home_owned = root_is_owned(romcloud_home, "home", romcloud_home)
        candidate_roots: list[tuple[Path, str]] = [
            (romcloud_home, "home"),
            (Path(config.data_path), "data"),
            *(([(Path(config.cache.path), "cache")]) if operation == "purge" else []),
            *((romcloud_home / name, "runtime") for name in ("bin", "venv", "ports-gfx", "runtime")),
            (runtime_root(config.data_path), "browser"),
        ]
        for candidate, kind in candidate_roots:
            owned, ownership_detail = _root_ownership(
                romcloud_home=romcloud_home,
                path=candidate,
                kind=kind,
                home_owned=home_owned,
            )
            if kind == "browser":
                from romcloud.web.browser_runtime import managed_runtime_is_owned

                owned = managed_runtime_is_owned(config.data_path)
                ownership_detail = (
                    "exact managed-browser root marker"
                    if owned
                    else "managed-browser root marker is absent or invalid"
                )
            identities.append(
                _validate_owned_tree(
                    candidate,
                    protected=protected,
                    mount_points=mount_points,
                    ownership_kind=kind,
                    owned=owned,
                    ownership_detail=ownership_detail,
                )
            )
            if owned:
                findings.append(f"{kind}:{candidate}:owned")
            elif candidate.exists() or candidate.is_symlink():
                warnings.append(
                    f"Preserved unverified {kind} root {candidate}: {ownership_detail}."
                )

        # Nested data/cache roots are normalized by deleting the deepest roots
        # first, but they must never contain one another or contain the home.
        home = romcloud_home.resolve(strict=False)
        data = Path(config.data_path).resolve(strict=False)
        cache = Path(config.cache.path).resolve(strict=False)
        if _paths_overlap(data, cache) or _is_within(home, data) or _is_within(home, cache):
            blockers.append("unsafe overlap among ROMCloud home/data/cache")

        mounts, mount_blockers = _mount_preflight(config)
        blockers.extend(mount_blockers)

        from romcloud.integrations.batocera import game_access

        romcloud_bin = romcloud_home / "bin" / "romcloud"
        if mount_service.SERVICE_SCRIPT_PATH.exists() or mount_service.SERVICE_SCRIPT_PATH.is_symlink():
            if mount_service.service_is_owned(mount_service.SERVICE_SCRIPT_PATH, romcloud_bin):
                findings.append("startup-service:owned")
            else:
                warnings.append("Startup service path contains an unverified artifact; it will be preserved.")
        if auto_savesync.HOOK_PATH.exists() or auto_savesync.HOOK_PATH.is_symlink():
            if auto_savesync.hook_is_owned(romcloud_bin, hook_path=auto_savesync.HOOK_PATH):
                findings.append("auto-savesync-hook:owned")
            else:
                warnings.append("Auto SaveSync hook path contains an unverified artifact; it will be preserved.")
        if es_config.ROMCLOUD_OVERRIDE_PATH.exists() or es_config.ROMCLOUD_OVERRIDE_PATH.is_symlink():
            if es_config.override_is_owned(
                es_config.ROMCLOUD_OVERRIDE_PATH,
                wrapper_path=romcloud_home / "bin" / "romcloud-run",
            ):
                findings.append("emulationstation-override:owned")
            else:
                warnings.append("ES override path contains an unverified artifact; it will be preserved.")

        _records, manifest_state = game_access._load_manifest_result(config)
        if manifest_state == "malformed":
            warnings.append("Direct-link ownership manifest is malformed; links will be preserved.")
        if not (Path(config.data_path) / "catalog.db").is_file():
            findings.append("Catalog ownership evidence is unavailable; proxy files will be preserved.")

    protected = _protected_roots(config) if config_trusted else ()
    if blockers:
        raise RuntimeError("Lifecycle preflight blocked: " + "; ".join(blockers))
    return LifecyclePreflight(
        operation,
        tuple(identities),
        protected,
        snapshot,
        mounts,
        tuple(findings),
        (),
        tuple(warnings),
        config_trusted,
    )


def _record_stage(
    stages: list[LifecycleStageResult],
    name: str,
    action: Callable[[], object],
    *,
    required: bool = True,
) -> object | None:
    try:
        result = action()
    except Exception as exc:  # noqa: BLE001 - convert to an honest stage result
        stages.append(LifecycleStageResult(name, "failed" if required else "warning", str(exc)))
        if required:
            raise
        return None
    status = "removed" if result else "already_absent"
    stages.append(LifecycleStageResult(name, status))
    return result


def _remove_verified_optional(
    action: Callable[[], object],
    still_owned: Callable[[], bool],
    label: str,
) -> object:
    result = action()
    if still_owned():
        raise RuntimeError(f"Owned {label} remains after cleanup")
    return result


def _runtime_identity(preflight: LifecyclePreflight, path: Path) -> PathIdentity:
    for identity in preflight.owned_roots:
        if identity.path == path:
            return identity
    raise RuntimeError(f"No preflight identity exists for {path}")


def _record_recursive_root(
    stages: list[LifecycleStageResult],
    warnings: list[str],
    *,
    name: str,
    path: Path,
    romcloud_home: Path,
    preflight: LifecyclePreflight,
) -> None:
    identity = _runtime_identity(preflight, path)
    if not identity.exists:
        stages.append(LifecycleStageResult(name, "already_absent"))
        return
    if not identity.owned:
        detail = f"Preserved {path}: {identity.ownership_detail}"
        stages.append(LifecycleStageResult(name, "uncertain", detail))
        warnings.append(detail)
        return
    _record_stage(
        stages,
        name,
        lambda: _remove_owned_tree(
            path,
            romcloud_home=romcloud_home,
            protected=preflight.protected_roots,
            expected=identity,
        ),
    )


def _remove_owned_tree(
    path: Path,
    *,
    romcloud_home: Path,
    protected: tuple[Path, ...],
    expected: PathIdentity,
) -> bool:
    if not expected.owned:
        raise RuntimeError(f"Recursive deletion lacks positive ownership proof: {path}")
    _assert_path_identity(expected)
    if not expected.exists:
        return False
    home_owned = root_is_owned(romcloud_home, "home", romcloud_home)
    still_owned, detail = _root_ownership(
        romcloud_home=romcloud_home,
        path=path,
        kind=expected.ownership_kind,
        home_owned=home_owned,
    )
    if not still_owned:
        raise RuntimeError(f"Ownership changed before recursive deletion of {path}: {detail}")
    # Re-run the complete safety proof immediately before recursive deletion.
    _validate_owned_tree(
        path,
        protected=protected,
        ownership_kind=expected.ownership_kind,
        owned=True,
        ownership_detail=detail,
    )
    shutil.rmtree(path)
    if path.exists() or path.is_symlink():
        raise RuntimeError(f"Required lifecycle target remains after deletion: {path}")
    return True


def uninstall(
    *,
    config: AppConfig,
    romcloud_home: Path,
    ports_dir: Optional[Path] = None,
    config_trusted: bool = True,
    preflight: LifecyclePreflight | None = None,
) -> LifecycleReport:
    resolved_ports_dir = ports_dir or install.DEFAULT_PORTS_DIR
    from romcloud.web.lifecycle import stop_manager
    selected_preflight = preflight or lifecycle_preflight(
        operation="uninstall",
        config=config,
        romcloud_home=romcloud_home,
        config_trusted=config_trusted,
    )
    stages: list[LifecycleStageResult] = [
        LifecycleStageResult("preflight", "removed", "Safety authorization completed")
    ]
    warnings = list(selected_preflight.warnings) + list(selected_preflight.ownership_findings)
    proxies_removed = 0
    direct_links_removed = 0
    library_entries_removed = 0
    ownership_home = (
        romcloud_home
        if selected_preflight.config_trusted
        else Path("/userdata/system/romcloud")
    )
    romcloud_bin = ownership_home / "bin" / "romcloud"
    home_identity = (
        _runtime_identity(selected_preflight, romcloud_home)
        if selected_preflight.config_trusted
        else replace(
            _path_identity(romcloud_home),
            ownership_kind="home",
            owned=False,
            ownership_detail="configuration is untrusted",
        )
    )
    if (mount_service.SERVICE_SCRIPT_PATH.exists() or mount_service.SERVICE_SCRIPT_PATH.is_symlink()) and not mount_service.service_is_owned(
        mount_service.SERVICE_SCRIPT_PATH, romcloud_bin
    ):
        warnings.append(
            f"Preserved foreign or unverifiable startup service: {mount_service.SERVICE_SCRIPT_PATH}"
        )
    if (auto_savesync.HOOK_PATH.exists() or auto_savesync.HOOK_PATH.is_symlink()) and not auto_savesync.hook_is_owned(
        romcloud_bin, hook_path=auto_savesync.HOOK_PATH
    ):
        warnings.append(
            f"Preserved foreign or unverifiable Auto SaveSync hook: {auto_savesync.HOOK_PATH}"
        )
    if (es_config.ROMCLOUD_OVERRIDE_PATH.exists() or es_config.ROMCLOUD_OVERRIDE_PATH.is_symlink()) and not es_config.override_is_owned(
        es_config.ROMCLOUD_OVERRIDE_PATH,
        wrapper_path=ownership_home / "bin" / "romcloud-run",
    ):
        warnings.append(
            f"Preserved foreign or unverifiable ES override: {es_config.ROMCLOUD_OVERRIDE_PATH}"
        )
    icon = ownership_home / "ports-gfx" / "ports_gfx" / "assets" / "icon.png"
    ports_ownership = ports_gamelist_config.inspect_ownership(
        ports_dir=resolved_ports_dir,
        expected_wrapper=ownership_home / "bin" / "romcloud-ports",
        expected_icon=icon,
    )
    warnings.extend(ports_ownership.warnings)
    try:
        if selected_preflight.config_trusted:
            _record_stage(stages, "browser-manager-stop", lambda: stop_manager(config.data_path))
            _record_stage(
                stages,
                "auto-savesync-stop",
                lambda: auto_savesync.stop_menu_loop(Path(config.data_path)),
            )
            _record_stage(stages, "mount-worker-stop", lambda: mount_worker.stop_worker(romcloud_home))

            def unmount_all() -> bool:
                changed = False
                targets = {item.mount_point: item for item in selected_preflight.mounts}
                for target in reversed(mount_worker.configured_mounts(config)):
                    expected = targets.get(Path(target.mount_point))
                    if expected is None or not expected.mounted:
                        continue
                    changed = mountlib.unmount_cifs_source(
                        target.mount_point,
                        expected_server=target.smb.server,
                        expected_share=target.smb.share,
                        expected_read_only=target.read_only,
                        expected_remote_path=target.smb.remote_path,
                    ) or changed
                return changed

            _record_stage(stages, "verified-smb-unmount", unmount_all)

        _record_stage(
            stages,
            "startup-service",
            lambda: _remove_verified_optional(
                lambda: mount_service.remove_service(romcloud_bin),
                lambda: mount_service.service_is_owned(
                    mount_service.SERVICE_SCRIPT_PATH, romcloud_bin
                ),
                "startup service",
            ),
            required=False,
        )
        _record_stage(
            stages,
            "auto-savesync-hook",
            lambda: _remove_verified_optional(
                lambda: auto_savesync.remove_hook(romcloud_bin),
                lambda: auto_savesync.hook_is_owned(
                    romcloud_bin, hook_path=auto_savesync.HOOK_PATH
                ),
                "Auto SaveSync hook",
            ),
            required=False,
        )
        _record_stage(
            stages,
            "emulationstation",
            lambda: es_config.remove(wrapper_path=ownership_home / "bin" / "romcloud-run"),
            required=False,
        )
        _record_stage(
            stages,
            "ports",
            lambda: ports_gamelist_config.remove(
                ports_dir=resolved_ports_dir,
                expected_wrapper=ownership_home / "bin" / "romcloud-ports",
                expected_icon=icon,
            ),
            required=False,
        )

        if selected_preflight.config_trusted:
            from romcloud.integrations.batocera.game_access import remove_direct_links

            direct_report = remove_direct_links(config)
            direct_links_removed = direct_report.removed
            warnings.extend(direct_report.warnings)
            stages.append(
                LifecycleStageResult(
                    "direct-links",
                    "removed" if direct_report.removed else "uncertain" if direct_report.uncertain else "already_absent",
                    "; ".join(direct_report.warnings),
                )
            )
            if (Path(config.data_path) / "catalog.db").is_file():
                library_entries_removed = int(
                    _record_stage(
                        stages,
                        "library-presentation",
                        lambda: Container(config).library_sync.remove_local_metadata(),
                        required=False,
                    )
                    or 0
                )
                proxies_removed = int(
                    _record_stage(stages, "proxies", lambda: remove_owned_proxies(config), required=False)
                    or 0
                )
            else:
                stages.append(LifecycleStageResult("proxies", "uncertain", "Catalog ownership evidence unavailable"))
            if home_identity.owned:
                _record_stage(
                    stages,
                    "mount-runtime-state",
                    lambda: mount_worker.cleanup_runtime_state(romcloud_home) or True,
                )
            else:
                stages.append(
                    LifecycleStageResult(
                        "mount-runtime-state",
                        "uncertain",
                        "Preserved because ROMCloud home ownership is unproven.",
                    )
                )

            from romcloud.web.browser_runtime import (
                managed_runtime_is_owned,
                remove_managed_runtime,
                runtime_root,
            )

            browser_root = runtime_root(config.data_path)
            browser_identity = _runtime_identity(selected_preflight, browser_root)
            if not browser_identity.exists:
                stages.append(LifecycleStageResult("managed-browser", "already_absent"))
            elif not browser_identity.owned:
                detail = f"Preserved unverified managed-browser runtime: {browser_root}"
                stages.append(LifecycleStageResult("managed-browser", "uncertain", detail))
                warnings.append(detail)
            else:
                _assert_path_identity(browser_identity)
                if not managed_runtime_is_owned(config.data_path):
                    raise RuntimeError(
                        f"Managed-browser ownership changed before deletion: {browser_root}"
                    )
                _record_stage(
                    stages,
                    "managed-browser",
                    lambda: remove_managed_runtime(config.data_path),
                )

            for name in ("bin", "venv", "ports-gfx", "runtime"):
                path = romcloud_home / name
                _record_recursive_root(
                    stages,
                    warnings,
                    name=f"runtime-{name}",
                    path=path,
                    romcloud_home=romcloud_home,
                    preflight=selected_preflight,
                )
            version = romcloud_home / "version.json"
            if home_identity.owned and version.is_file() and not version.is_symlink():
                version.unlink()
                stages.append(LifecycleStageResult("build-metadata", "removed"))
            elif version.exists() or version.is_symlink():
                detail = f"Preserved unverified build metadata: {version}"
                warnings.append(detail)
                stages.append(LifecycleStageResult("build-metadata", "uncertain", detail))
            else:
                stages.append(LifecycleStageResult("build-metadata", "already_absent"))
            run_dir = romcloud_home / "run"
            if not home_identity.owned and run_dir.exists():
                detail = f"Preserved unverified transient runtime directory: {run_dir}"
                warnings.append(detail)
                stages.append(LifecycleStageResult("transient-runtime", "uncertain", detail))
            else:
                try:
                    run_dir.rmdir()
                    stages.append(LifecycleStageResult("transient-runtime", "removed"))
                except FileNotFoundError:
                    stages.append(LifecycleStageResult("transient-runtime", "already_absent"))
                except OSError as exc:
                    warnings.append(f"Preserved non-empty transient runtime directory: {run_dir}: {exc}")
                    stages.append(LifecycleStageResult("transient-runtime", "warning", str(exc)))
    except Exception as exc:
        report = LifecycleReport(
            proxies_removed,
            0,
            direct_links_removed,
            library_entries_removed,
            0,
            tuple(stages),
            tuple(warnings),
        )
        raise LifecycleFailure(str(exc), report) from exc

    return LifecycleReport(
        proxies_removed=proxies_removed,
        direct_links_removed=direct_links_removed,
        library_entries_removed=library_entries_removed,
        stages=tuple(stages),
        warnings=tuple(dict.fromkeys(warnings)),
    )


def purge(
    *,
    config: AppConfig,
    romcloud_home: Path,
    ports_dir: Optional[Path] = None,
    config_trusted: bool = True,
) -> LifecycleReport:
    selected_preflight = lifecycle_preflight(
        operation="purge",
        config=config,
        romcloud_home=romcloud_home,
        config_trusted=config_trusted,
    )
    if not config_trusted:
        return uninstall(
            config=config,
            romcloud_home=romcloud_home,
            ports_dir=ports_dir,
            config_trusted=False,
            preflight=selected_preflight,
        )

    report = uninstall(
        config=config,
        romcloud_home=romcloud_home,
        ports_dir=ports_dir,
        config_trusted=True,
        preflight=selected_preflight,
    )
    stages = list(report.stages)
    warnings = list(report.warnings)
    media_removed = 0
    try:
        catalog = Path(config.data_path) / "catalog.db"
        if catalog.is_file():
            media_removed, media_warnings = Container(config).library_sync.remove_owned_local_media()
            warnings.extend(media_warnings)
            stages.append(
                LifecycleStageResult(
                    "library-media",
                    "removed" if media_removed else "uncertain" if media_warnings else "already_absent",
                    "; ".join(media_warnings),
                )
            )

        home_identity = _runtime_identity(selected_preflight, romcloud_home)
        credentials_parent = config.credentials_path.parent.resolve(strict=False)
        expected_config_dir = (romcloud_home / "config").resolve(strict=False)
        if home_identity.owned and credentials_parent == expected_config_dir:
            _record_stage(stages, "credentials-auth", lambda: _remove_credential_files(config) or True)
        else:
            detail = f"Preserved credentials at unverified location: {config.credentials_path}"
            stages.append(LifecycleStageResult("credentials-auth", "uncertain", detail))
            warnings.append(detail)

        persistent_roots = {Path(config.cache.path), Path(config.data_path)}
        for root in sorted(persistent_roots, key=lambda value: len(value.parts), reverse=True):
            _record_recursive_root(
                stages,
                warnings,
                name=f"persistent-{root.name}",
                path=root,
                romcloud_home=romcloud_home,
                preflight=selected_preflight,
            )

        if (
            home_identity.owned
            and config.logging.path
            and _is_within(Path(config.logging.path), romcloud_home)
        ):
            log_dir = Path(config.logging.path)
            removed_log = False
            for name in (
                "romcloud.log",
                "romcloud.log.1",
                "romcloud.log.2",
                "romcloud.log.3",
                "startup-service.log",
                "mount-worker.log",
                "gui-display.log",
                "auto-savesync-lifecycle.log",
                "browser-controller.log",
            ):
                path = log_dir / name
                if path.is_file() and not path.is_symlink():
                    path.unlink()
                    removed_log = True
            stages.append(LifecycleStageResult("logs", "removed" if removed_log else "already_absent"))

        # Configuration is removed narrowly.  The home itself is never
        # recursively deleted here: unknown siblings must survive even when
        # the ROMCloud home ledger is valid.
        if home_identity.owned:
            for path in (
                romcloud_home / "config" / "romcloud.toml",
                romcloud_home / "config" / "owned-roots.json",
            ):
                if path.is_file() and not path.is_symlink():
                    path.unlink()
        if home_identity.owned:
            for directory in (
                romcloud_home / "config",
                romcloud_home / "logs",
                romcloud_home / "run",
                romcloud_home,
            ):
                try:
                    directory.rmdir()
                except FileNotFoundError:
                    continue
                except OSError as exc:
                    warnings.append(f"Preserved non-empty lifecycle directory {directory}: {exc}")
        stages.append(
            LifecycleStageResult(
                "romcloud-home",
                "removed" if not romcloud_home.exists() else "uncertain",
                "Unknown or unverifiable children were preserved."
                if romcloud_home.exists()
                else "",
            )
        )
    except Exception as exc:
        failed = LifecycleReport(
            report.proxies_removed,
            report.proxies_restored,
            report.direct_links_removed,
            report.library_entries_removed,
            media_removed,
            tuple(stages),
            tuple(dict.fromkeys(warnings)),
        )
        raise LifecycleFailure(str(exc), failed) from exc
    return LifecycleReport(
        proxies_removed=report.proxies_removed,
        proxies_restored=report.proxies_restored,
        direct_links_removed=report.direct_links_removed,
        library_entries_removed=report.library_entries_removed,
        library_media_removed=media_removed,
        stages=tuple(stages),
        warnings=tuple(dict.fromkeys(warnings)),
    )
