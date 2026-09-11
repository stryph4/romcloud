"""Configuration adapter for the central capability policy."""

from romcloud.core.capabilities import Capability, CapabilityPolicy, OperatingMode
from romcloud.infrastructure.config import AppConfig
from romcloud.infrastructure.library_view import operating_mode


_SFTP_SAVE_SYNC_REASON = (
    "SaveSync is unavailable with read-only SFTP data storage. "
    "Use SMB or local/external ROMCloud data storage for SaveSync."
)


def capability_policy(config: AppConfig) -> CapabilityPolicy:
    try:
        mode = operating_mode(config)
    except (AttributeError, TypeError):
        # Lightweight command/test contexts created before the persisted
        # operating-state feature have no data_path and therefore use the
        # configured strategy as their compatibility default.
        mode = (
            OperatingMode.CONNECTED
            if getattr(config, "game_access_mode", "smart_cache") == "direct_nas"
            else OperatingMode.CACHE
        )

    remote_data = getattr(config, "remote_data", None)
    sftp_remote_data = (
        remote_data is not None
        and getattr(remote_data, "provider", None) == "sftp"
    )
    blocked = (
        frozenset({Capability.SAVE_SYNC})
        if sftp_remote_data
        else frozenset()
    )
    return CapabilityPolicy(
        config.game_access_mode,
        mode,
        blocked_capabilities=blocked,
        blocked_reason=_SFTP_SAVE_SYNC_REASON if sftp_remote_data else None,
    )
