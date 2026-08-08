"""Core domain exceptions.

All ROMCloud exceptions inherit from ROMCloudError so callers can catch broadly
or specifically as needed. No dependency on Batocera or infrastructure here.
"""


class ROMCloudError(Exception):
    """Base class for all ROMCloud errors."""


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


# ── Catalog ───────────────────────────────────────────────────────────────────


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
