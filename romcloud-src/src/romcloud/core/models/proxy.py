"""Proxy record domain model."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone


@dataclass
class ProxyRecord:
    """Tracks a ``.romcloud`` proxy file that ROMCloud created.

    ROMCloud only manipulates proxy files that are recorded here.
    It will never touch a ``.romcloud`` file that it did not create.
    """

    game_id: str
    proxy_path: str
    """Absolute path to the ``.romcloud`` file on the local filesystem."""
    created_at: datetime

    @classmethod
    def create(cls, game_id: str, proxy_path: str) -> ProxyRecord:
        return cls(
            game_id=game_id,
            proxy_path=proxy_path,
            created_at=datetime.now(timezone.utc),
        )
