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

from typing import Optional

from romcloud.core.models.cache import CachePolicy
from romcloud.core.storage import StorageProvider
from romcloud.infrastructure.providers.local import LocalFilesystemProvider
from romcloud.integrations.batocera.catalog import CatalogService
from romcloud.infrastructure.config import AppConfig
from romcloud.infrastructure.database import Database
from romcloud.infrastructure.repositories.cache import CacheRepository
from romcloud.infrastructure.repositories.game import GameRepository
from romcloud.infrastructure.repositories.proxy import ProxyRepository
from romcloud.core.exceptions import ConfigurationError
from romcloud.services.cache import CacheService
from romcloud.services.saves import SaveSyncService
from romcloud.services.transfer import TransferService


class Container:
    """Wires together all application dependencies."""

    def __init__(self, config: AppConfig) -> None:
        self._config = config
        self._db: Optional[Database] = None
        self._provider: Optional[StorageProvider] = None
        self._game_repo: Optional[GameRepository] = None
        self._cache_repo: Optional[CacheRepository] = None
        self._proxy_repo: Optional[ProxyRepository] = None
        self._transfer: Optional[TransferService] = None
        self._cache: Optional[CacheService] = None
        self._catalog: Optional[CatalogService] = None
        self._saves: Optional[SaveSyncService] = None

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
            )
        return self._cache

    @property
    def catalog(self) -> CatalogService:
        if self._catalog is None:
            self._catalog = CatalogService(
                provider=self.provider,
                game_repo=self.game_repo,
                proxy_repo=self.proxy_repo,
                local_roms_root=self._config.local_roms_path,
                source_root=self._config.source.rom_root,
            )
        return self._catalog

    @property
    def saves(self) -> SaveSyncService:
        if self._saves is None:
            from pathlib import Path

            self._saves = SaveSyncService(
                provider=self.provider,
                connectivity_root=self._config.source.rom_root,
                local_root=self._config.saves.local_path,
                remote_root=str(Path(self._config.source.rom_root) / self._config.saves.remote_subdir),
                state_path=Path(self._config.data_path) / "savesync-state.json",
                xbox_enabled=self._config.saves.xbox_enabled,
            )
        return self._saves

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
