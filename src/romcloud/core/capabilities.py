"""Central capability policy for ROMCloud operating states.

The policy is deliberately pure: callers provide the configured game-access
mode and persisted presentation intent, then both backend guards and the GUI
consume the same decisions.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from romcloud.core.exceptions import CapabilityUnavailableError


class PresentationIntent(str, Enum):
    ONLINE = "online"
    OFFLINE = "offline"


class Capability(str, Enum):
    OFFLINE_MODE = "offline_mode"
    CACHED_LAUNCH = "cached_launch"
    CACHE_STATUS = "cache_status"
    CACHE_MANAGE = "cache_manage"
    GAME_DOWNLOAD = "game_download"
    CATALOG_REFRESH = "catalog_refresh"
    LIBRARY_SYNC = "library_sync"
    SAVE_SYNC = "save_sync"
    UPDATE_NETWORK = "update_network"
    REMOTE_VALIDATION = "remote_validation"
    LOCAL_SETTINGS = "local_settings"
    LOCAL_DIAGNOSTICS = "local_diagnostics"
    CONNECTION_RECOVERY = "connection_recovery"


_OFFLINE_BLOCKED = frozenset(
    {
        Capability.GAME_DOWNLOAD,
        Capability.CATALOG_REFRESH,
        Capability.LIBRARY_SYNC,
        Capability.SAVE_SYNC,
        Capability.UPDATE_NETWORK,
        Capability.REMOTE_VALIDATION,
    }
)


@dataclass(frozen=True)
class CapabilityDecision:
    allowed: bool
    reason: str | None = None


@dataclass(frozen=True)
class CapabilityPolicy:
    game_access_mode: str
    presentation_intent: PresentationIntent = PresentationIntent.ONLINE

    @property
    def offline_mode_supported(self) -> bool:
        return self.game_access_mode == "smart_cache"

    @property
    def offline(self) -> bool:
        return (
            self.offline_mode_supported
            and self.presentation_intent is PresentationIntent.OFFLINE
        )

    def decision(self, capability: Capability) -> CapabilityDecision:
        if capability is Capability.OFFLINE_MODE and not self.offline_mode_supported:
            return CapabilityDecision(
                False, "Offline Mode is available only in Smart Cache mode."
            )
        if self.offline and capability in _OFFLINE_BLOCKED:
            return CapabilityDecision(
                False,
                "Unavailable while Offline Mode is active. Switch to Online Mode first.",
            )
        return CapabilityDecision(True)

    def allows(self, capability: Capability) -> bool:
        return self.decision(capability).allowed

    def require(self, capability: Capability, operation: str) -> None:
        decision = self.decision(capability)
        if not decision.allowed:
            raise CapabilityUnavailableError(f"{operation}: {decision.reason}")

    def serialize(self) -> dict[str, object]:
        decisions = {cap.value: self.decision(cap) for cap in Capability}
        return {
            "game_access_mode": self.game_access_mode,
            "presentation_intent": (
                PresentationIntent.OFFLINE.value
                if self.offline
                else PresentationIntent.ONLINE.value
            ),
            "offline_mode": self.offline,
            "offline_mode_supported": self.offline_mode_supported,
            "capabilities": {
                key: decision.allowed for key, decision in decisions.items()
            },
            "blocked_reasons": {
                key: decision.reason
                for key, decision in decisions.items()
                if decision.reason is not None
            },
        }
