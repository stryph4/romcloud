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

from pathlib import Path, PurePosixPath
from typing import Optional

from romcloud.core.capabilities import CapabilityPolicy, OperatingMode
from romcloud.core.exceptions import ConfigurationError
from romcloud.core.models.cache import CachePolicy
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
from romcloud.infrastructure.remote_saves import build_remote_save_store
from romcloud.services.library_sync import LibrarySyncService
from romcloud.services.transfer import TransferService

_NETWORK_STORAGE_PROBE_TIMEOUT = 5.0


def _remote_data_base_path(remote_data) -> object:  # noqa: ANN001
    """``remote_data.root`` for SFTP is a remote POSIX path on another host,
    never a local one — join it with :class:`PurePosixPath` regardless of
    ROMCloud's own OS. Local/mounted-SMB roots remain real local
    :class:`~pathlib.Path` objects, unchanged."""
    if remote_data is None:
        return None
    if remote_data.provider == "sftp":
        return PurePosixPath(remote_data.root)
    return Path(remote_data.root)


def _remote_data_base_path(remote_data) -> "Path | PurePosixPath | None":  # noqa: ANN001
    """``remote_data.root`` for SFTP is a remote POSIX path on another host,
    never a local one — join it with :class:`PurePosixPath` regardless of
    ROMCloud's own OS. Local/mounted-SMB roots remain real local
    :class:`~pathlib.Path` objects, unchanged."""
    if remote_data is None:
        return None
    if remote_data.provider == "sftp":
        return PurePosixPath(remote_data.root)
    return Path(remote_data.root)


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
        self._library_manager = None

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
            CacheRepository(self._db).reconcile_legacy_cache_paths(
                self._config.cache.path
            )
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

    @property
    def system_registry(self):  # noqa: ANN201
        """Current effective Batocera launch registry with LKG fallback."""
        from romcloud.integrations.batocera.system_registry import (
            REGISTRY_CACHE_FILENAME,
            load_effective_system_registry,
        )

        # Deliberately reload on each operation: a long-lived GUI/container
        # must see user edits and recover from a previously malformed config
        # without requiring a ROMCloud restart.
        return load_effective_system_registry(
            cache_path=Path(self._config.data_path) / REGISTRY_CACHE_FILENAME
        )

    # ── provider ──────────────────────────────────────────────────────────────

    @property
    def provider(self) -> StorageProvider:
        if self._provider is None:
            if not self._config.source.enabled:
                raise ConfigurationError(
                    "ROMCloud game management is disabled; no ROM source provider is configured."
                )
            provider_id = self._config.source.provider
            if provider_id == "local":
                self._provider = LocalFilesystemProvider(
                    probe_timeout=(
                        _NETWORK_STORAGE_PROBE_TIMEOUT
                        if self._config.smb is not None
                        else None
                    )
                )
            elif provider_id == "smb":
                self._provider = self._build_smb_provider()
            elif provider_id == "sftp":
                self._provider = self._build_sftp_provider()
            else:
                raise ConfigurationError(
                    f"Unknown storage provider: {provider_id!r}. "
                    "Valid options: local, smb, sftp"
                )
        return self._provider

    # ── services ──────────────────────────────────────────────────────────────

    @property
    def transfer(self) -> TransferService:
        if self._transfer is None:
            self._transfer = TransferService(
                provider=self.provider,
                cache_root=self._config.cache.path,
                source_root=self._config.source.rom_root,
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
                registry_loader=lambda: self.system_registry,
                selected_systems=self._config.source.selected_systems,
                write_proxies=operating_mode(self._config) is OperatingMode.CACHE,
                capability_policy=self._policy(),
                cache_repo=self.cache_repo,
            )
        return self._catalog

    @property
    def saves(self) -> SaveSyncService:
        if self._saves is None:
            remote_data = self._config.remote_data
            validate_remote_data_boundary(
                source=self._config.source,
                source_smb=self._config.smb,
                source_sftp=self._config.sftp,
                cache=self._config.cache,
                data_path=self._config.data_path,
                local_saves_path=self._config.saves.local_path,
                remote_data=remote_data,
                context="ROMCloud configuration",
            )
            saves_provider = self._build_remote_data_provider()
            connectivity_root = remote_data.root if remote_data is not None else None
            dataset_root = (
                saves_provider.remote_data_root(connectivity_root, "saves")
                if saves_provider is not None and connectivity_root is not None
                else None
            )
            remote_store = build_remote_save_store(
                saves_provider,
                connectivity_root=connectivity_root,
                dataset_root=dataset_root,
            )
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
            self._saves = SaveSyncService(
                provider=saves_provider,
                connectivity_root=(
                    str(connectivity_root) if connectivity_root is not None else None
                ),
                local_root=self._config.saves.local_path,
                remote_root=None,
                state_path=Path(self._config.data_path) / "savesync-state.json",
                xbox_enabled=self._config.saves.xbox_enabled,
                rpcs3_installed_games_enabled=(
                    self._config.saves.rpcs3_installed_games_enabled
                ),
                legacy_rpcs3_root=(
                    str(legacy_rpcs3_root) if legacy_rpcs3_root is not None else None
                ),
                capability_policy=self._policy(),
                remote_store=remote_store,
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
                source_sftp=self._config.sftp,
                cache=self._config.cache,
                data_path=self._config.data_path,
                local_saves_path=self._config.saves.local_path,
                remote_data=remote_data,
                context="ROMCloud configuration",
            )
            remote_base = _remote_data_base_path(remote_data)
            if remote_data is None:
                provider = None
            elif remote_data.provider == "smb":
                if remote_data.smb is None:
                    raise ConfigurationError("SMB remote data requires an SMB target")
                provider = WritableMountedFilesystemProvider(
                    expected_server=remote_data.smb.server,
                    expected_share=remote_data.smb.share,
                    probe_timeout=_NETWORK_STORAGE_PROBE_TIMEOUT,
                )
            elif remote_data.provider == "sftp":
                provider = self._build_writable_sftp_provider(remote_data.sftp)
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

    @property
    def library_manager(self):  # noqa: ANN201
        if self._library_manager is None:
            from romcloud.infrastructure.capabilities import capability_policy
            from romcloud.infrastructure.repositories.library_browser import (
                LibraryBrowserRepository,
            )
            from romcloud.services.library_manager import LibraryManagerService

            self._library_manager = LibraryManagerService(
                browser_repo=LibraryBrowserRepository(self.database),
                game_repo=self.game_repo,
                cache_repo=self.cache_repo,
                cache=self.cache,
                policy_loader=lambda: capability_policy(self._config),
                source_reachable=lambda: self.provider.is_reachable(
                    self._config.source.rom_root
                ),
            )
        return self._library_manager

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

    def _build_sftp_provider(self) -> StorageProvider:
        from romcloud.infrastructure.credentials import load_sftp_password
        from romcloud.infrastructure.providers.sftp import SFTPProvider

        sftp_cfg = self._config.sftp
        if sftp_cfg is None:
            raise ConfigurationError(
                "SFTP provider selected but [sftp] section is missing in config."
            )
        password = load_sftp_password(self._config.credentials_path)
        return SFTPProvider(
            host=sftp_cfg.host,
            username=sftp_cfg.username,
            port=sftp_cfg.port,
            password=password,
            private_key_path=sftp_cfg.private_key_path or None,
            trusted_host_key_fingerprint=sftp_cfg.host_key_fingerprint or None,
            probe_writable=False,
        )

    def _build_writable_sftp_provider(self, sftp_cfg) -> StorageProvider:  # noqa: ANN001
        """Independent remote-data SFTP instance: its own host/credentials,
        never coupled to the source SFTP target. Write-dependent
        SaveSync/Library Sync operations remain gated by
        ``capabilities.supports_durable_transactions`` (False for SFTP) at
        the service layer, not here — read-only consumption and write
        *validation* both still need a real, independently-probed instance.
        """
        from romcloud.infrastructure.credentials import load_remote_data_sftp_password
        from romcloud.infrastructure.providers.sftp import SFTPProvider

        if sftp_cfg is None:
            raise ConfigurationError("SFTP remote data requires an [remote_data.sftp] target")
        password = load_remote_data_sftp_password(self._config.credentials_path)
        return SFTPProvider(
            host=sftp_cfg.host,
            username=sftp_cfg.username,
            port=sftp_cfg.port,
            password=password,
            private_key_path=sftp_cfg.private_key_path or None,
            trusted_host_key_fingerprint=sftp_cfg.host_key_fingerprint or None,
            probe_writable=True,
        )

    def _build_remote_data_provider(self) -> Optional[StorageProvider]:
        """Build the independently configured ROMCloud-owned data provider."""
        remote_data = self._config.remote_data
        if remote_data is None:
            return None
        if remote_data.provider == "smb":
            if remote_data.smb is None:
                raise ConfigurationError("SMB remote data requires an SMB target")
            return WritableMountedFilesystemProvider(
                expected_server=remote_data.smb.server,
                expected_share=remote_data.smb.share,
                probe_timeout=_NETWORK_STORAGE_PROBE_TIMEOUT,
            )
        if remote_data.provider == "sftp":
            return self._build_writable_sftp_provider(remote_data.sftp)
        if remote_data.provider == "local":
            return WritableLocalFilesystemProvider()
        raise ConfigurationError(
            f"Unknown remote-data provider: {remote_data.provider!r}"
        )
