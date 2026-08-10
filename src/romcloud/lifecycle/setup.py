"""Backend workflow used by the system-Python graphical setup wizard."""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from romcloud.bootstrap.container import Container
from romcloud.infrastructure.atomic_file import atomic_write_text
from romcloud.infrastructure.config import (
    AppConfig,
    CacheConfig,
    LoggingConfig,
    RemoteDataConfig,
    SavesConfig,
    SMBConfig,
    SourceConfig,
    paths_overlap,
    load_config,
    write_config,
)
from romcloud.infrastructure.credentials import (
    load_remote_data_smb_password,
    load_smb_password,
    write_cifs_credentials_file,
    write_remote_data_smb_password,
    write_smb_password,
)
from romcloud.infrastructure.mount import mount_cifs_source
from romcloud.infrastructure import mount_worker
from romcloud.infrastructure.smb_discovery_client import build_default_smb_discovery_service
from romcloud.integrations.batocera import es_config, mount_service
from romcloud.services.smb_discovery import SMBCredentials, SMBServerTarget

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
            rom_root=str(payload.get("rom_root", DEFAULT_ROM_ROOT)).strip(),
            cache_root=str(payload.get("cache_root", DEFAULT_CACHE_ROOT)).strip(),
            max_size_gb=_number(payload.get("max_size_gb", DEFAULT_MAX_SIZE_GB), "Maximum cache size"),
            min_free_gb=_number(payload.get("min_free_gb", DEFAULT_MIN_FREE_GB), "Minimum free space"),
            port=port,
            remote_data_type=str(payload.get("remote_data_type", "none")).strip().lower(),
            remote_data_root=str(payload.get("remote_data_root", "")).strip(),
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
        )
        request.validate(require_share=require_share, validate_cache=validate_cache)
        return request

    def validate(self, *, require_share: bool = True, validate_cache: bool = True) -> None:
        if not self.server:
            raise ValueError("SMB server is required.")
        if require_share and not self.share:
            raise ValueError("SMB share is required.")
        if not self.username:
            raise ValueError("SMB username is required.")
        if not self.password:
            raise ValueError("SMB password is required.")
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
        )
        if any('"' in value or "\n" in value or "\r" in value for value in serialized_values):
            raise ValueError("Setup values cannot contain quotes or line breaks.")
        if not 1 <= self.port <= 65535:
            raise ValueError("SMB port must be between 1 and 65535.")
        if self.remote_data_type not in {"none", "local", "smb"}:
            raise ValueError("ROMCloud data storage type must be none, local, or smb.")
        if self.remote_data_type == "local":
            if not self.remote_data_root or not Path(self.remote_data_root).is_absolute():
                raise ValueError("Local ROMCloud data location must be an absolute path.")
        if self.remote_data_type == "smb":
            if not all((self.remote_server, self.remote_share, self.remote_username, self.remote_password)):
                raise ValueError("Remote-data SMB server, share, username, and password are required.")
            if not 1 <= self.remote_port <= 65535:
                raise ValueError("Remote-data SMB port must be between 1 and 65535.")
            if (
                self.server.casefold() == self.remote_server.casefold()
                and self.share.strip("/").casefold()
                == self.remote_share.strip("/").casefold()
            ):
                raise ValueError(
                    "ROMCloud data must use a separate writable share from the ROM library."
                )
        if validate_cache and self.max_size_gb <= 0:
            raise ValueError("Maximum cache size must be greater than zero.")
        if validate_cache and self.min_free_gb < 0:
            raise ValueError("Minimum free space cannot be negative.")

        rom_root = Path(self.rom_root)
        cache_root = Path(self.cache_root)
        if not rom_root.is_absolute() or not cache_root.is_absolute():
            raise ValueError("ROM and cache paths must be absolute.")
        if validate_cache and (cache_root == rom_root or rom_root in cache_root.parents):
            raise ValueError("Cache location cannot be inside the mounted ROM source.")
        if self.remote_data_type != "none":
            remote_root = (
                Path(self.remote_data_root)
                if self.remote_data_type == "local"
                else Path(DEFAULT_REMOTE_DATA_ROOT)
            )
            for label, other in (("ROM source", rom_root), ("cache", cache_root)):
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
    state_path = config_path.parent / SETUP_STATE_FILENAME
    saved_state = _read_state(state_path)
    if not config_path.exists():
        if saved_state.get("status") in ("applying", "failed"):
            failed_step = str(saved_state.get("failed_step") or saved_state.get("step") or "setup")
            return {
                "state": "partial",
                "issues": [f"Setup did not finish at: {failed_step}."],
                "source_type": None,
                "failed_step": failed_step,
            }
        return {"state": "fresh", "issues": [], "source_type": None}

    try:
        config = load_config(str(config_path))
    except Exception as exc:  # noqa: BLE001 - malformed config is a repairable state
        return {
            "state": "partial",
            "issues": [f"Configuration could not be loaded: {exc}"],
            "source_type": None,
        }

    issues = _structural_issues(config)
    if saved_state.get("status") in ("applying", "failed"):
        failed_step = str(saved_state.get("failed_step") or saved_state.get("step") or "setup")
        issues.append(f"Setup did not finish at: {failed_step}.")

    payload: dict[str, Any] = {
        "state": "partial" if issues else "configured",
        "issues": issues,
        "source_type": "smb" if config.smb is not None else "local",
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
        })
    if config.remote_data is not None and config.remote_data.smb is not None:
        payload.update({
            "remote_server": config.remote_data.smb.server,
            "remote_share": config.remote_data.smb.share,
            "remote_username": config.remote_data.smb.username,
            "remote_port": config.remote_data.smb.port,
        })
    return payload


def _structural_issues(config: AppConfig) -> list[str]:
    issues = []
    if not Path(config.source.rom_root).is_absolute():
        issues.append("ROM source path must be absolute.")
    if not Path(config.cache.path).is_absolute():
        issues.append("Cache path must be absolute.")
    if config.cache.max_size_gb <= 0:
        issues.append("Maximum cache size must be greater than zero.")
    if config.cache.min_free_gb < 0:
        issues.append("Minimum free space cannot be negative.")
    if config.smb is not None and load_smb_password(config.credentials_path) is None:
        issues.append("SMB credentials are missing.")
    if config.remote_data is not None:
        if not Path(config.remote_data.root).is_absolute():
            issues.append("ROMCloud data root must be absolute.")
        if (
            config.remote_data.provider == "smb"
            and load_remote_data_smb_password(config.credentials_path) is None
        ):
            issues.append("Remote-data SMB credentials are missing.")
    return issues


def discover_shares(payload: dict[str, Any]) -> dict[str, Any]:
    server, port, username, password = _connection_values(payload)
    discovery = build_default_smb_discovery_service()
    target = SMBServerTarget(server, port)
    credentials = SMBCredentials(username, password)

    reachability = discovery.validate_server(target)
    if not reachability.ok:
        raise ValueError(_redact(reachability.detail, password) or "SMB server is unreachable.")
    authentication = discovery.authenticate(target, credentials)
    if not authentication.ok:
        raise ValueError(
            _redact(authentication.detail, password)
            or str(authentication.error_kind or "Authentication failed.")
        )
    result = discovery.list_shares(target, credentials)
    if not result.ok:
        raise ValueError(
            _redact(result.detail, password)
            or str(result.error_kind or "No shares found.")
        )
    return {
        "shares": [
            {"name": share.name, "comment": share.comment}
            for share in result.shares
        ],
    }


def validate_share(payload: dict[str, Any]) -> dict[str, Any]:
    purpose = str(payload.get("purpose", "source"))
    server, port, username, password = _connection_values(payload)
    share_key = "remote_share" if purpose == "remote_data" else "share"
    share = str(payload.get(share_key, "")).strip()
    if not share:
        raise ValueError("SMB share is required.")
    discovery = build_default_smb_discovery_service()
    target = SMBServerTarget(server, port)
    credentials = SMBCredentials(username, password)
    validation = discovery.validate_share(target, credentials, share)
    if not validation.ok:
        raise ValueError(
            _redact(validation.detail, password)
            or str(validation.error_kind or "Share validation failed.")
        )
    if purpose == "remote_data":
        return {
            "systems": [],
            "count": 0,
            "validation": {"connected": True, "read_verified": True},
        }
    detection = discovery.detect_systems(validation)
    return {
        "systems": list(detection.detected_systems),
        "count": detection.count,
        "validation": {"connected": True, "read_verified": True},
    }


def apply_setup(config_path: Path, payload: dict[str, Any]) -> dict[str, Any]:
    request = SetupRequest.from_payload(payload)
    validation_result = validate_share(payload)
    if request.remote_data_type == "smb":
        validate_share({**payload, "purpose": "remote_data"})
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
    config = _build_config(config_path, request, existing)
    mounted_during_setup: list[str] = []

    step = "write configuration"
    _write_state(state_path, {"status": "applying", "step": step})
    try:
        write_config(config, str(config_path))
        write_smb_password(config.credentials_path, request.password)
        if request.remote_data_type == "smb":
            write_remote_data_smb_password(
                config.credentials_path, request.remote_password
            )

        step = "install mount service"
        _write_state(state_path, {"status": "applying", "step": step})
        romcloud_bin = config_path.parent.parent / "bin" / "romcloud"
        mount_service.install_service(str(romcloud_bin))

        step = "mount and test storage"
        _write_state(state_path, {"status": "applying", "step": step})
        for target in mount_worker.configured_mounts(config):
            password, cifs_path = mount_worker.credentials_for_mount(config, target)
            assert password is not None
            write_cifs_credentials_file(cifs_path, target.smb.username, password)
            outcome = mount_cifs_source(
                target.smb.server,
                target.smb.share,
                target.mount_point,
                cifs_path,
                read_only=target.read_only,
                port=target.smb.port,
            )
            if outcome is not None and not outcome.already_mounted:
                mounted_during_setup.append(target.mount_point)

        container = Container(config)
        source_probe = container.provider.validate_access(config.source.rom_root)
        if not source_probe.ok:
            raise RuntimeError(
                f"ROM library access validation failed: {source_probe.detail}"
            )

        remote_probe = None
        if config.remote_data is not None:
            if config.remote_data.provider == "local":
                Path(config.remote_data.root).mkdir(parents=True, exist_ok=True)
            remote_probe = container.saves.validate_remote_storage()
            if not remote_probe.ok:
                raise RuntimeError(
                    "Configured ROMCloud data location failed validation: "
                    f"{remote_probe.detail}"
                )

        step = "refresh catalog"
        _write_state(state_path, {"status": "applying", "step": step})
        refresh_result = container.catalog.refresh()
        if refresh_result.errors:
            details = "; ".join(f"{system}: {message}" for system, message in refresh_result.errors)
            raise RuntimeError(details)

        step = "update EmulationStation integration"
        _write_state(state_path, {"status": "applying", "step": step})
        managed_systems = container.game_repo.list_systems()
        es_config.install(managed_systems)
    except Exception as exc:
        from romcloud.infrastructure.mount import unmount_cifs_source

        cleanup_errors: list[str] = []
        for mount_point in reversed(mounted_during_setup):
            try:
                unmount_cifs_source(mount_point)
            except Exception as cleanup_exc:  # noqa: BLE001 - try every new mount
                cleanup_errors.append(f"{mount_point}: {cleanup_exc}")
        safe_error = str(exc).replace(request.password, "***")
        if request.remote_password:
            safe_error = safe_error.replace(request.remote_password, "***")
        if cleanup_errors:
            cleanup_detail = "; ".join(cleanup_errors).replace(
                request.password, "***"
            )
            if request.remote_password:
                cleanup_detail = cleanup_detail.replace(request.remote_password, "***")
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
        raise RuntimeError(f"{step}: {safe_error}") from exc

    state_path.unlink(missing_ok=True)
    return {
        "source_type": "smb",
        "server": request.server,
        "share": request.share,
        "systems": validation_result["systems"],
        "system_count": validation_result["count"],
        "max_size_gb": request.max_size_gb,
        "remote_data_type": request.remote_data_type,
        "source_validation": source_probe.as_dict(),
        "remote_data_validation": (
            remote_probe.as_dict() if remote_probe is not None else None
        ),
    }


def _existing_config(config_path: Path) -> AppConfig | None:
    try:
        return load_config(str(config_path))
    except Exception:  # noqa: BLE001 - a broken config is replaced by setup
        return None


def _build_config(config_path: Path, request: SetupRequest, existing: AppConfig | None) -> AppConfig:
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
            ),
        )
    return AppConfig(
        source=SourceConfig(provider="local", rom_root=request.rom_root),
        cache=CacheConfig(
            path=request.cache_root,
            max_size_gb=request.max_size_gb,
            min_free_gb=request.min_free_gb,
        ),
        local_roms_path=existing.local_roms_path if existing else "/userdata/roms",
        data_path=existing.data_path if existing else str(home / "data"),
        logging=existing.logging if existing else LoggingConfig(level="INFO", path=str(home / "logs")),
        smb=SMBConfig(
            server=request.server,
            share=request.share,
            username=request.username,
            port=request.port,
        ),
        remote_data=remote_data,
        saves=existing.saves if existing else SavesConfig(),
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


def _redact(detail: str, *secrets: str) -> str:
    for secret in secrets:
        if secret:
            detail = detail.replace(secret, "***")
    return detail


def _read_state(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_state(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(path, json.dumps(payload, sort_keys=True) + "\n", mode=0o600)
