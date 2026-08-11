"""Configuration adapter for the central capability policy."""

from romcloud.core.capabilities import CapabilityPolicy, OperatingMode
from romcloud.infrastructure.config import AppConfig
from romcloud.infrastructure.library_view import operating_mode


def capability_policy(config: AppConfig) -> CapabilityPolicy:
    try:
        mode = operating_mode(config)
    except (AttributeError, TypeError):
        # Lightweight command/test contexts created before the persisted
        # operating-state feature have no data_path and therefore imply NAS.
        mode = OperatingMode.NAS
    return CapabilityPolicy(config.game_access_mode, mode)
