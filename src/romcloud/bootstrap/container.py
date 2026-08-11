"""Dependency injection container.

:class:`Container` wires all application dependencies together from a single
:class:`~romcloud.infrastructure.config.AppConfig`.

It uses lazy initialisation — objects are only created on first access.
Modules should request the object they need (e.g. ``container.catalog``) rather
than traversing nested relationships.

No global singletons.  Each CLI command constructs its own container from the
loaded config.  This keeps tests simple and avoids cross-request state.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

from romcloud.core.capabilities import CapabilityPolicy, OperatingMode
from romcloud.core.exceptions import ConfigurationError
from romcloud.core.models.cache import CachePolicy
from romcloud.core.save_ownership import ManagedSaveOwnershipPolicy
from romcloud.core.storage import StorageProvider
from romcloud.infrastructure.config import AppConfig, validate_remote_data_boundary
from romcloud.infrastructure.capabilities import capability_policy
from romcloud.infrastructure.database import Database
from romcloud.infrastructure.providers.local import (
    LocalFilesystemProvider,
    WritableLocalFilesystemProvider,
    WritableMountedFilesystemProvider,
)
from romcloud.integrations.batocera.catalog import CatalogService
from romcloud.infrastructure.repositories.cache import CacheRepository
from romcloud.infrastructure.repositories.game import GameRepository
from romcloud.infrastructure.repositories.proxy import ProxyRepository
from romcloud.services.cache import CacheService
from romcloud.services.saves import SaveSyncService
from romcloud.services.library_sync import LibrarySyncService
from romcloud.services.transfer import TransferService


def _local_game_stems(
    local_roms_root: Path, owned_proxy_paths: set[str]
) -> dict[str, frozenset[str]]:
    """Collect possible non-ROMCloud game names without following links.

    The catalog remains authoritative for managed identity. This filesystem
    evidence is only a conservative ambiguity guard: if an ordinary local ROM
    could produce the same save basename, automatic synchronization abstains.
    """
    if not local_roms_root.is_dir():
        return {}
    owned = {os.path.abspath(path) for path in owned_proxy_paths}
    result: dict[str, frozenset[str]] = {}
    for system_dir in sorted(local_roms_root.iterdir()):
        if not system_dir.is_dir() or system_dir.is_symlink():
            continue
        stems: set[str] = set()
        for current, directories, filenames in os.walk(system_dir, followlinks=False):
            current_path = Path(current)
            safe_directories = []
            for dirname in directories:
                candidate = current_path / dirname
                if candidate.is_symlink():
                    continue
                safe_directories.append(dirname)
                stems.add(dirname.casefold())
            directories[:] = safe_directories
            for filename in filenames:
                candidate = current_path / filename
                if candidate.is_symlink() or os.path.abspath(candidate) in owned:
                    continue
                stems.add(candidate.stem.casefold())
        result[system_dir.name] = frozenset(stems)
    return result


class Container:
    """Wires together all application dependencies."""

    def __init__(
        self,
        config: AppConfig,
        *,
        operating_policy: Optional[CapabilityPolicy] = None,
    ) -> None:
        self._config = config
        self._operating_policy = operating_policy
        self._db: Optional[Database] = None
        self._provider: Optional[StorageProvider] = None
        self._game_repo: Optional[GameRepository] = None
        self._cache_repo: Optional[CacheRepository] = None
        self._proxy_repo: Optional[ProxyRepository] = None
        self._transfer: Optional[TransferService] = None
        self._cache: Optional[CacheService] = None
        self._catalog: Optional[CatalogService] = None
        self._saves: Optional[SaveSyncService] = None
        self._library_sync: Optional[LibrarySyncService] = None

    def _policy(self) -> CapabilityPolicy:
        """Resolve mode policy only for services that actually consume it."""
        return self._operating_policy or capability_policy(self._config)

    @property
    def config(self) -> AppConfig:
        return self._config

    # ── infrastructure ────────────────────────────────────────────────────────

    @property
    def database(self) -> Database:
        if self._db is None:
            from pathlib import Path
            db_path = str(Path(self._config.data_path) / "catalog.db")
            self._db = Database(db_path)
            self._db.initialize()
        return self._db

    # ── repositories ─────────────────────────────────────────────────────────

    @property
    def game_repo(self) -> GameRepository:
        if self._game_repo is None:
            self._game_repo = GameRepository(self.database)
        return self._game_repo

    @property
    def cache_repo(self) -> CacheRepository:
        if self._cache_repo is None:
            self._cache_repo = CacheRepository(self.database)
        return self._cache_repo

    @property
    def proxy_repo(self) -> ProxyRepository:
        if self._proxy_repo is None:
            self._proxy_repo = ProxyRepository(self.database)
        return self._proxy_repo

    # ── provider ──────────────────────────────────────────────────────────────

    @property
    def provider(self) -> StorageProvider:
        if self._provider is None:
            provider_id = self._config.source.provider
            if provider_id == "local":
                self._provider = LocalFilesystemProvider()
            elif provider_id == "smb":
                self._provider = self._build_smb_provider()
            else:
                raise ConfigurationError(
                    f"Unknown storage provider: {provider_id!r}. "
                    "Valid options: local, smb"
                )
        return self._provider

    # ── services ──────────────────────────────────────────────────────────────

    @property
    def transfer(self) -> TransferService:
        if self._transfer is None:
            self._transfer = TransferService(
                provider=self.provider,
                cache_root=self._config.cache.path,
            )
        return self._transfer

    @property
    def cache(self) -> CacheService:
        if self._cache is None:
            policy = CachePolicy.from_gb(
                max_size_gb=self._config.cache.max_size_gb,
                min_free_gb=self._config.cache.min_free_gb,
            )
            self._cache = CacheService(
                cache_repo=self.cache_repo,
                game_repo=self.game_repo,
                transfer_service=self.transfer,
                cache_root=self._config.cache.path,
                policy=policy,
                capability_policy=self._policy(),
            )
        return self._cache

    @property
    def catalog(self) -> CatalogService:
        if self._catalog is None:
            from romcloud.infrastructure.library_view import operating_mode

            self._catalog = CatalogService(
                provider=self.provider,
                game_repo=self.game_repo,
                proxy_repo=self.proxy_repo,
                local_roms_root=self._config.local_roms_path,
                source_root=self._config.source.rom_root,
                write_proxies=operating_mode(self._config) is OperatingMode.CACHE,
                capability_policy=self._policy(),
            )
        return self._catalog

    @property
    def saves(self) -> SaveSyncService:
        if self._saves is None:
            remote_data = self._config.remote_data
            validate_remote_data_boundary(
                source=self._config.source,
                source_smb=self._config.smb,
                cache=self._config.cache,
                data_path=self._config.data_path,
                local_saves_path=self._config.saves.local_path,
                remote_data=remote_data,
                context="ROMCloud configuration",
            )
            remote_base = Path(remote_data.root) if remote_data is not None else None
            if remote_data is None:
                saves_provider = None
            elif remote_data.provider == "smb":
                if remote_data.smb is None:  # validated above; keeps type narrowing explicit
                    raise ConfigurationError("SMB remote data requires an SMB target")
                saves_provider = WritableMountedFilesystemProvider(
                    expected_server=remote_data.smb.server,
                    expected_share=remote_data.smb.share,
                )
            else:
                saves_provider = WritableLocalFilesystemProvider()
            data_path = Path(self._config.data_path)
            legacy_rpcs3_root = None
            local_saves_path = Path(self._config.saves.local_path)
            if (
                local_saves_path.name == "saves"
                and local_saves_path.parent.name == "userdata"
            ):
                legacy_rpcs3_root = (
                    local_saves_path.parent
                    / "system"
                    / "configs"
                    / "rpcs3"
                    / "dev_hdd0"
                )
            elif data_path.name == "data" and data_path.parent.name == "romcloud":
                legacy_rpcs3_root = (
                    data_path.parent.parent / "configs" / "rpcs3" / "dev_hdd0"
                )
            ownership = ManagedSaveOwnershipPolicy(
                self.game_repo.list_all(),
                ambiguous_local_stems=_local_game_stems(
                    Path(self._config.local_roms_path),
                    {record.proxy_path for record in self.proxy_repo.list_all()},
                ),
            )
            self._saves = SaveSyncService(
                provider=saves_provider,
                connectivity_root=str(remote_base) if remote_base is not None else None,
                local_root=self._config.saves.local_path,
                remote_root=str(remote_base / "saves") if remote_base is not None else None,
                state_path=Path(self._config.data_path) / "savesync-state.json",
                xbox_enabled=self._config.saves.xbox_enabled,
                rpcs3_installed_games_enabled=(
                    self._config.saves.rpcs3_installed_games_enabled
                ),
                include_local_games=self._config.saves.include_local_games,
                ownership_policy=ownership,
                legacy_rpcs3_root=(
                    str(legacy_rpcs3_root) if legacy_rpcs3_root is not None else None
                ),
                capability_policy=self._policy(),
            )
        return self._saves

    @property
    def library_sync(self) -> LibrarySyncService:
        if self._library_sync is None:
            from pathlib import Path

            remote_data = self._config.remote_data
            validate_remote_data_boundary(
                source=self._config.source,
                source_smb=self._config.smb,
                cache=self._config.cache,
                data_path=self._config.data_path,
                local_saves_path=self._config.saves.local_path,
                remote_data=remote_data,
                context="ROMCloud configuration",
            )
            remote_base = Path(remote_data.root) if remote_data is not None else None
            if remote_data is None:
                provider = None
            elif remote_data.provider == "smb":
                if remote_data.smb is None:
                    raise ConfigurationError("SMB remote data requires an SMB target")
                provider = WritableMountedFilesystemProvider(
                    expected_server=remote_data.smb.server,
                    expected_share=remote_data.smb.share,
                )
            else:
                provider = WritableLocalFilesystemProvider()
            self._library_sync = LibrarySyncService(
                enabled=self._config.library_sync.enabled,
                provider=provider,
                connectivity_root=str(remote_base) if remote_base is not None else None,
                source_root=self._config.source.rom_root,
                local_roms_root=self._config.local_roms_path,
                data_root=self._config.data_path,
                remote_root=str(remote_base / "library") if remote_base is not None else None,
                game_access_mode=(
                    "direct_nas"
                    if self._policy().effective_mode is OperatingMode.CONNECTED
                    else "smart_cache"
                ),
                game_repo=self.game_repo,
                proxy_repo=self.proxy_repo,
                capability_policy=self._policy(),
            )
        return self._library_sync

    # ── helpers ───────────────────────────────────────────────────────────────

    def _build_smb_provider(self) -> StorageProvider:
        from romcloud.infrastructure.credentials import load_smb_password
        from romcloud.infrastructure.providers.smb import SMBProvider

        smb_cfg = self._config.smb
        if smb_cfg is None:
            raise ConfigurationError(
                "SMB provider selected but [smb] section is missing in config."
            )

        password = load_smb_password(self._config.credentials_path) or ""
        return SMBProvider(
            server=smb_cfg.server,
            share=smb_cfg.share,
            username=smb_cfg.username,
            password=password,
            port=smb_cfg.port,
        )
