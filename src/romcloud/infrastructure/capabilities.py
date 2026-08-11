"""Configuration adapter for the central capability policy."""

from romcloud.core.capabilities import CapabilityPolicy, PresentationIntent
from romcloud.infrastructure.config import AppConfig
from romcloud.infrastructure.library_view import offline_library_enabled


def capability_policy(config: AppConfig) -> CapabilityPolicy:
    try:
        offline = offline_library_enabled(config)
    except (AttributeError, TypeError):
        # Lightweight command/test contexts created before the persisted
        # presentation feature have no data_path and therefore imply online.
        offline = False
    intent = PresentationIntent.OFFLINE if offline else PresentationIntent.ONLINE
    return CapabilityPolicy(config.game_access_mode, intent)
