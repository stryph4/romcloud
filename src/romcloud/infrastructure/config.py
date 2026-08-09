"""Application configuration.

User-facing configuration lives in a TOML file.  Credentials are stored in a
*separate* file with restricted filesystem permissions and are never loaded
into the main :class:`AppConfig` object.

Default installation path: ``/userdata/system/romcloud/config/romcloud.toml``
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

if sys.version_info >= (3, 11):
    import tomllib
else:
    try:
        import tomllib  # type: ignore[no-redef]
    except ImportError:
        try:
            import tomli as tomllib  # type: ignore[no-redef]
        except ImportError as exc:
            raise ImportError(
                "Python < 3.11 requires the 'tomli' package: pip install tomli"
            ) from exc

from romcloud.core.exceptions import ConfigurationError, ConfigurationNotFoundError
from romcloud.infrastructure.atomic_file import atomic_write_text
from romcloud.infrastructure.credentials import migrate_legacy_smb_credentials

# ── defaults ──────────────────────────────────────────────────────────────────

_DEFAULT_ROMCLOUD_HOME = Path("/userdata/system/romcloud")
_DEFAULT_CACHE_ROOT = Path("/userdata/romcloud-cache")
_DEFAULT_LOCAL_ROMS = Path("/userdata/roms")


# ── sub-configs ───────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class SourceConfig:
    provider: str
    """Storage provider implementation to use — currently always ``"local"``.

    ROMCloud has exactly one implemented storage provider
    (:class:`~romcloud.infrastructure.providers.local.LocalFilesystemProvider`). A
    network SMB/CIFS share is mounted locally first (see
    :mod:`romcloud.infrastructure.mount`) and then read through this same
    local provider — ``rom_root`` is its mount point. The native
    ``"smb"`` provider remains an unimplemented stub reserved for future
    direct-SMB support; configs loaded with ``provider = "smb"`` are
    transparently normalized to ``"local"`` (see :func:`load_config`).
    """
    rom_root: str
    """Absolute local filesystem path to the ROM root — either a plain
    local/USB directory, or the local mount point of an SMB share."""


@dataclass(frozen=True)
class SMBConfig:
    server: str
    share: str
    username: str = "guest"
    port: int = 445


@dataclass(frozen=True)
class CacheConfig:
    path: str
    max_size_gb: float = 50.0
    min_free_gb: float = 5.0


@dataclass(frozen=True)
class LoggingConfig:
    level: str = "INFO"
    path: Optional[str] = None


@dataclass(frozen=True)
class AppConfig:
    source: SourceConfig
    cache: CacheConfig
    local_roms_path: str
    data_path: str
    logging: LoggingConfig = field(default_factory=LoggingConfig)
    smb: Optional[SMBConfig] = None

    @property
    def credentials_path(self) -> Path:
        return Path(self.data_path).parent / "config" / "credentials.toml"


# ── parsing ───────────────────────────────────────────────────────────────────


def load_config(config_path: Optional[str] = None) -> AppConfig:
    """Load and validate configuration from a TOML file."""
    path = Path(config_path) if config_path else default_config_path()

    if not path.exists():
        raise ConfigurationNotFoundError(
            f"No configuration found at {path}. Run `romcloud configure` to set up."
        )

    try:
        with path.open("rb") as fh:
            data = tomllib.load(fh)
    except Exception as exc:
        raise ConfigurationError(f"Failed to parse config {path}: {exc}") from exc

    config = _parse(data, path)
    migrate_legacy_smb_credentials(config.credentials_path)
    return config


def _parse(data: dict, path: Path) -> AppConfig:  # noqa: C901
    try:
        src = data["source"]
        provider = src["provider"]
        rom_root = src["rom_root"]
    except KeyError as exc:
        raise ConfigurationError(f"Missing required config key: {exc}") from exc

    smb = None
    if "smb" in data:
        s = data["smb"]
        try:
            smb = SMBConfig(
                server=s["server"],
                share=s["share"],
                username=s.get("username", "guest"),
                port=int(s.get("port", 445)),
            )
        except KeyError as exc:
            raise ConfigurationError(f"Missing required [smb] key: {exc}") from exc

    if provider == "smb":
        # Legacy configs (written before the mounted-SMB model) used
        # provider = "smb" to mean "read ROMs from this SMB source" — but
        # the native SMBProvider is still an unimplemented stub, and
        # activating it here would crash every command with
        # NotImplementedError. At runtime an SMB source always means a
        # locally-mounted share read via LocalFilesystemProvider, so
        # normalize transparently as long as we have enough [smb] detail
        # to actually mount it; otherwise fail clearly instead of crashing
        # deep inside an unrelated command.
        if smb is None:
            raise ConfigurationError(
                f"{path}: source.provider = \"smb\" but no [smb] section is "
                "present, so there is nothing to mount. Run `romcloud configure` "
                "again to fix this."
            )
        provider = "local"

    source = SourceConfig(provider=provider, rom_root=rom_root)

    cache_raw = data.get("cache", {})
    cache = CacheConfig(
        path=cache_raw.get(
            "path", str(_DEFAULT_CACHE_ROOT)
        ),
        max_size_gb=float(cache_raw.get("max_size_gb", 50.0)),
        min_free_gb=float(cache_raw.get("min_free_gb", 5.0)),
    )

    local_roms_path = data.get("local_roms", {}).get(
        "path", str(_DEFAULT_LOCAL_ROMS)
    )

    data_path = data.get("data", {}).get(
        "path", str(_DEFAULT_ROMCLOUD_HOME / "data")
    )

    log_raw = data.get("logging", {})
    logging = LoggingConfig(
        level=log_raw.get("level", "INFO"),
        path=log_raw.get("path"),
    )

    return AppConfig(
        source=source,
        cache=cache,
        local_roms_path=local_roms_path,
        data_path=data_path,
        logging=logging,
        smb=smb,
    )


def write_config(config: AppConfig, config_path: Optional[str] = None) -> Path:
    """Serialize *config* to TOML and atomically write to disk.

    Never overwrites credentials — those are managed separately. Uses
    atomic file replacement (write-temp-then-rename) so a crash mid-write
    can never leave a corrupt/partial ``romcloud.toml`` behind — the
    existing file is left completely untouched until the new content is
    fully written.
    """
    path = Path(config_path) if config_path else default_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)

    lines = [
        "# ROMCloud configuration\n",
        "# Edit this file or run `romcloud configure` to change settings.\n",
        "\n",
        "[source]\n",
        f'provider = "{config.source.provider}"\n',
        f'rom_root = "{config.source.rom_root}"\n',
        "\n",
        "[cache]\n",
        f'path = "{config.cache.path}"\n',
        f"max_size_gb = {config.cache.max_size_gb}\n",
        f"min_free_gb = {config.cache.min_free_gb}\n",
        "\n",
        "[logging]\n",
        f'level = "{config.logging.level}"\n',
    ]

    if config.logging.path:
        lines.append(f'path = "{config.logging.path}"\n')

    lines += [
        "\n",
        "# ── Advanced settings ────────────────────────────────────────────────────────\n",
        "# Most users should not need to change the settings below.\n",
        "# They can be overridden for non-standard installations or development.\n",
        "\n",
        "[local_roms]\n",
        "# Directory where Batocera stores local ROM directories.\n",
        "# ROMCloud creates .romcloud proxy files here; never modifies existing ROMs.\n",
        f'path = "{config.local_roms_path}"\n',
        "\n",
        "[data]\n",
        f'path = "{config.data_path}"\n',
    ]

    if config.smb:
        lines += [
            "\n",
            "[smb]\n",
            f'server = "{config.smb.server}"\n',
            f'share = "{config.smb.share}"\n',
            f'username = "{config.smb.username}"\n',
            f"port = {config.smb.port}\n",
        ]

    atomic_write_text(path, "".join(lines))
    return path


def default_config_path() -> Path:
    return _DEFAULT_ROMCLOUD_HOME / "config" / "romcloud.toml"
