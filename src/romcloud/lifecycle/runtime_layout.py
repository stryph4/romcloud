"""Conservative reconciliation of pre-beta ROMCloud operational paths.

Only exact historical defaults are considered. Mount points are unmounted
before removal and are removed only with ``rmdir`` (therefore only when
empty). The legacy cache is moved only when its ROMCloud-owned ``.partial``
marker exists. Remote synchronized data is never deleted or migrated.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from romcloud.infrastructure import mount, mount_worker
from romcloud.infrastructure.config import (
    load_config,
    migrate_legacy_storage_config,
    tomllib,
)
from romcloud.infrastructure.logging import get_logger

log = get_logger("runtime-layout")

LEGACY_SOURCE_ROOT = Path("/userdata/romcloud-source")
LEGACY_REMOTE_MOUNT = Path("/userdata/romcloud-saves-source")
LEGACY_CACHE_ROOT = Path("/userdata/romcloud-cache")

SOURCE_ROOT = Path("/userdata/romcloud/source")
CACHE_ROOT = Path("/userdata/romcloud/cache")


def reconcile_legacy_runtime_layout(config_path: Path) -> bool:
    """Best-effort migration of exact ROMCloud-owned legacy defaults.

    Returns True when the configuration was rewritten. Filesystem cleanup is
    deliberately best-effort and never prevents the exact-default config
    migration from taking effect.
    """
    if not config_path.exists():
        return False
    try:
        raw_config = config_path.read_text(encoding="utf-8")
        raw_data = tomllib.loads(raw_config)
        config_changed = migrate_legacy_storage_config(
            config_path,
            legacy_source_root=str(LEGACY_SOURCE_ROOT),
            source_root=str(SOURCE_ROOT),
            legacy_cache_root=str(LEGACY_CACHE_ROOT),
            cache_root=str(CACHE_ROOT),
        )
        config = load_config(str(config_path))
    except Exception:  # noqa: BLE001 - reconciliation must not break update/repair
        return False

    source_raw = raw_data.get("source", {})
    cache_raw = raw_data.get("cache", {})
    legacy_source_configured = isinstance(source_raw, dict) and (
        source_raw.get("rom_root") == str(LEGACY_SOURCE_ROOT)
    )
    saves_raw = raw_data.get("saves", {})
    legacy_remote_configured = isinstance(saves_raw, dict) and (
        saves_raw.get("remote_mount_path") == str(LEGACY_REMOTE_MOUNT)
        or (
            "remote_mount_path" not in saves_raw
            and saves_raw.get("remote_subdir") == "romcloud-saves"
        )
    )
    legacy_cache_configured = isinstance(cache_raw, dict) and (
        cache_raw.get("path") == str(LEGACY_CACHE_ROOT)
    )
    if not (
        legacy_source_configured
        or legacy_remote_configured
        or legacy_cache_configured
    ):
        return config_changed

    romcloud_home = Path(config.data_path).parent
    if legacy_source_configured or legacy_remote_configured:
        mount_worker.stop_worker(romcloud_home)

    if legacy_remote_configured:
        if _unmount_legacy_path(LEGACY_REMOTE_MOUNT):
            _remove_empty(LEGACY_REMOTE_MOUNT)
        else:
            log.warning(
                "Could not unmount obsolete legacy remote-data path: %s",
                LEGACY_REMOTE_MOUNT,
            )

    if legacy_source_configured:
        if _unmount_legacy_path(LEGACY_SOURCE_ROOT) and _remove_empty(
            LEGACY_SOURCE_ROOT
        ):
            SOURCE_ROOT.parent.mkdir(parents=True, exist_ok=True)

    if legacy_cache_configured:
        if LEGACY_CACHE_ROOT.exists():
            if (LEGACY_CACHE_ROOT / ".partial").is_dir() and not CACHE_ROOT.exists():
                CACHE_ROOT.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(LEGACY_CACHE_ROOT), str(CACHE_ROOT))
            else:
                log.warning(
                    "Leaving legacy cache path untouched because ownership or destination "
                    "state is ambiguous: %s",
                    LEGACY_CACHE_ROOT,
                )

    return config_changed


def _unmount_legacy_path(path: Path) -> bool:
    try:
        mount.unmount_cifs_source(str(path))
    except Exception:  # noqa: BLE001 - do not rewrite while an old mount may remain active
        return False
    return True


def _remove_empty(path: Path) -> bool:
    try:
        path.rmdir()
    except FileNotFoundError:
        pass
    except OSError:
        return False
    return True
