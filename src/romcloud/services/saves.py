"""Save sync service (stub for v0.1).

Save synchronisation reuses the same :class:`~romcloud.core.storage.StorageProvider`
abstraction as ROM caching.  Save data is NOT disposable — divergent saves are
always preserved before overwriting.

This module establishes the architectural slot.  Full implementation follows
after the local cache vertical slice is stable.
"""

from __future__ import annotations

from romcloud.infrastructure.logging import get_logger

log = get_logger("saves")


class SaveSyncService:
    """Manages save data synchronisation.

    .. note::
        Not yet implemented.  The interface is defined here so the CLI and
        bootstrap layer can reference it without placeholder hacks elsewhere.
    """

    def sync(self, game_id: str | None = None) -> None:
        raise NotImplementedError("Save sync not yet implemented in v0.1")

    def status(self, game_id: str | None = None) -> dict:
        raise NotImplementedError("Save sync not yet implemented in v0.1")
