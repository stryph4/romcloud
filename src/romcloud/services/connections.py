"""Shared connection lifecycle used by both CLI and graphical controls."""

from __future__ import annotations

from pathlib import Path

from romcloud.core.exceptions import ConfigurationError, ROMCloudError
from romcloud.core.progress import ProgressSink, emit_progress
from romcloud.infrastructure import mount, mount_worker


def connection_status(config) -> dict[str, object]:
    targets = mount_worker.configured_mounts(config)
    if not targets:
        root = Path(config.source.rom_root)
        connected = root.is_dir()
        return {
            "state": "connected" if connected else "disconnected",
            "configured": False,
            "source_provider": "local",
            "source": str(root),
            "mount_point": str(root),
            "targets": [],
            "error": "" if connected else "The selected local ROM folder is unavailable.",
        }

    home = mount_worker.romcloud_home_from_config(config)
    diagnostics = mount_worker.get_diagnostics(home, config)
    if diagnostics.mounted:
        state = "connected"
    elif diagnostics.worker_pid is not None:
        state = "connecting"
    elif diagnostics.last_state == "failed":
        state = "error"
    else:
        state = "disconnected"
    return {
        "state": state,
        "configured": True,
        "source_provider": "smb",
        "source": f"//{targets[0].smb.server}/{targets[0].smb.share}",
        "mount_point": targets[0].mount_point,
        "targets": [
            {
                "label": target.label,
                "source": f"//{target.smb.server}/{target.smb.share}",
                "remote_path": getattr(target.smb, "remote_path", ""),
                "mount_point": target.mount_point,
                "mode": "read-only" if target.read_only else "read-write",
            }
            for target in targets
        ],
        "error": diagnostics.last_detail if state == "error" else "",
    }


def mount_connections(
    config,
    progress: ProgressSink = None,
    *,
    mount_fn=None,
) -> dict[str, object]:
    targets = mount_worker.configured_mounts(config)
    if not targets:
        raise ConfigurationError("This configuration uses a local folder and does not need mounting.")
    mount_fn = mount_fn or mount.mount_cifs_source
    mounted_now: list[str] = []
    try:
        for target in targets:
            emit_progress(
                progress,
                "mount",
                "connect",
                "running",
                f"Connecting to {target.smb.server}…",
            )
            password = mount_worker.credentials_for_mount(config, target)
            if not password:
                raise ConfigurationError(
                    f"No SMB password stored for {target.label}; open setup to update credentials."
                )
            outcome = mount_worker.mount_configured_target(
                config, target, password, mount_fn=mount_fn
            )
            if not outcome.already_mounted:
                mounted_now.append(target.mount_point)
            emit_progress(
                progress,
                "mount",
                "mounted",
                "success",
                f"{target.label} connected",
            )
    except Exception as exc:
        emit_progress(
            progress,
            "mount",
            "connect",
            "error",
            _friendly_connection_error(exc),
            detail=str(exc),
        )
        for mount_point in reversed(mounted_now):
            try:
                mount.unmount_cifs_source(mount_point)
            except ROMCloudError:
                pass
        raise
    result = connection_status(config)
    result["changed"] = bool(mounted_now)
    return result


def unmount_connections(config, progress: ProgressSink = None) -> dict[str, object]:
    targets = mount_worker.configured_mounts(config)
    if not targets:
        raise ConfigurationError("This configuration uses a local folder and does not need unmounting.")
    mount_worker.stop_worker(mount_worker.romcloud_home_from_config(config))
    errors: list[str] = []
    changed = False
    for target in reversed(targets):
        emit_progress(
            progress,
            "unmount",
            "disconnect",
            "running",
            f"Disconnecting {target.label}…",
        )
        try:
            changed = mount.unmount_cifs_source(target.mount_point) or changed
            emit_progress(
                progress,
                "unmount",
                "disconnect",
                "success",
                f"{target.label} disconnected",
            )
        except ROMCloudError as exc:
            emit_progress(
                progress,
                "unmount",
                "disconnect",
                "error",
                "Could not disconnect one of the storage locations.",
                detail=str(exc),
            )
            errors.append(f"{target.label}: {exc}")
    if errors:
        raise ROMCloudError("; ".join(errors))
    result = connection_status(config)
    result["changed"] = changed
    return result


def _friendly_connection_error(exc: Exception) -> str:
    name = type(exc).__name__.lower()
    text = str(exc).lower()
    if "auth" in name or "permission denied" in text:
        return "The server rejected the stored credentials. Open Storage Setup to update them."
    if "reachable" in name or "unreachable" in text or "timed out" in text:
        return "Could not reach the storage server. Check the network and try again."
    if isinstance(exc, ConfigurationError):
        return str(exc)
    return "Could not access the selected storage location."
