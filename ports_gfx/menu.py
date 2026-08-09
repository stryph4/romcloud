"""Pure menu navigation state — no pygame, no I/O, fully unit-testable.

Kept separate from ``app.py`` (the pygame render/event loop) so the actual
navigation logic can be tested without pygame installed — pygame is only
ever present on Batocera's system Python, never in ROMCloud's dev/test venv.
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
    """Tracks which menu item is currently selected.

    Navigation wraps around at both ends (moving up from the first item
    selects the last, and vice versa) — a common, predictable convention
    for D-pad/controller-driven menus.
    """

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

    def move_up(self) -> None:
        self._selected = (self._selected - 1) % len(self._items)

    def move_down(self) -> None:
        self._selected = (self._selected + 1) % len(self._items)
