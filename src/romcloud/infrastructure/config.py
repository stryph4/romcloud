"""Application configuration.

User-facing configuration lives in a TOML file.  Credentials are stored in a
*separate* file with restricted filesystem permissions and are never loaded
into the main :class:`AppConfig` object.

Default installation path: ``/userdata/system/romcloud/config/romcloud.toml``
"""

from __future__ import annotations

import re
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
from romcloud.infrastructure.credentials import (
    migrate_legacy_smb_credentials,
    migrate_plaintext_credentials,
)

# ── defaults ──────────────────────────────────────────────────────────────────

_DEFAULT_ROMCLOUD_HOME = Path("/userdata/system/romcloud")
_DEFAULT_RUNTIME_ROOT = Path("/userdata/romcloud")
_DEFAULT_REMOTE_DATA_ROOT = _DEFAULT_RUNTIME_ROOT / "remote"
_DEFAULT_CACHE_ROOT = _DEFAULT_RUNTIME_ROOT / "cache"
_DEFAULT_LOCAL_ROMS = Path("/userdata/roms")
_DEFAULT_SAVES_LOCAL_PATH = Path("/userdata/saves")

_LEGACY_SOURCE_ROOT = "/userdata/romcloud-source"
_LEGACY_CACHE_ROOT = "/userdata/romcloud-cache"
_LEGACY_SAVES_KEYS = frozenset({"remote_mount_path", "remote_subdir"})

SMART_CACHE_MODE = "smart_cache"
DIRECT_NAS_MODE = "direct_nas"
GAME_ACCESS_MODES = frozenset({SMART_CACHE_MODE, DIRECT_NAS_MODE})


def _validated_smb_remote_path(value: object, context: object = "configuration") -> str:
    """Validate the persisted share-relative mount path without I/O."""
    raw = str(value or "").replace("\\", "/").strip("/")
    if not raw:
        return ""
    parts = raw.split("/")
    if (
        any(part in ("", ".", "..") for part in parts)
        or any(char in raw for char in ('"', "\n", "\r", "\x00", ","))
    ):
        raise ConfigurationError(
            f"{context}: SMB remote_path must be a safe path inside the selected share."
        )
    return "/".join(parts)


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
    remote_path: str = ""
    """Optional share-relative directory mounted as the storage root."""


@dataclass(frozen=True)
class RemoteDataConfig:
    """General writable storage owned by ROMCloud synchronized features.

    ``root`` is the filesystem path ROMCloud uses. For a local/USB target it
    is the user-selected directory. For SMB it is the operational mount point
    (normally ``/userdata/romcloud/remote``), while ``smb`` identifies the
    independently selected network target.
    """

    provider: str
    root: str
    smb: Optional[SMBConfig] = None


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
class SavesConfig:
    """Local save/state selection settings.

    The remote dataset location is intentionally not configurable here. It
    is always ``<remote_data.root>/saves`` when general remote data storage
    is configured.
    """

    local_path: str = str(_DEFAULT_SAVES_LOCAL_PATH)
    xbox_enabled: bool = False
    rpcs3_installed_games_enabled: bool = False
    include_local_games: bool = False


@dataclass(frozen=True)
class LibrarySyncConfig:
    """Opt-in synchronized EmulationStation metadata settings."""

    enabled: bool = False


@dataclass(frozen=True)
class AppConfig:
    source: SourceConfig
    cache: CacheConfig
    local_roms_path: str
    data_path: str
    logging: LoggingConfig = field(default_factory=LoggingConfig)
    smb: Optional[SMBConfig] = None
    remote_data: Optional[RemoteDataConfig] = None
    saves: SavesConfig = field(default_factory=SavesConfig)
    library_sync: LibrarySyncConfig = field(default_factory=LibrarySyncConfig)
    game_access_mode: str = SMART_CACHE_MODE

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
        migrate_legacy_storage_config(path)
    except OSError as exc:
        raise ConfigurationError(
            f"Failed to migrate legacy config {path}: {exc}"
        ) from exc

    try:
        with path.open("rb") as fh:
            data = tomllib.load(fh)
    except Exception as exc:
        raise ConfigurationError(f"Failed to parse config {path}: {exc}") from exc

    config = _parse(data, path)
    migrate_legacy_smb_credentials(config.credentials_path)
    migrate_plaintext_credentials(config.credentials_path)
    return config


def migrate_legacy_storage_config(
    path: Path,
    *,
    legacy_source_root: str = _LEGACY_SOURCE_ROOT,
    source_root: str = str(_DEFAULT_RUNTIME_ROOT / "source"),
    legacy_cache_root: str = _LEGACY_CACHE_ROOT,
    cache_root: str = str(_DEFAULT_CACHE_ROOT),
) -> bool:
    """Atomically reconcile the pre-0.9.2 storage schema in *path*.

    Only the two exact historical ROMCloud defaults are replaced. Legacy
    SaveSync destination keys are removed because they cannot safely identify
    the independently selected writable target required by ``[remote_data]``.
    The rest of the file is patched in place rather than serialized from
    :class:`AppConfig`, preserving comments, unknown sections, and unrelated
    user settings.
    """
    raw = path.read_text(encoding="utf-8")
    try:
        data = tomllib.loads(raw)
    except Exception:  # The normal load path will report the parse error.
        return False

    source_raw = data.get("source", {})
    cache_raw = data.get("cache", {})
    saves_raw = data.get("saves", {})
    migrate_source = isinstance(source_raw, dict) and (
        source_raw.get("rom_root") == legacy_source_root
    )
    migrate_cache = isinstance(cache_raw, dict) and (
        cache_raw.get("path") == legacy_cache_root
    )
    remove_saves_keys = (
        _LEGACY_SAVES_KEYS.intersection(saves_raw)
        if isinstance(saves_raw, dict)
        else frozenset()
    )
    if not (migrate_source or migrate_cache or remove_saves_keys):
        return False

    section = ""
    rewritten: list[str] = []
    for line in raw.splitlines(keepends=True):
        section_match = re.match(r"^\s*\[([^\[\]]+)\]\s*(?:#.*)?(?:\r?\n)?$", line)
        if section_match:
            section = section_match.group(1).strip()
            rewritten.append(line)
            continue

        key_match = re.match(r"^\s*([A-Za-z0-9_-]+)\s*=", line)
        key = key_match.group(1) if key_match else None
        if section == "saves" and key in remove_saves_keys:
            continue
        if section == "source" and key == "rom_root" and migrate_source:
            line = _replace_exact_toml_string(
                line, "rom_root", legacy_source_root, source_root
            )
        elif section == "cache" and key == "path" and migrate_cache:
            line = _replace_exact_toml_string(
                line, "path", legacy_cache_root, cache_root
            )
        rewritten.append(line)

    result = "".join(rewritten)
    if result == raw:
        return False
    atomic_write_text(path, result, mode=path.stat().st_mode & 0o777)
    return True


def _replace_exact_toml_string(line: str, key: str, old: str, new: str) -> str:
    """Replace a simple historical TOML string while retaining its formatting."""
    pattern = re.compile(
        rf"^(\s*{re.escape(key)}\s*=\s*)(['\"]){re.escape(old)}\2(\s*(?:#.*)?)(\r?\n)?$"
    )
    match = pattern.match(line)
    if match is None:  # Valid but non-simple TOML remains untouched conservatively.
        return line
    newline = match.group(4) or ""
    return f'{match.group(1)}"{new}"{match.group(3)}{newline}'


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
                remote_path=_validated_smb_remote_path(s.get("remote_path", ""), path),
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

    remote_data = None
    remote_raw = data.get("remote_data")
    if remote_raw is not None:
        provider_id = str(remote_raw.get("provider", "local")).lower()
        if provider_id not in {"local", "smb"}:
            raise ConfigurationError(
                f"{path}: remote_data.provider must be \"local\" or \"smb\"."
            )
        default_root = (
            str(_DEFAULT_REMOTE_DATA_ROOT) if provider_id == "smb" else ""
        )
        remote_root = str(remote_raw.get("root", default_root)).strip()
        if not remote_root or not Path(remote_root).is_absolute():
            raise ConfigurationError(
                f"{path}: remote_data.root must be an explicit absolute path."
            )

        remote_smb = None
        if provider_id == "smb":
            remote_smb_raw = remote_raw.get("smb")
            if not isinstance(remote_smb_raw, dict):
                raise ConfigurationError(
                    f"{path}: remote_data.provider = \"smb\" requires [remote_data.smb]."
                )
            try:
                remote_smb = SMBConfig(
                    server=remote_smb_raw["server"],
                    share=remote_smb_raw["share"],
                    username=remote_smb_raw.get("username", "guest"),
                    port=int(remote_smb_raw.get("port", 445)),
                    remote_path=_validated_smb_remote_path(
                        remote_smb_raw.get("remote_path", ""), path
                    ),
                )
            except KeyError as exc:
                raise ConfigurationError(
                    f"Missing required [remote_data.smb] key: {exc}"
                ) from exc

        remote_data = RemoteDataConfig(
            provider=provider_id,
            root=remote_root,
            smb=remote_smb,
        )

    saves_raw = data.get("saves", {})
    saves = SavesConfig(
        local_path=saves_raw.get("local_path", str(_DEFAULT_SAVES_LOCAL_PATH)),
        xbox_enabled=bool(saves_raw.get("xbox_enabled", False)),
        rpcs3_installed_games_enabled=bool(
            saves_raw.get("rpcs3_installed_games_enabled", False)
        ),
        include_local_games=bool(saves_raw.get("include_local_games", False)),
    )

    library_sync_raw = data.get("library_sync", {})
    library_sync = LibrarySyncConfig(
        enabled=bool(library_sync_raw.get("enabled", False)),
    )
    if library_sync.enabled and remote_data is None:
        raise ConfigurationError(
            f"{path}: Library Sync requires configured writable [remote_data]."
        )

    game_access_mode = str(
        data.get("game_access", {}).get("mode", SMART_CACHE_MODE)
    ).strip().lower()
    if game_access_mode not in GAME_ACCESS_MODES:
        raise ConfigurationError(
            f"{path}: game_access.mode must be \"smart_cache\" or \"direct_nas\"."
        )
    if game_access_mode == DIRECT_NAS_MODE and paths_overlap(
        Path(source.rom_root), Path(local_roms_path)
    ):
        raise ConfigurationError(
            f"{path}: Connected Mode source.rom_root must not overlap local_roms.path."
        )

    validate_remote_data_boundary(
        source=source,
        source_smb=smb,
        cache=cache,
        data_path=data_path,
        local_saves_path=saves.local_path,
        remote_data=remote_data,
        context=str(path),
    )

    return AppConfig(
        source=source,
        cache=cache,
        local_roms_path=local_roms_path,
        data_path=data_path,
        logging=logging,
        smb=smb,
        remote_data=remote_data,
        saves=saves,
        library_sync=library_sync,
        game_access_mode=game_access_mode,
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
    if config.game_access_mode not in GAME_ACCESS_MODES:
        raise ConfigurationError(
            f"{path}: game_access_mode must be smart_cache or direct_nas."
        )
    if config.game_access_mode == DIRECT_NAS_MODE and paths_overlap(
        Path(config.source.rom_root), Path(config.local_roms_path)
    ):
        raise ConfigurationError(
            f"{path}: Connected Mode source.rom_root must not overlap local_roms.path."
        )
    if config.library_sync.enabled and config.remote_data is None:
        raise ConfigurationError(
            f"{path}: Library Sync requires configured writable [remote_data]."
        )
    validate_remote_data_boundary(
        source=config.source,
        source_smb=config.smb,
        cache=config.cache,
        data_path=config.data_path,
        local_saves_path=config.saves.local_path,
        remote_data=config.remote_data,
        context=str(path),
    )
    path.parent.mkdir(parents=True, exist_ok=True)

    lines = [
        "# ROMCloud configuration\n",
        "# Edit this file or run `romcloud configure` to change settings.\n",
        "\n",
        "[source]\n",
        f'provider = "{config.source.provider}"\n',
        f'rom_root = "{config.source.rom_root}"\n',
        "\n",
        "[game_access]\n",
        "# smart_cache downloads games on demand; direct_nas plays from the source.\n",
        f'mode = "{config.game_access_mode}"\n',
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
        "# Cache Mode creates proxies; Connected Mode creates verified system symlinks.\n",
        "# ROMCloud never modifies existing user ROMs or owns system directories.\n",
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
        if config.smb.remote_path:
            lines.append(f'remote_path = "{config.smb.remote_path}"\n')

    if config.remote_data is not None:
        lines += [
            "\n",
            "[remote_data]\n",
            "# General writable storage for synchronized ROMCloud data.\n",
            f'provider = "{config.remote_data.provider}"\n',
            f'root = "{config.remote_data.root}"\n',
        ]
        if config.remote_data.smb is not None:
            lines += [
                "\n",
                "[remote_data.smb]\n",
                f'server = "{config.remote_data.smb.server}"\n',
                f'share = "{config.remote_data.smb.share}"\n',
                f'username = "{config.remote_data.smb.username}"\n',
                f"port = {config.remote_data.smb.port}\n",
            ]
            if config.remote_data.smb.remote_path:
                lines.append(
                    f'remote_path = "{config.remote_data.smb.remote_path}"\n'
                )

    lines += [
        "\n",
        "[saves]\n",
        "# Shared save/state continuity — see `romcloud saves --help`.\n",
        f'local_path = "{config.saves.local_path}"\n',
        f"xbox_enabled = {'true' if config.saves.xbox_enabled else 'false'}\n",
        "# Compatibility keys: RPCS3 applications remain ineligible and eligible "
        "local-game saves remain included regardless of these values.\n",
        "rpcs3_installed_games_enabled = "
        f"{'true' if config.saves.rpcs3_installed_games_enabled else 'false'}\n",
        f"include_local_games = {'true' if config.saves.include_local_games else 'false'}\n",
        "\n",
        "[library_sync]\n",
        "# Opt-in metadata/media sync. Source gamelist.xml files are read-only.\n",
        f"enabled = {'true' if config.library_sync.enabled else 'false'}\n",
    ]

    atomic_write_text(path, "".join(lines))
    return path


def default_config_path() -> Path:
    return _DEFAULT_ROMCLOUD_HOME / "config" / "romcloud.toml"


def validate_remote_data_boundary(
    *,
    source: SourceConfig,
    source_smb: Optional[SMBConfig],
    cache: CacheConfig,
    data_path: str,
    local_saves_path: str,
    remote_data: Optional[RemoteDataConfig],
    context: str,
) -> None:
    """Keep user-controlled synchronized data outside ROM/runtime state.

    Rejecting both parent and child relationships prevents SaveSync from
    ever treating the read-only ROM tree, local cache, or persistent system
    state as its writable ownership boundary.
    """
    if remote_data is None:
        return
    if (
        source_smb is not None
        and remote_data.provider == "smb"
        and remote_data.smb is not None
        and source_smb.server.casefold() == remote_data.smb.server.casefold()
        and source_smb.share.strip("/").casefold()
        == remote_data.smb.share.strip("/").casefold()
    ):
        raise ConfigurationError(
            f"{context}: remote_data.smb must not reuse the ROM source SMB target; "
            "select a separate writable share."
        )
    remote_root = Path(remote_data.root)
    if not remote_root.is_absolute():
        raise ConfigurationError(
            f"{context}: remote_data.root must be an explicit absolute path."
        )
    for label, other in (
        ("source.rom_root", Path(source.rom_root)),
        ("cache.path", Path(cache.path)),
        ("data.path", Path(data_path)),
        ("ROMCloud system home", _DEFAULT_ROMCLOUD_HOME),
        ("saves.local_path", Path(local_saves_path)),
    ):
        if paths_overlap(remote_root, other):
            raise ConfigurationError(
                f"{context}: remote_data.root must not overlap {label}."
            )


def paths_overlap(first: Path, second: Path) -> bool:
    try:
        first = first.resolve(strict=False)
        second = second.resolve(strict=False)
    except OSError:
        pass
    return first == second or first in second.parents or second in first.parents
