"""Repair, uninstall, and purge orchestration for ROMCloud-owned artifacts."""

from __future__ import annotations

import json
import shutil
import sqlite3
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from romcloud.bootstrap.container import Container
from romcloud.infrastructure import mount as mountlib
from romcloud.infrastructure import mount_worker
from romcloud.infrastructure.config import AppConfig
from romcloud.infrastructure.credentials import (
    cifs_credentials_path,
    remote_data_cifs_credentials_path,
)
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


@dataclass(frozen=True)
class LifecycleReport:
    proxies_removed: int = 0
    proxies_restored: int = 0
    direct_links_removed: int = 0
    library_entries_removed: int = 0


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
    return reconcile_report, LifecycleReport(
        proxies_restored=reconcile_report.proxies_restored
    )


_LEGACY_CREDENTIALS_FILENAME = "smb.credentials"


def _remove_credential_files(config: AppConfig) -> None:
    """Remove every persisted copy of SMB credentials on uninstall/purge.

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
        stale.unlink(missing_ok=True)


def uninstall(
    *,
    config: AppConfig,
    romcloud_home: Path,
    ports_dir: Optional[Path] = None,
) -> LifecycleReport:
    resolved_ports_dir = ports_dir or install.DEFAULT_PORTS_DIR
    # Stop the exact verified resident before removing its executable,
    # startup service, hook, or durable state. This never uses broad process
    # matching and cannot target an unrelated Batocera process.
    auto_savesync.stop_menu_loop(Path(config.data_path))
    mount_worker.stop_worker(romcloud_home)
    unmount_errors: list[str] = []
    if mount_worker.configured_mounts(config):
        for target in reversed(mount_worker.configured_mounts(config)):
            try:
                mountlib.unmount_cifs_source(target.mount_point)
            except Exception as exc:  # noqa: BLE001 - attempt every configured target
                unmount_errors.append(f"{target.label}: {exc}")
    if unmount_errors:
        raise RuntimeError(
            "Could not unmount all ROMCloud SMB locations; uninstall stopped "
            f"before removing runtime state: {'; '.join(unmount_errors)}"
        )
    mount_service.remove_service()
    es_config.remove()
    ports_gamelist_config.remove(ports_dir=resolved_ports_dir)
    auto_savesync.HOOK_PATH.unlink(missing_ok=True)
    from romcloud.integrations.batocera.game_access import remove_direct_links

    direct_links_removed = remove_direct_links(config).removed
    library_entries_removed = (
        Container(config).library_sync.remove_local_metadata()
        if (Path(config.data_path) / "catalog.db").is_file()
        else 0
    )
    proxies_removed = remove_owned_proxies(config)
    mount_worker.cleanup_runtime_state(romcloud_home)
    _remove_credential_files(config)

    for name in ("bin", "venv", "ports-gfx"):
        shutil.rmtree(romcloud_home / name, ignore_errors=True)
    (romcloud_home / "version.json").unlink(missing_ok=True)
    run_dir = romcloud_home / "run"
    try:
        run_dir.rmdir()
    except OSError:
        pass
    return LifecycleReport(
        proxies_removed=proxies_removed,
        direct_links_removed=direct_links_removed,
        library_entries_removed=library_entries_removed,
    )


def _validate_owned_tree(path: Path, *, protected: tuple[Path, ...]) -> None:
    resolved = path.resolve()
    forbidden = {Path("/"), Path("/userdata"), Path("/userdata/system")}
    if not path.is_absolute() or resolved in forbidden:
        raise RuntimeError(f"Refusing unsafe purge target: {path}")
    if any(resolved == item.resolve() or _is_within(item, resolved) for item in protected):
        raise RuntimeError(f"Refusing purge target that contains protected ROM data: {path}")


def _remove_owned_tree(path: Path, *, protected: tuple[Path, ...]) -> None:
    _validate_owned_tree(path, protected=protected)
    shutil.rmtree(path, ignore_errors=True)


def purge(
    *,
    config: AppConfig,
    romcloud_home: Path,
    ports_dir: Optional[Path] = None,
) -> LifecycleReport:
    protected = (Path(config.local_roms_path), Path(config.source.rom_root))
    if config.remote_data is not None:
        protected = (*protected, Path(config.remote_data.root))
    external_roots = {Path(config.cache.path), Path(config.data_path)}
    purge_roots = [
        root for root in external_roots
        if not _is_within(root, romcloud_home)
    ]
    for root in purge_roots:
        _validate_owned_tree(root, protected=(*protected, romcloud_home))
    _validate_owned_tree(romcloud_home, protected=protected)

    report = uninstall(config=config, romcloud_home=romcloud_home, ports_dir=ports_dir)
    for root in sorted(external_roots, key=lambda value: len(value.parts), reverse=True):
        if not _is_within(root, romcloud_home):
            _remove_owned_tree(root, protected=protected)
    if config.logging.path and not _is_within(Path(config.logging.path), romcloud_home):
        log_dir = Path(config.logging.path)
        for name in ("romcloud.log", "romcloud.log.1", "romcloud.log.2", "romcloud.log.3"):
            (log_dir / name).unlink(missing_ok=True)
    _remove_owned_tree(romcloud_home, protected=protected)
    return report
