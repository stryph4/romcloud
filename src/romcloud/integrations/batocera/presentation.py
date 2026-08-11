"""One authoritative refresh path for ROMCloud's ES presentation."""

from __future__ import annotations

from collections.abc import Iterable

from romcloud.infrastructure.config import AppConfig, DIRECT_NAS_MODE
from romcloud.integrations.batocera import es_config
from romcloud.integrations.batocera.es_systems import GeneratedOverride


def refresh_emulationstation(
    config: AppConfig, managed_systems: Iterable[str]
) -> GeneratedOverride | None:
    """Reconcile ROMCloud's owned ES registration after local state commits."""
    if config.game_access_mode == DIRECT_NAS_MODE:
        es_config.remove()
        return None
    # install() and refresh() are intentionally identical; the former keeps
    # this shared path compatible with setup's first-write integration seam.
    return es_config.install(managed_systems)
