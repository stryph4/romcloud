"""Provider-neutral managed-system selection and reconciliation."""

from __future__ import annotations

import os
from dataclasses import replace
from pathlib import Path
from typing import Any

from romcloud.bootstrap.container import Container
from romcloud.core.progress import ProgressSink, emit_progress
from romcloud.infrastructure.config import (
    canonical_system_ids,
    load_config,
    write_config,
)


def selection_status(config_path: Path) -> dict[str, Any]:
    """Return detected systems and the effective positive allowlist."""
    config = load_config(str(config_path))
    container = Container(config)
    detected = canonical_system_ids(
        container.provider.list_systems(config.source.rom_root),
        "detected systems",
    )
    launchable = tuple(
        system for system in detected if system in container.system_registry.names
    )
    selected = (
        launchable
        if config.source.selected_systems is None
        else tuple(
            system
            for system in launchable
            if system in config.source.selected_systems
        )
    )
    return {
        "detected_systems": list(launchable),
        "selected_systems": list(selected),
        "setting_missing": config.source.selected_systems is None,
    }


def update_selection(
    config_path: Path,
    payload: dict[str, Any],
    progress: ProgressSink = None,
) -> dict[str, Any]:
    """Persist an explicit allowlist, then run catalog/access reconciliation."""
    if "selected_systems" not in payload:
        raise ValueError("selected_systems is required.")
    selected = canonical_system_ids(
        payload["selected_systems"], "Select Systems request"
    )
    status = selection_status(config_path)
    detected = tuple(status["detected_systems"])
    unknown = sorted(set(selected) - set(detected))
    if unknown:
        raise ValueError(
            "Selected systems were not detected in this source: " + ", ".join(unknown)
        )

    old_selected = tuple(status["selected_systems"])
    config = load_config(str(config_path))
    from romcloud.integrations.batocera.direct_saves import MANIFEST_FILENAME

    if (
        os.path.lexists(Path(config.data_path) / MANIFEST_FILENAME)
        and config.source.selected_systems != selected
    ):
        raise ValueError(
            "Direct save routing is active. Switch to Cached Storage before "
            "changing selected systems so save authority can be handed off safely."
        )
    updated = replace(
        config,
        source=replace(config.source, selected_systems=selected),
    )

    emit_progress(
        progress,
        "system_selection",
        "save",
        "running",
        "Saving selected systems",
    )
    write_config(updated, str(config_path))
    emit_progress(
        progress,
        "system_selection",
        "save",
        "success",
        "Selected systems saved",
    )

    emit_progress(
        progress,
        "system_selection",
        "catalog",
        "running",
        "Reconciling selected systems",
    )
    container = Container(updated)
    refresh = container.catalog.refresh(progress=progress)
    if refresh.errors:
        detail = "; ".join(
            f"{system}: {message}" for system, message in refresh.errors
        )
        raise RuntimeError(f"System selection catalog reconciliation failed: {detail}")

    from romcloud.integrations.batocera.game_access import reconcile_game_access

    # The local Library Sync render is ownership-marker aware: ineligible
    # games remove only ROMCloud-tagged gamelist entries and preserve every
    # user-owned entry. It performs no remote metadata import here.
    access = reconcile_game_access(updated)
    emit_progress(
        progress,
        "system_selection",
        "complete",
        "success",
        "Selected systems reconciled",
    )
    old_set = set(old_selected)
    new_set = set(selected)
    return {
        "detected_systems": list(detected),
        "selected_systems": list(selected),
        "newly_selected": sorted(new_set - old_set),
        "newly_deselected": sorted(old_set - new_set),
        "catalog_added": refresh.added,
        "catalog_removed": refresh.removed,
        "access_created": access.created,
        "access_removed": access.removed,
    }
