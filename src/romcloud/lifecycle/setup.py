"""Backend workflow used by the system-Python graphical setup wizard."""

from __future__ import annotations

import json
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from romcloud.bootstrap.container import Container
from romcloud.core.progress import ProgressSink, emit_progress
from romcloud.infrastructure.atomic_file import atomic_write_text
from romcloud.infrastructure.config import (
    AppConfig,
    CacheConfig,
    LoggingConfig,
    LibrarySyncConfig,
    RemoteDataConfig,
    SavesConfig,
    SMBConfig,
    SFTPConfig,
    SourceConfig,
    SMART_CACHE_MODE,
    DIRECT_NAS_MODE,
    canonical_system_ids,
    is_absolute_config_path,
    paths_overlap,
    load_config,
    write_config,
)
from romcloud.infrastructure.credentials import (
    describe_protection,
    load_remote_data_smb_password,
    load_smb_password,
    write_remote_data_sftp_password,
    write_remote_data_smb_password,
    write_sftp_password,
    write_smb_password,
)
from romcloud.infrastructure import mount_worker
from romcloud.infrastructure.smb_discovery_client import build_default_smb_discovery_service
from romcloud.integrations.batocera import es_config, mount_service
from romcloud.integrations.batocera.systems import BATOCERA_SYSTEMS
from romcloud.services.smb_discovery import SMBErrorKind, SMBCredentials, SMBServerTarget
from romcloud.services.directory_browser import (
    browse_local_directory,
    normalize_remote_directory,
    normalize_sftp_directory,
    remote_parent,
    sftp_parent,
)

DEFAULT_ROM_ROOT = "/userdata/romcloud/source"
DEFAULT_REMOTE_DATA_ROOT = "/userdata/romcloud/remote"
DEFAULT_CACHE_ROOT = "/userdata/romcloud/cache"
DEFAULT_MAX_SIZE_GB = 50.0
DEFAULT_MIN_FREE_GB = 5.0
SETUP_STATE_FILENAME = "setup-state.json"


@dataclass(frozen=True)
class SetupRequest:
    server: str
    share: str
    username: str
    password: str = field(repr=False)
    source_type: str = "smb"
    source_remote_path: str = ""
    rom_root: str = DEFAULT_ROM_ROOT
    cache_root: str = DEFAULT_CACHE_ROOT
    max_size_gb: float = DEFAULT_MAX_SIZE_GB
    min_free_gb: float = DEFAULT_MIN_FREE_GB
    port: int = 445
    remote_data_type: str = "none"
    remote_data_root: str = ""
    remote_server: str = ""
    remote_share: str = ""
    remote_username: str = ""
    remote_password: str = field(default="", repr=False)
    remote_port: int = 445
    remote_reuse_source_credentials: bool = False
    remote_remote_path: str = ""
    sftp_host_key_fingerprint: str = ""
    remote_sftp_host_key_fingerprint: str = ""
    game_access_mode: str = SMART_CACHE_MODE
    library_sync_enabled: bool = False
    selected_systems: tuple[str, ...] | None = None

    @classmethod
    def from_payload(
        cls,
        payload: dict[str, Any],
        *,
        require_share: bool = True,
        validate_cache: bool = True,
    ) -> "SetupRequest":
        server = str(payload.get("server", "")).strip()
        username = str(payload.get("username", "")).strip()
        password = str(payload.get("password", ""))
        port = int(payload.get("port", 445))
        reuse_remote = bool(payload.get("remote_reuse_source_credentials", False))
        request = cls(
            server=server,
            share=str(payload.get("share", "")).strip(),
            username=username,
            password=password,
            source_type=str(payload.get("source_type", "smb")).strip().lower(),
            source_remote_path=_source_remote_path(payload),
            rom_root=str(payload.get("rom_root", DEFAULT_ROM_ROOT)).strip(),
            cache_root=str(payload.get("cache_root", DEFAULT_CACHE_ROOT)).strip(),
            max_size_gb=_number(payload.get("max_size_gb", DEFAULT_MAX_SIZE_GB), "Maximum cache size"),
            min_free_gb=_number(payload.get("min_free_gb", DEFAULT_MIN_FREE_GB), "Minimum free space"),
            port=port,
            remote_data_type=str(payload.get("remote_data_type", "none")).strip().lower(),
            remote_data_root=_remote_data_root(payload),
            remote_server=(
                server if reuse_remote else str(payload.get("remote_server", "")).strip()
            ),
            remote_share=str(payload.get("remote_share", "")).strip(),
            remote_username=(
                username if reuse_remote else str(payload.get("remote_username", "")).strip()
            ),
            remote_password=(
                password if reuse_remote else str(payload.get("remote_password", ""))
            ),
            remote_port=(port if reuse_remote else int(payload.get("remote_port", 445))),
            remote_reuse_source_credentials=reuse_remote,
            remote_remote_path=normalize_remote_directory(str(payload.get("remote_remote_path", ""))),
            sftp_host_key_fingerprint=str(payload.get("sftp_host_key_fingerprint", "")).strip(),
            remote_sftp_host_key_fingerprint=str(payload.get("remote_sftp_host_key_fingerprint", "")).strip(),
            game_access_mode=str(payload.get("game_access_mode", SMART_CACHE_MODE)).strip().lower(),
            library_sync_enabled=bool(payload.get("library_sync_enabled", False)),
            selected_systems=(
                canonical_system_ids(payload["selected_systems"], "setup request")
                if "selected_systems" in payload
                else None
            ),
        )
        request.validate(require_share=require_share, validate_cache=validate_cache)
        return request

    def validate(self, *, require_share: bool = True, validate_cache: bool = True) -> None:
        if self.source_type not in {"none", "local", "smb", "sftp"}:
            raise ValueError("ROM source type must be none, local, smb, or sftp.")
        if self.game_access_mode not in {SMART_CACHE_MODE, DIRECT_NAS_MODE}:
            raise ValueError("Game access mode must be smart_cache or direct_nas.")
        if self.source_type == "smb":
            if not self.server:
                raise ValueError("SMB server is required.")
            if require_share and not self.share:
                raise ValueError("SMB share is required.")
            if not self.username:
                raise ValueError("SMB username is required.")
            if not self.password:
                raise ValueError("SMB password is required.")
        if self.source_type == "sftp":
            if not all((self.server, self.username, self.password, self.source_remote_path)):
                raise ValueError("SFTP server, username, password, and remote path are required.")
            if not self.source_remote_path.startswith("/"):
                raise ValueError("SFTP ROM path must be an absolute remote path.")
            if not self.sftp_host_key_fingerprint:
                raise ValueError("Trust the SFTP server host key before continuing.")
            if not 1 <= self.port <= 65535:
                raise ValueError("SFTP port must be between 1 and 65535.")
        serialized_values = (
            self.server,
            self.share,
            self.username,
            self.rom_root,
            self.cache_root,
            self.remote_data_root,
            self.remote_server,
            self.remote_share,
            self.remote_username,
            self.source_remote_path,
            self.remote_remote_path,
        )
        if any('"' in value or "\n" in value or "\r" in value for value in serialized_values):
            raise ValueError("Setup values cannot contain quotes or line breaks.")
        if self.source_type == "smb" and not 1 <= self.port <= 65535:
            raise ValueError("SMB port must be between 1 and 65535.")
        if self.remote_data_type not in {
            "none", "local", "smb", "sftp", "google_drive"
        }:
            raise ValueError(
                "ROMCloud data storage type must be none, local, smb, sftp, "
                "or google_drive."
            )
        if self.library_sync_enabled and self.remote_data_type == "none":
            raise ValueError("Library Sync requires writable ROMCloud data storage.")
        if self.library_sync_enabled and self.source_type == "none":
            raise ValueError("Library Sync requires ROMCloud game management.")
        if self.library_sync_enabled and self.remote_data_type == "google_drive":
            raise ValueError("Google Drive Library Sync is not implemented.")
        if self.remote_data_type == "local":
            if not self.remote_data_root or not is_absolute_config_path(self.remote_data_root):
                raise ValueError("Local ROMCloud data location must be an absolute path.")
        if self.remote_data_type == "smb":
            if not all((self.remote_server, self.remote_share, self.remote_username, self.remote_password)):
                raise ValueError("Remote-data SMB server, share, username, and password are required.")
            if not 1 <= self.remote_port <= 65535:
                raise ValueError("Remote-data SMB port must be between 1 and 65535.")
            if self.source_type == "smb" and (
                self.server.casefold() == self.remote_server.casefold()
                and self.share.strip("/").casefold()
                == self.remote_share.strip("/").casefold()
            ):
                raise ValueError(
                    "ROMCloud data must use a separate writable share from the ROM library."
                )
        if self.remote_data_type == "sftp":
            if not all((self.remote_server, self.remote_username, self.remote_password, self.remote_data_root)):
                raise ValueError("Remote-data SFTP server, username, password, and path are required.")
            if not self.remote_data_root.startswith("/"):
                raise ValueError("Remote-data SFTP path must be an absolute remote path.")
            if not self.remote_sftp_host_key_fingerprint:
                raise ValueError("Trust the remote-data SFTP server host key before continuing.")
            if not 1 <= self.remote_port <= 65535:
                raise ValueError("Remote-data SFTP port must be between 1 and 65535.")
        if validate_cache and self.max_size_gb <= 0:
            raise ValueError("Maximum cache size must be greater than zero.")
        if validate_cache and self.min_free_gb < 0:
            raise ValueError("Minimum free space cannot be negative.")

        rom_root = Path(self.rom_root) if self.source_type != "none" else None
        cache_root = Path(self.cache_root)
        if self.source_type != "none" and (
            rom_root is None or not is_absolute_config_path(self.rom_root)
        ):
            raise ValueError("ROM path must be absolute.")
        if validate_cache and not is_absolute_config_path(self.cache_root):
            raise ValueError("ROM and cache paths must be absolute.")
        if validate_cache and rom_root is not None and (
            cache_root == rom_root or rom_root in cache_root.parents
        ):
            raise ValueError("Cache location cannot be inside the mounted ROM source.")
        if self.source_type != "none" and self.game_access_mode == DIRECT_NAS_MODE and paths_overlap(
            rom_root, Path("/userdata/roms")  # type: ignore[arg-type]
        ):
            raise ValueError(
                "Connected Mode ROM source must be separate from /userdata/roms."
            )
        if self.remote_data_type not in {"none", "google_drive"}:
            remote_root = (
                Path(self.remote_data_root)
                if self.remote_data_type == "local"
                else Path(DEFAULT_REMOTE_DATA_ROOT)
            )
            compared = (
                []
                if self.source_type == "none"
                else [("ROM source", rom_root), ("cache", cache_root)]
            )
            for label, other in compared:
                if paths_overlap(remote_root, other):
                    raise ValueError(
                        f"ROMCloud data location cannot overlap the {label}."
                    )

        if not validate_cache:
            return
        usage_path = cache_root
        while not usage_path.exists() and usage_path != usage_path.parent:
            usage_path = usage_path.parent
        total_gb = shutil.disk_usage(usage_path).total / (1024 ** 3)
        if self.min_free_gb >= total_gb:
            raise ValueError("Minimum free space must be less than total available storage.")


def _number(value: Any, label: str) -> float:
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be a number.") from exc


def setup_state(config_path: Path) -> dict[str, Any]:
    from romcloud.lifecycle.google_drive_setup import (
        google_drive_build_status,
        google_drive_experimental_enabled,
    )

    google_drive_experimental = google_drive_experimental_enabled()
    google_drive_status = (
        google_drive_build_status(config_path)
        if google_drive_experimental
        else {}
    )
    google_drive_state = {
        "google_drive_experimental": google_drive_experimental,
        **google_drive_status,
    }
    state_path = config_path.parent / SETUP_STATE_FILENAME
    saved_state = _read_state(state_path)
    if not config_path.exists():
        if saved_state.get("status") in ("applying", "failed"):
            failed_step = str(saved_state.get("failed_step") or saved_state.get("step") or "setup")
            return {
                **google_drive_state,
                "state": "partial",
                "issues": [f"Setup did not finish at: {failed_step}."],
                "source_type": None,
                "failed_step": failed_step,
            }
        return {
            **google_drive_state,
            "state": "fresh",
            "issues": [],
            "source_type": None,
        }

    try:
        config = load_config(str(config_path))
    except Exception as exc:  # noqa: BLE001 - malformed config is a repairable state
        return {
            **google_drive_state,
            "state": "partial",
            "issues": [f"Configuration could not be loaded: {exc}"],
            "source_type": None,
        }

    issues = _structural_issues(config)
    if saved_state.get("status") in ("applying", "failed"):
        failed_step = str(saved_state.get("failed_step") or saved_state.get("step") or "setup")
        issues.append(f"Setup did not finish at: {failed_step}.")

    from romcloud.infrastructure.capabilities import capability_policy
    from romcloud.infrastructure.library_view import offline_library_enabled

    operating_state = capability_policy(config).serialize()
    operating_state["game_management_enabled"] = config.source.enabled
    payload: dict[str, Any] = {
        **google_drive_state,
        "state": "partial" if issues else "configured",
        "issues": issues,
        "source_type": (
            "none"
            if not config.source.enabled
            else "smb"
            if config.smb is not None
            else "sftp"
            if config.source.provider == "sftp"
            else "local"
        ),
        "game_access_mode": config.game_access_mode,
        "update_channel": config.update_channel,
        "library_sync_enabled": config.library_sync.enabled,
        "selected_systems": (
            list(config.source.selected_systems)
            if config.source.selected_systems is not None
            else None
        ),
        "offline_library_mode": offline_library_enabled(config),
        "operating_state": operating_state,
        "rom_root": config.source.rom_root,
        "cache_root": config.cache.path,
        "max_size_gb": config.cache.max_size_gb,
        "min_free_gb": config.cache.min_free_gb,
        "remote_data_type": config.remote_data.provider if config.remote_data else "none",
        "remote_data_root": (
            config.remote_data.root
            if config.remote_data and config.remote_data.provider == "local"
            else ""
        ),
        "failed_step": saved_state.get("failed_step"),
    }
    if config.smb is not None:
        payload.update({
            "server": config.smb.server,
            "share": config.smb.share,
            "username": config.smb.username,
            "port": config.smb.port,
            "source_remote_path": config.smb.remote_path,
        })
    if config.remote_data is not None and config.remote_data.smb is not None:
        payload.update({
            "remote_server": config.remote_data.smb.server,
            "remote_share": config.remote_data.smb.share,
            "remote_username": config.remote_data.smb.username,
            "remote_port": config.remote_data.smb.port,
            "remote_remote_path": config.remote_data.smb.remote_path,
        })
    return payload


def _structural_issues(config: AppConfig) -> list[str]:
    issues = []
    if config.source.enabled and not is_absolute_config_path(config.source.rom_root):
        issues.append("ROM source path must be absolute.")
    if config.source.enabled and not is_absolute_config_path(config.cache.path):
        issues.append("Cache path must be absolute.")
    if config.source.enabled and config.cache.max_size_gb <= 0:
        issues.append("Maximum cache size must be greater than zero.")
    if config.source.enabled and config.cache.min_free_gb < 0:
        issues.append("Minimum free space cannot be negative.")
    if config.smb is not None and load_smb_password(config.credentials_path) is None:
        issues.append("SMB credentials are missing.")
    if config.remote_data is not None:
        if (
            config.remote_data.provider != "google_drive"
            and not is_absolute_config_path(config.remote_data.root)
        ):
            issues.append("ROMCloud data root must be absolute.")
        if (
            config.remote_data.provider == "smb"
            and load_remote_data_smb_password(config.credentials_path) is None
        ):
            issues.append("Remote-data SMB credentials are missing.")
    return issues


def discover_shares(
    payload: dict[str, Any], progress: ProgressSink = None
) -> dict[str, Any]:
    server, port, username, password = _connection_values(payload)
    discovery = build_default_smb_discovery_service()
    target = SMBServerTarget(server, port)
    credentials = SMBCredentials(username, password)

    emit_progress(progress, "configure", "connect", "running", f"Connecting to {server}…")
    reachability = discovery.validate_server(target)
    if not reachability.ok:
        emit_progress(progress, "configure", "connect", "error", "Could not reach the server.", detail=_redact(reachability.detail, password))
        raise ValueError(_redact(reachability.detail, password) or "SMB server is unreachable.")
    emit_progress(progress, "configure", "connect", "success", "Connected to server")
    authentication = discovery.authenticate(target, credentials)
    if not authentication.ok:
        emit_progress(progress, "configure", "authenticate", "error", "Authentication failed.", detail=_redact(authentication.detail, password))
        raise ValueError(
            _redact(authentication.detail, password)
            or str(authentication.error_kind or "Authentication failed.")
        )
    emit_progress(progress, "configure", "authenticate", "success", "Authentication successful")
    result = discovery.list_shares(target, credentials)
    if not result.ok:
        emit_progress(
            progress,
            "configure",
            "shares",
            "error",
            "Could not list accessible shares.",
            detail=_redact(result.detail, password),
        )
        raise ValueError(
            _redact(result.detail, password)
            or str(result.error_kind or "No shares found.")
        )
    emit_progress(
        progress,
        "configure",
        "shares",
        "success",
        f"Connection successful — {len(result.shares)} share{'s' if len(result.shares) != 1 else ''} found",
    )
    return {
        "shares": [
            {"name": share.name, "comment": share.comment}
            for share in result.shares
        ],
    }


def validate_share(
    payload: dict[str, Any], progress: ProgressSink = None
) -> dict[str, Any]:
    purpose = str(payload.get("purpose", "source"))
    server, port, username, password = _connection_values(payload)
    share_key = "remote_share" if purpose == "remote_data" else "share"
    share = str(payload.get(share_key, "")).strip()
    if not share:
        raise ValueError("SMB share is required.")
    discovery = build_default_smb_discovery_service()
    target = SMBServerTarget(server, port)
    credentials = SMBCredentials(username, password)
    path_key = "remote_remote_path" if purpose == "remote_data" else "source_remote_path"
    remote_path = normalize_remote_directory(str(payload.get(path_key, "")))
    emit_progress(progress, "configure", "directory", "running", "Checking the selected folder…")
    validation = (
        discovery.browse_directory(target, credentials, share, remote_path)
        if remote_path
        else discovery.validate_share(target, credentials, share)
    )
    if not validation.ok:
        detail = _redact(validation.detail, password)
        emit_progress(
            progress,
            "configure",
            "directory",
            "error",
            _friendly_smb_error(validation.error_kind),
            detail=detail,
        )
        raise ValueError(
            _friendly_smb_error(validation.error_kind)
        )
    emit_progress(progress, "configure", "directory", "success", "Directory accessible")
    emit_progress(progress, "configure", "read", "success", "Read access verified")
    if purpose == "remote_data":
        return {
            "systems": [],
            "count": 0,
            "validation": {"connected": True, "read_verified": True},
        }
    detection = discovery.detect_systems(validation)
    emit_progress(
        progress,
        "configure",
        "systems",
        "success",
        f"Library check complete — {detection.count} system{'s' if detection.count != 1 else ''} found",
    )
    return {
        "systems": list(detection.detected_systems),
        "count": detection.count,
        "validation": {"connected": True, "read_verified": True},
    }


def browse_smb_directory(
    payload: dict[str, Any], progress: ProgressSink = None
) -> dict[str, Any]:
    """List one share-relative SMB directory after connectivity/auth checks."""
    purpose = str(payload.get("purpose", "source"))
    server, port, username, password = _connection_values(payload)
    share_key = "remote_share" if purpose == "remote_data" else "share"
    path_key = "remote_remote_path" if purpose == "remote_data" else "source_remote_path"
    share = str(payload.get(share_key, "")).strip()
    path = normalize_remote_directory(str(payload.get(path_key, "")))
    if not share:
        raise ValueError("SMB share is required.")
    discovery = build_default_smb_discovery_service()
    target = SMBServerTarget(server, port)
    credentials = SMBCredentials(username, password)
    emit_progress(progress, "browse", "connect", "running", f"Opening //{server}/{share}…")
    reachability = discovery.validate_server(target)
    if not reachability.ok:
        emit_progress(
            progress,
            "browse",
            "connect",
            "error",
            "Could not reach the server.",
            detail=_redact(reachability.detail, password),
        )
        raise ValueError(_redact(reachability.detail, password) or "SMB server is unreachable.")
    authentication = discovery.authenticate(target, credentials)
    if not authentication.ok:
        emit_progress(
            progress,
            "browse",
            "authenticate",
            "error",
            "Authentication failed.",
            detail=_redact(authentication.detail, password),
        )
        raise ValueError(_redact(authentication.detail, password) or "Authentication failed.")
    result = discovery.browse_directory(target, credentials, share, path)
    if not result.ok:
        detail = _redact(result.detail, password)
        emit_progress(
            progress,
            "browse",
            "directory",
            "error",
            _friendly_smb_error(result.error_kind),
            detail=detail,
        )
        raise ValueError(
            _friendly_smb_error(result.error_kind)
        )
    emit_progress(progress, "browse", "directory", "success", "Folder contents loaded")
    entries = result.entries or tuple(
        type("Directory", (), {"name": name, "is_directory": True})()
        for name in result.top_level_entries
    )
    return {
        "path": path,
        "parent": remote_parent(path),
        "entries": [
            {
                "name": entry.name,
                "is_directory": bool(entry.is_directory),
            }
            for entry in entries
        ],
    }


def browse_local(payload: dict[str, Any]) -> dict[str, object]:
    return browse_local_directory(str(payload.get("path", "/userdata")))


def validate_local_source(
    payload: dict[str, Any], progress: ProgressSink = None
) -> dict[str, Any]:
    root = str(payload.get("rom_root", "")).strip()
    emit_progress(progress, "configure", "directory", "running", "Checking the selected folder…")
    from romcloud.infrastructure.providers.local import LocalFilesystemProvider

    provider = LocalFilesystemProvider()
    validation = provider.validate_access(root)
    if not validation.ok:
        emit_progress(
            progress,
            "configure",
            "directory",
            "error",
            "Could not access the selected ROM folder.",
            detail=validation.detail,
        )
        raise ValueError(validation.detail or "Could not access the selected folder.")
    emit_progress(progress, "configure", "directory", "success", "Directory accessible")
    emit_progress(progress, "configure", "read", "success", "Read access verified")
    systems = [
        system
        for system in canonical_system_ids(
            provider.list_systems(root), "detected systems"
        )
        if system in BATOCERA_SYSTEMS
    ]
    emit_progress(
        progress,
        "configure",
        "systems",
        "success",
        f"Library check complete — {len(systems)} system{'s' if len(systems) != 1 else ''} found",
    )
    return {
        "systems": systems,
        "count": len(systems),
        "validation": validation.as_dict(),
    }


def probe_sftp_host_key(payload: dict[str, Any]) -> dict[str, str]:
    """Return the host key for explicit wizard trust before credentials use."""
    from romcloud.infrastructure.providers.sftp import probe_host_key

    connection = _sftp_setup_connection(payload)
    host = connection["host"]
    port = connection["port"]
    if not host:
        raise ValueError("SFTP server is required.")
    key_type, fingerprint = probe_host_key(host, port)
    return {"host_key_type": key_type, "host_key_fingerprint": fingerprint}


def validate_sftp_source(
    payload: dict[str, Any], progress: ProgressSink = None
) -> dict[str, Any]:
    """Validate an SFTP target using its existing strict host-key provider."""
    from romcloud.infrastructure.providers.sftp import SFTPProvider

    connection = _sftp_setup_connection(payload)
    remote = connection["purpose"] == "remote_data"
    root = normalize_sftp_directory(
        str(payload.get("remote_data_root" if remote else "source_remote_path", ""))
    )
    provider = SFTPProvider(
        host=connection["host"],
        username=connection["username"],
        password=connection["password"],
        port=connection["port"],
        trusted_host_key_fingerprint=connection["trusted_fingerprint"],
        probe_writable=remote,
    )
    emit_progress(progress, "configure", "directory", "running", "Checking the selected SFTP folder…")
    try:
        validation = provider.validate_access(root)
    except Exception as exc:
        raise ValueError(
            _sftp_operation_diagnostic(
                "detect systems access check", connection, root, exc
            )
        ) from None
    if not validation.connected or not validation.read_verified:
        detail = validation.detail or "Could not access the selected SFTP folder."
        raise ValueError(
            _sftp_operation_diagnostic(
                "detect systems access check",
                connection,
                root,
                RuntimeError(detail),
            )
        ) from None
    emit_progress(progress, "configure", "directory", "success", "Directory accessible")
    emit_progress(progress, "configure", "read", "success", "Read access verified")
    if remote:
        return {"systems": [], "count": 0, "validation": validation.as_dict()}
    try:
        directory_names = provider.list_systems(root)
    except Exception as exc:
        raise ValueError(
            _sftp_operation_diagnostic("detect systems listing", connection, root, exc)
        ) from None
    # Storage providers enumerate directories, not pre-validated configuration
    # values. Ignore NAS metadata and unrelated folders before applying strict
    # Batocera-ID validation (for example Synology's ``@eaDir``).
    systems = list(
        canonical_system_ids(
            [
                name.strip().lower()
                for name in directory_names
                if isinstance(name, str)
                and name.strip().lower() in BATOCERA_SYSTEMS
            ],
            "detected systems",
        )
    )
    emit_progress(
        progress, "configure", "systems", "success",
        f"Library check complete — {len(systems)} system{'s' if len(systems) != 1 else ''} found",
    )
    return {"systems": systems, "count": len(systems), "validation": validation.as_dict()}


def browse_sftp_directory(
    payload: dict[str, Any], progress: ProgressSink = None
) -> dict[str, Any]:
    """List directories at one absolute, account-visible SFTP path."""
    from romcloud.infrastructure.providers.sftp import SFTPProvider

    connection = _sftp_setup_connection(payload)
    path = normalize_sftp_directory(str(payload.get("sftp_browse_path", "/")))
    provider = SFTPProvider(
        host=connection["host"],
        username=connection["username"],
        password=connection["password"],
        port=connection["port"],
        trusted_host_key_fingerprint=connection["trusted_fingerprint"],
        probe_writable=False,
    )
    emit_progress(progress, "browse", "directory", "running", f"Opening {path}…")
    try:
        directories = provider.list_systems(path)
    except Exception as exc:
        raise ValueError(
            _sftp_operation_diagnostic("browser", connection, path, exc)
        ) from None
    emit_progress(progress, "browse", "directory", "success", "Folder contents loaded")
    return {
        "path": path,
        "parent": sftp_parent(path),
        "entries": [
            {"name": name, "is_directory": True}
            for name in directories
        ],
    }


def apply_setup(
    config_path: Path, payload: dict[str, Any], progress: ProgressSink = None
) -> dict[str, Any]:
    requested_mode = str(payload.get("game_access_mode", SMART_CACHE_MODE)).lower()
    requested_source = str(payload.get("source_type", "smb")).strip().lower()
    request = SetupRequest.from_payload(
        payload,
        validate_cache=(
            requested_source != "none" and requested_mode != DIRECT_NAS_MODE
        ),
    )
    validation_result = (
        {"systems": [], "count": 0}
        if request.source_type == "none"
        else validate_share(payload, progress)
        if request.source_type == "smb"
        else validate_sftp_source(payload, progress)
        if request.source_type == "sftp"
        else validate_local_source(payload, progress)
    )
    detected_systems = canonical_system_ids(
        validation_result["systems"], "detected systems"
    )
    selected_systems = (
        detected_systems
        if request.selected_systems is None
        else request.selected_systems
    )
    unknown_systems = sorted(set(selected_systems) - set(detected_systems))
    if unknown_systems:
        raise ValueError(
            "Selected systems were not detected in this source: "
            + ", ".join(unknown_systems)
        )
    if request.remote_data_type == "smb":
        validate_share({**payload, "purpose": "remote_data"}, progress)
    elif request.remote_data_type == "sftp":
        validate_sftp_source({**payload, "purpose": "remote_data"}, progress)
    state_path = config_path.parent / SETUP_STATE_FILENAME
    existing = _existing_config(config_path)
    existing_was_valid = existing is not None and not _structural_issues(existing)
    previous_config = config_path.read_bytes() if existing_was_valid else None
    previous_credentials_path = existing.credentials_path if existing_was_valid else None
    previous_credentials = (
        previous_credentials_path.read_bytes()
        if previous_credentials_path is not None and previous_credentials_path.exists()
        else None
    )
    config = _build_config(
        config_path, request, existing, selected_systems=selected_systems
    )
    _guard_active_direct_save_provider_change(existing, config)
    mounted_during_setup: list[str] = []
    direct_save_routing = None

    step = "write configuration"
    emit_progress(progress, "configure", "save", "running", "Saving configuration…")
    _write_state(state_path, {"status": "applying", "step": step})
    try:
        write_config(config, str(config_path))
        if request.source_type == "smb":
            write_smb_password(config.credentials_path, request.password)
        elif request.source_type == "sftp":
            write_sftp_password(config.credentials_path, request.password)
        if request.remote_data_type == "smb":
            write_remote_data_smb_password(
                config.credentials_path, request.remote_password
            )
        elif request.remote_data_type == "sftp":
            write_remote_data_sftp_password(
                config.credentials_path, request.remote_password
            )

        emit_progress(progress, "configure", "save", "success", "Configuration saved")
        step = "install mount service"
        _write_state(state_path, {"status": "applying", "step": step})
        romcloud_bin = config_path.parent.parent / "bin" / "romcloud"
        mount_service.install_service(
            str(romcloud_bin),
            activation_state_path=mount_service.startup_activation.state_path(
                config_path.parent.parent
            ),
        )

        step = "mount and test storage"
        emit_progress(progress, "configure", "mount", "running", "Connecting storage…")
        _write_state(state_path, {"status": "applying", "step": step})
        for target in mount_worker.configured_mounts(config):
            password = mount_worker.credentials_for_mount(config, target)
            assert password is not None
            outcome = mount_worker.mount_configured_target(config, target, password)
            if outcome is not None and not outcome.already_mounted:
                mounted_during_setup.append(target.mount_point)
        if mount_worker.configured_mounts(config):
            emit_progress(progress, "configure", "mount", "success", "Mounted successfully")
            protection = describe_protection(config.credentials_path, "smb")
            if protection:
                emit_progress(
                    progress, "configure", "credential_protection", "success", protection
                )

        container = Container(config)
        source_probe = None
        if config.source.enabled:
            source_probe = container.provider.validate_access(config.source.rom_root)
            if not source_probe.ok:
                raise RuntimeError(
                    f"ROM library access validation failed: {source_probe.detail}"
                )
            emit_progress(progress, "configure", "read", "success", "ROM read access verified")

        remote_probe = None
        save_sync_state = None
        if config.remote_data is not None:
            if config.remote_data.provider == "local":
                Path(config.remote_data.root).mkdir(parents=True, exist_ok=True)
            remote_probe = container.saves.validate_remote_storage()
            if not remote_probe.connected or not remote_probe.read_verified:
                raise RuntimeError(
                    "Configured ROMCloud data location failed validation: "
                    f"{remote_probe.detail}"
                )
            if remote_probe.ok:
                emit_progress(progress, "configure", "write", "success", "Write test created")
                emit_progress(progress, "configure", "read_back", "success", "Read-back verified")
                emit_progress(progress, "configure", "cleanup", "success", "Test file removed")
            else:
                raise RuntimeError(
                    "Configured ROMCloud data location is not writable: "
                    f"{remote_probe.detail}"
                )

        step = "refresh catalog"
        emit_progress(progress, "configure", "catalog", "running", "Refreshing the game catalog…")
        _write_state(state_path, {"status": "applying", "step": step})
        refresh_result = (
            container.catalog.refresh(progress=progress)
            if config.source.enabled
            else None
        )
        if refresh_result is not None and refresh_result.errors:
            details = "; ".join(f"{system}: {message}" for system, message in refresh_result.errors)
            raise RuntimeError(details)
        emit_progress(progress, "configure", "catalog", "success", "Catalog refreshed")

        step = "update EmulationStation integration"
        _write_state(state_path, {"status": "applying", "step": step})
        from romcloud.integrations.batocera.game_access import reconcile_game_access

        emit_progress(
            progress,
            "configure",
            "emulationstation",
            "running",
            "Preparing games for EmulationStation…",
        )
        if (
            config.remote_data is not None
            and container.saves._remote_supports_durable_transactions()  # noqa: SLF001
        ):
            step = "initialize SaveSync"
            _write_state(state_path, {"status": "applying", "step": step})
            emit_progress(
                progress,
                "configure",
                "savesync_initialize",
                "running",
                "Initializing SaveSync with an Initial Full Sync…",
            )
            container.saves.full_sync(progress=progress)
            save_sync_state = container.saves.get_state()
            if (
                not save_sync_state.quick_sync_ready
                or save_sync_state.quick_sync_cursor_generation is None
            ):
                raise RuntimeError(
                    "Initial Full Sync completed without establishing a trusted "
                    "Quick Sync baseline"
                )
            emit_progress(
                progress,
                "configure",
                "savesync_initialize",
                "success",
                "Initial Full Sync complete — Auto SaveSync baseline ready",
            )
        # SaveSync must establish its baseline before Direct routing hides the
        # local directories behind emulator-visible remote bind mounts.
        if config.source.enabled:
            from romcloud.core.capabilities import OperatingMode
            from romcloud.infrastructure.library_view import operating_mode

            if operating_mode(config) is OperatingMode.CONNECTED:
                from romcloud.core.exceptions import SaveAuthorityConflictError
                from romcloud.integrations.batocera.direct_saves import (
                    DirectSaveRouting,
                )
                from romcloud.infrastructure.library_view import write_operating_mode

                direct_save_routing = DirectSaveRouting(
                    config,
                    container.saves.selection_policy,
                    container.saves.filesystem_remote_root,
                )
                direct_conflicts = tuple(
                    conflict
                    for conflict in container.saves.get_state().active_conflicts
                    if conflict.layout_id in direct_save_routing.layout_ids
                )
                if direct_conflicts:
                    # Keep the newly configured installation usable with local
                    # ownership. The GUI can now reuse the normal Direct-mode
                    # Resolve / remote-wins / Cancel decision flow.
                    write_operating_mode(config, OperatingMode.CACHE)
                    reconcile_game_access(config, render_library_metadata=False)
                    raise SaveAuthorityConflictError(
                        "Unresolved Save Conflicts: Direct Mode was not activated. "
                        "Resolve them, explicitly use remote saves, or remain in "
                        "Cached Storage.",
                        conflict_ids=tuple(
                            conflict.conflict_id for conflict in direct_conflicts
                        ),
                    )
                direct_save_routing.activate()
            # Optional metadata/media enrichment remains a post-setup action.
            reconcile_game_access(config, render_library_metadata=False)
        emit_progress(
            progress,
            "configure",
            "emulationstation",
            "success",
            "EmulationStation entries prepared",
        )
        emit_progress(progress, "configure", "complete", "success", "ROMCloud setup complete")
    except Exception as exc:
        from romcloud.core.exceptions import SaveAuthorityConflictError

        if isinstance(exc, SaveAuthorityConflictError):
            state_path.unlink(missing_ok=True)
            emit_progress(
                progress,
                "configure",
                step,
                "warning",
                "Setup is configured in Cached Storage pending a save-conflict decision.",
                detail=str(exc),
            )
            raise
        from romcloud.infrastructure.mount import unmount_cifs_source

        cleanup_errors: list[str] = []
        if direct_save_routing is not None and direct_save_routing.active:
            try:
                direct_save_routing.deactivate()
            except Exception as cleanup_exc:  # noqa: BLE001
                cleanup_errors.append(f"Direct save routing: {cleanup_exc}")
        for mount_point in reversed(mounted_during_setup):
            try:
                unmount_cifs_source(mount_point)
            except Exception as cleanup_exc:  # noqa: BLE001 - try every new mount
                cleanup_errors.append(f"{mount_point}: {cleanup_exc}")
        safe_error = _redact(str(exc), request.password, request.remote_password)
        if cleanup_errors:
            cleanup_detail = _redact(
                "; ".join(cleanup_errors), request.password, request.remote_password
            )
            safe_error += f"; mount cleanup failed: {cleanup_detail}"
        if previous_config is not None:
            config_path.write_bytes(previous_config)
            if previous_credentials_path is not None:
                if previous_credentials is None:
                    previous_credentials_path.unlink(missing_ok=True)
                else:
                    previous_credentials_path.write_bytes(previous_credentials)
                    previous_credentials_path.chmod(0o600)
            if cleanup_errors:
                _write_state(
                    state_path,
                    {"status": "failed", "failed_step": step, "error": safe_error},
                )
            else:
                state_path.unlink(missing_ok=True)
        else:
            _write_state(state_path, {"status": "failed", "failed_step": step, "error": safe_error})
        emit_progress(
            progress,
            "configure",
            step,
            "error",
            "Setup could not be completed.",
            detail=safe_error,
        )
        raise RuntimeError(f"{step}: {safe_error}") from exc

    state_path.unlink(missing_ok=True)
    return {
        "source_type": request.source_type,
        "game_access_mode": request.game_access_mode,
        "server": request.server,
        "share": request.share,
        "systems": validation_result["systems"],
        "system_count": validation_result["count"],
        "selected_systems": list(selected_systems),
        "selected_system_count": len(selected_systems),
        "max_size_gb": request.max_size_gb,
        "remote_data_type": request.remote_data_type,
        "library_sync_enabled": request.library_sync_enabled,
        "source_validation": (
            source_probe.as_dict() if source_probe is not None else None
        ),
        "remote_data_validation": (
            remote_probe.as_dict() if remote_probe is not None else None
        ),
        "save_sync_initialized": save_sync_state is not None,
        "quick_sync_ready": (
            save_sync_state.quick_sync_ready if save_sync_state is not None else False
        ),
        "quick_sync_cursor_generation": (
            save_sync_state.quick_sync_cursor_generation
            if save_sync_state is not None
            else None
        ),
    }


def _existing_config(config_path: Path) -> AppConfig | None:
    try:
        return load_config(str(config_path))
    except Exception:  # noqa: BLE001 - a broken config is replaced by setup
        return None


def _guard_active_direct_save_provider_change(
    existing: AppConfig | None, requested: AppConfig
) -> None:
    """Never strand owned bind mounts by rewriting their path/provider identity."""
    if existing is None:
        return
    from romcloud.integrations.batocera.direct_saves import MANIFEST_FILENAME

    manifest = Path(existing.data_path) / MANIFEST_FILENAME
    identity_changed = (
        existing.data_path != requested.data_path
        or existing.saves.local_path != requested.saves.local_path
        or existing.remote_data != requested.remote_data
        or existing.source.selected_systems != requested.source.selected_systems
    )
    if os.path.lexists(manifest) and identity_changed:
        raise ValueError(
            "Direct save routing is active. Switch to Cached Storage first so "
            "remote saves are materialized locally before changing the remote-data "
            "provider, save paths, or selected systems."
        )


def _build_config(
    config_path: Path,
    request: SetupRequest,
    existing: AppConfig | None,
    *,
    selected_systems: tuple[str, ...] | None = None,
) -> AppConfig:
    home = config_path.parent.parent
    remote_data = None
    if request.remote_data_type == "local":
        remote_data = RemoteDataConfig(provider="local", root=request.remote_data_root)
    elif request.remote_data_type == "smb":
        remote_data = RemoteDataConfig(
            provider="smb",
            root=DEFAULT_REMOTE_DATA_ROOT,
            smb=SMBConfig(
                server=request.remote_server,
                share=request.remote_share,
                username=request.remote_username,
                port=request.remote_port,
                remote_path=request.remote_remote_path,
            ),
        )
    elif request.remote_data_type == "sftp":
        remote_data = RemoteDataConfig(
            provider="sftp",
            root=request.remote_data_root,
            sftp=SFTPConfig(
                host=request.remote_server,
                username=request.remote_username,
                port=request.remote_port,
                host_key_fingerprint=request.remote_sftp_host_key_fingerprint,
            ),
        )
    elif request.remote_data_type == "google_drive":
        remote_data = RemoteDataConfig(
            provider="google_drive",
            root="romcloud-savesync",
        )
    return AppConfig(
        source=SourceConfig(
            provider=(
                "none"
                if request.source_type == "none"
                else "sftp"
                if request.source_type == "sftp"
                else "local"
            ),
            rom_root=(
                ""
                if request.source_type == "none"
                else request.source_remote_path
                if request.source_type == "sftp"
                else request.rom_root
            ),
            selected_systems=(
                ()
                if request.source_type == "none"
                else request.selected_systems
                if selected_systems is None
                else selected_systems
            ),
        ),
        cache=CacheConfig(
            path=request.cache_root,
            max_size_gb=request.max_size_gb,
            min_free_gb=request.min_free_gb,
        ),
        local_roms_path=existing.local_roms_path if existing else "/userdata/roms",
        data_path=existing.data_path if existing else str(home / "data"),
        logging=existing.logging if existing else LoggingConfig(level="INFO", path=str(home / "logs")),
        smb=(
            SMBConfig(
                server=request.server,
                share=request.share,
                username=request.username,
                port=request.port,
                remote_path=request.source_remote_path,
            )
            if request.source_type == "smb"
            else None
        ),
        sftp=(
            SFTPConfig(
                host=request.server,
                username=request.username,
                port=request.port,
                host_key_fingerprint=request.sftp_host_key_fingerprint,
            )
            if request.source_type == "sftp"
            else None
        ),
        remote_data=remote_data,
        saves=existing.saves if existing else SavesConfig(),
        library_sync=LibrarySyncConfig(enabled=request.library_sync_enabled),
        game_access_mode=request.game_access_mode,
    )


def _connection_values(payload: dict[str, Any]) -> tuple[str, int, str, str]:
    purpose = str(payload.get("purpose", "source"))
    if purpose == "remote_data" and payload.get("remote_reuse_source_credentials"):
        server = str(payload.get("server", "")).strip()
        username = str(payload.get("username", "")).strip()
        password = str(payload.get("password", ""))
        port = int(payload.get("port", 445))
        if not server or not username or not password:
            raise ValueError("Source SMB credentials are required for reuse.")
        return server, port, username, password
    prefix = "remote_" if purpose == "remote_data" else ""
    server = str(payload.get(f"{prefix}server", "")).strip()
    username = str(payload.get(f"{prefix}username", "")).strip()
    password = str(payload.get(f"{prefix}password", ""))
    port = int(payload.get(f"{prefix}port", 445))
    if not server or not username or not password:
        raise ValueError("SMB server, username, and password are required.")
    return server, port, username, password


def _source_remote_path(payload: dict[str, Any]) -> str:
    raw = str(payload.get("source_remote_path", "")).strip()
    if str(payload.get("source_type", "smb")).strip().lower() == "sftp":
        return normalize_sftp_directory(raw)
    return normalize_remote_directory(raw)


def _remote_data_root(payload: dict[str, Any]) -> str:
    raw = str(payload.get("remote_data_root", "")).strip()
    if str(payload.get("remote_data_type", "none")).strip().lower() == "sftp":
        return normalize_sftp_directory(raw)
    return raw


def _sftp_setup_connection(payload: dict[str, Any]) -> dict[str, Any]:
    """Resolve one wizard SFTP role without mixing source and data fields.

    Host-key observation, directory browsing, and final validation all use
    this same extraction path so a wizard transition cannot silently switch
    credentials or fingerprints. Remote paths remain separate because the
    provider accepts absolute server-visible POSIX paths directly.
    """
    purpose = str(payload.get("purpose", "source")).strip().lower()
    if purpose not in ("source", "remote_data"):
        raise ValueError(f"Unsupported SFTP setup role: {purpose or 'missing'}")
    remote = purpose == "remote_data"
    prefix = "remote_" if remote else ""
    fingerprint_key = (
        "remote_sftp_host_key_fingerprint"
        if remote
        else "sftp_host_key_fingerprint"
    )
    return {
        "purpose": purpose,
        "host": str(payload.get(f"{prefix}server", "")).strip(),
        "port": int(payload.get(f"{prefix}port", 22)),
        "username": str(payload.get(f"{prefix}username", "")).strip(),
        "password": str(payload.get(f"{prefix}password", "")),
        "trusted_fingerprint": str(payload.get(fingerprint_key, "")).strip()
        or None,
    }


def _sftp_operation_diagnostic(
    operation: str,
    connection: dict[str, Any],
    path: str,
    exc: Exception,
) -> str:
    """Return password-safe context for one setup-time SFTP operation."""
    detail = _redact(str(exc), connection["password"])
    return (
        f"SFTP {operation} {type(exc).__name__}: {detail or '(no message)'}; "
        f"provider=SFTPProvider; role={connection['purpose']}; "
        f"host={connection['host']}; port={connection['port']}; "
        f"username={connection['username']}; "
        f"password_present={bool(connection['password'])}; "
        f"trusted_fingerprint={connection['trusted_fingerprint'] or 'missing'}; "
        f"path={path}"
    )


def _redact(detail: str, *secrets: str) -> str:
    for secret in secrets:
        if secret:
            detail = detail.replace(secret, "***")
    return detail


def _friendly_smb_error(kind: SMBErrorKind | None) -> str:
    messages = {
        SMBErrorKind.ACCESS_DENIED: (
            "ROMCloud connected to the server, but this account does not have "
            "permission to access the selected folder."
        ),
        SMBErrorKind.AUTH_FAILED: "Authentication failed. Check the username and password.",
        SMBErrorKind.SERVER_NOT_FOUND: "Could not find the server.",
        SMBErrorKind.CONNECTION_REFUSED: "Could not connect to the server.",
        SMBErrorKind.TIMEOUT: "The server took too long to respond.",
        SMBErrorKind.SHARE_UNAVAILABLE: "The selected share is no longer available.",
        SMBErrorKind.TOOL_UNAVAILABLE: "SMB browsing is unavailable on this system.",
    }
    return messages.get(kind, "Could not access the selected folder.")


def _read_state(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_state(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(path, json.dumps(payload, sort_keys=True) + "\n", mode=0o600)
