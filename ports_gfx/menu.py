"""Pure menu state — no pygame, no I/O, no layout/geometry.

Selection is a plain index into ``items``. *How* that index changes in
response to a directional key press is entirely the responsibility of
``layout.find_next_focus_index``, which reasons about the actual rendered
widget rects rather than array order — see ``app.py``'s event loop.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Sequence


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
    description: str = ""
    active: bool = False


EXIT_ACTION = "exit"
CONTROLLER_TEST_ACTION = "controller-test"
"""Sentinel action the UI layer interprets as "switch to the controller
mapping/diagnostics screen" rather than dispatching to the backend."""

CATEGORY_ACTION_PREFIX = "category:"
BACK_ACTION = "navigation-back"


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


class NavigationState:
    """One-pane hierarchical menu with focus restoration.

    Entering a category replaces the current item list; returning restores
    the category that was focused at the root.  The model deliberately has
    only two levels so routine actions remain ``category -> action``.
    """

    def __init__(
        self,
        roots: Sequence[MenuItem],
        categories: Mapping[str, Sequence[MenuItem]],
    ) -> None:
        if not roots:
            raise ValueError("NavigationState requires root categories")
        self._roots = list(roots)
        self._categories = {key: list(items) for key, items in categories.items()}
        self._category: str | None = None
        self._selected = 0
        self._root_selected = 0

    @property
    def level(self) -> str:
        return "root" if self._category is None else "category"

    @property
    def category(self) -> str | None:
        return self._category

    @property
    def title(self) -> str:
        return "ROMCloud" if self._category is None else self._category

    @property
    def items(self) -> list[MenuItem]:
        if self._category is None:
            return list(self._roots)
        return [*self._categories[self._category], MenuItem("Back", BACK_ACTION)]

    @property
    def selected_index(self) -> int:
        return self._selected

    @property
    def selected_item(self) -> MenuItem:
        return self.items[self._selected]

    def select(self, index: int) -> None:
        self._selected = max(0, min(index, len(self.items) - 1))

    def enter_selected_category(self) -> bool:
        action = self.selected_item.action
        if not action.startswith(CATEGORY_ACTION_PREFIX):
            return False
        category = action[len(CATEGORY_ACTION_PREFIX) :]
        if category not in self._categories:
            return False
        self._root_selected = self._selected
        self._category = category
        self._selected = 0
        return True

    def back(self) -> bool:
        """Return one level; False means already at the root."""
        if self._category is None:
            return False
        self._category = None
        self._selected = self._root_selected
        return True

    def open_category(self, category: str, *, action: str | None = None) -> bool:
        """Open *category*, optionally focusing a canonical action."""
        if category not in self._categories:
            return False
        for index, item in enumerate(self._roots):
            if item.action == f"{CATEGORY_ACTION_PREFIX}{category}":
                self._root_selected = index
                break
        self._category = category
        self._selected = 0
        if action is not None:
            for index, item in enumerate(self.items):
                if item.action == action:
                    self._selected = index
                    break
        return True
