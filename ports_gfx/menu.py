"""Pure menu state — no pygame, no I/O, no layout/geometry.

Selection is a plain index into ``items``. *How* that index changes in
response to a directional key press is entirely the responsibility of
``layout.find_next_focus_index``, which reasons about the actual rendered
widget rects rather than array order — see ``app.py``'s event loop.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence


@dataclass(frozen=True)
class MenuItem:
    """A single menu entry.

    *action* is either one of the ``romcloud uidata`` action names
    (``"status"``, ``"refresh"``, ``"healthcheck"``, ``"cache-status"``) or
    the sentinel ``"exit"``, which the UI layer interprets as "quit" rather
    than dispatching to the backend.
    """

    label: str
    action: str


EXIT_ACTION = "exit"


class MenuState:
    """Tracks which menu item is currently selected."""

    def __init__(self, items: Sequence[MenuItem]) -> None:
        if not items:
            raise ValueError("MenuState requires at least one item")
        self._items = list(items)
        self._selected = 0

    @property
    def items(self) -> list[MenuItem]:
        return list(self._items)

    @property
    def selected_index(self) -> int:
        return self._selected

    @property
    def selected_item(self) -> MenuItem:
        return self._items[self._selected]

    def select(self, index: int) -> None:
        """Set the selected index directly (e.g. from geometric focus
        navigation) — clamped to the valid range."""
        self._selected = max(0, min(index, len(self._items) - 1))

