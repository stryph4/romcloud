"""Core domain exceptions.

All ROMCloud exceptions inherit from ROMCloudError so callers can catch broadly
or specifically as needed. No dependency on Batocera or infrastructure here.
"""


class ROMCloudError(Exception):
    """Base class for all ROMCloud errors."""


class CapabilityUnavailableError(ROMCloudError):
    """The current operating state intentionally disables a capability."""


class ModeTransitionError(ROMCloudError):
    """A requested operating-mode transition failed without being committed."""


# ── Configuration ─────────────────────────────────────────────────────────────


class ConfigurationError(ROMCloudError):
    """Configuration is missing or invalid."""


class ConfigurationNotFoundError(ConfigurationError):
    """No configuration file exists yet. Run `romcloud configure`."""


# ── Provider / source ─────────────────────────────────────────────────────────


class ProviderError(ROMCloudError):
    """A storage provider operation failed."""


class ProviderNotReachableError(ProviderError):
    """The storage source is currently unreachable."""


class ProviderAuthError(ProviderError):
    """Authentication with the storage source failed."""


class ProviderPermissionError(ProviderError):
    """The storage source was reached and authenticated, but the requested
    operation was denied (e.g. a read-only account/path)."""


class ProviderHostKeyUnknownError(ProviderError):
    """No trusted host key is on file yet for this server.

    Carries ``fingerprint``/``key_type`` so a setup flow can present them for
    explicit first-connection trust instead of connecting blindly.
    """

    def __init__(self, message: str, *, fingerprint: str, key_type: str) -> None:
        super().__init__(message)
        self.fingerprint = fingerprint
        self.key_type = key_type


class ProviderHostKeyMismatchError(ProviderError):
    """The server's host key does not match the previously trusted key.

    Never bypassed automatically — this always indicates either a
    reconfigured server or a potential MITM and requires explicit user
    action to re-trust.
    """

    def __init__(self, message: str, *, fingerprint: str, key_type: str) -> None:
        super().__init__(message)
        self.fingerprint = fingerprint
        self.key_type = key_type


class MountError(ProviderError):
    """Mounting or unmounting a source filesystem failed."""


# ── Catalog ─────────────────────────────────────────────────────────────────


class CatalogError(ROMCloudError):
    """Catalog operation failed."""


class GameNotFoundError(CatalogError):
    """Game not found in catalog."""


class ProxyError(CatalogError):
    """Proxy file operation failed."""


class ProxyNotOwnedError(ProxyError):
    """Refusing to touch a proxy file that ROMCloud did not create."""


# ── Cache ─────────────────────────────────────────────────────────────────────


class CacheError(ROMCloudError):
    """Cache operation failed."""


class InsufficientSpaceError(CacheError):
    """Not enough free space to cache the game."""


class GamePinnedError(CacheError):
    """Cannot remove a pinned game without unpinning first."""


class DependencyResolutionError(CacheError):
    """A game descriptor has an unsafe, invalid, or missing dependency."""


# ── Transfer ──────────────────────────────────────────────────────────────────


class TransferError(ROMCloudError):
    """A file transfer failed."""


class TransferInterruptedError(TransferError):
    """Transfer was interrupted; partial data remains for resume."""


class TransferValidationError(TransferError):
    """Transferred data failed validation (size mismatch, etc.)."""


# ── Launch ────────────────────────────────────────────────────────────────────


class LaunchError(ROMCloudError):
    """Failed to launch a game."""


class GameNotCachedError(LaunchError):
    """Game is not cached and the source is unreachable — cannot launch."""


# ── Update ────────────────────────────────────────────────────────────────────


class UpdateError(ROMCloudError):
    """Self-update failed."""


class UpdateDownloadError(UpdateError):
    """Downloading the update archive, or querying GitHub for commit info, failed."""


class UpdateArchiveError(UpdateError):
    """The downloaded archive is malformed, or unsafe to extract."""


class UpdateInstallError(UpdateError):
    """Installing the updated package into the virtual environment failed."""


# ── Save Sync ─────────────────────────────────────────────────────────────────


class SaveSyncError(ROMCloudError):
    """A save-sync operation failed."""


class SaveSyncConnectivityError(SaveSyncError):
    """The remote save location is not reachable."""


class SaveSyncVerificationError(SaveSyncError):
    """Staged save data failed verification after copying — the source
    changed since the preview, or the copy was corrupted."""


class SaveSyncWriteUnavailableError(SaveSyncError):
    """A specific operation needs to write to remote-data, but the
    configured remote-data provider instance does not support ROMCloud's
    durable write-transaction requirements (see
    ``ProviderCapabilities.supports_durable_transactions``). Read-only
    consumption of the same remote-data target remains available and is
    not affected by this error."""


# ── Library Sync ────────────────────────────────────────────────────────────────────────────


class LibrarySyncError(ROMCloudError):
    """Library metadata synchronization failed safely."""


class LibrarySyncConnectivityError(LibrarySyncError):
    """The configured canonical library store is unavailable."""


class LibrarySyncWriteUnavailableError(LibrarySyncError):
    """A specific operation needs to publish to remote-data, but the
    configured remote-data provider instance does not support ROMCloud's
    durable write-transaction requirements (see
    ``ProviderCapabilities.supports_durable_transactions``). Reading
    existing remote metadata/media remains available and is not affected
    by this error."""
