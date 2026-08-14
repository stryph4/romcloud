"""One authoritative refresh path for ROMCloud's ES presentation."""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import Iterable

from romcloud.core.capabilities import OperatingMode
from romcloud.infrastructure.config import AppConfig
from romcloud.integrations.batocera import es_config
from romcloud.integrations.batocera.es_systems import GeneratedOverride


def refresh_emulationstation(
    config: AppConfig,
    managed_systems: Iterable[str],
    *,
    mode: OperatingMode | str | None = None,
) -> GeneratedOverride | None:
    """Reconcile ROMCloud's owned ES registration after local state commits."""
    if mode is None:
        from romcloud.infrastructure.library_view import operating_mode

        selected = operating_mode(config)
    else:
        selected = OperatingMode(mode)
    if selected is OperatingMode.CONNECTED:
        es_config.remove()
        return None
    # install() and refresh() are intentionally identical; the former keeps
    # this shared path compatible with setup's first-write integration seam.
    if hasattr(config, "data_path"):
        from romcloud.bootstrap.container import Container

        return es_config.install(
            managed_systems, system_registry=Container(config).system_registry
        )
    # Lightweight adapter/test configurations predate registry injection.
    return es_config.install(managed_systems)


def reload_emulationstation() -> bool:
    """Ask Batocera to reload the running frontend after a mode transition.

    Development and non-Batocera environments do not provide the swissknife
    utility, so there is no running Batocera frontend to notify there.
    """
    command = shutil.which("batocera-es-swissknife")
    if command is None:
        return False
    subprocess.run(
        [command, "--restart"],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    return True
