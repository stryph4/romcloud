"""ROMCloud-owned on-screen keyboard (OSK) foundation.

Built for the upcoming graphical setup wizard (SMB server/share/username/
password entry, etc.) but usable by any future text field — this module
only owns text-entry *state* and *key geometry*; it renders nothing and
knows nothing about any particular screen. Navigation reuses
``layout.find_next_focus_index`` against the OSK's own computed key rects,
exactly like the main menu — so a controller/keyboard/touch user gets the
same navigation model everywhere in the app, not a second bespoke one.

Physical keyboard text entry keeps working while the OSK is shown for the
same field: ``pygame.TEXTINPUT`` events are still delivered to
:meth:`OskState.insert_text` regardless of whether any on-screen key is
ever pressed (see ``app.py``'s event loop).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from ports_gfx.layout import Rect, compute_safe_area

MASK_CHAR = "\u2022"  # "•"


@dataclass(frozen=True)
class OskKey:
    kind: str
    """One of: char, space, backspace, shift, symbols, mask, confirm, cancel."""

    char: Optional[str]
    row: int
    start: float
    """Fraction (0..1) of the row's width where this key begins."""
    width: float
    """Fraction (0..1) of the row's width this key occupies."""


def _row(row_index: int, entries: list[tuple[str, Optional[str], float]]) -> list[OskKey]:
    total_units = sum(units for _, _, units in entries)
    keys = []
    cursor = 0.0
    for kind, char, units in entries:
        width = units / total_units
        keys.append(OskKey(kind=kind, char=char, row=row_index, start=cursor, width=width))
        cursor += width
    return keys


def _bottom_row(row_index: int, *, masked: bool) -> list[OskKey]:
    entries: list[tuple[str, Optional[str], float]] = [("symbols", None, 1.3), ("space", None, 4.0)]
    if masked:
        entries.append(("mask", None, 1.5))
    entries += [("cancel", None, 1.3), ("confirm", None, 2.0)]
    return _row(row_index, entries)


def _letter_rows(masked: bool) -> list[OskKey]:
    row0 = _row(0, [("char", c, 1.0) for c in "qwertyuiop"])
    row1 = _row(1, [("char", c, 1.0) for c in "asdfghjkl"])
    row2 = _row(
        2,
        [("shift", None, 1.5)] + [("char", c, 1.0) for c in "zxcvbnm"] + [("backspace", None, 1.5)],
    )
    row3 = _bottom_row(3, masked=masked)
    return row0 + row1 + row2 + row3


def _symbol_rows(masked: bool) -> list[OskKey]:
    row0 = _row(0, [("char", c, 1.0) for c in "1234567890"])
    row1 = _row(1, [("char", c, 1.0) for c in "-/:;()$&@\""])
    row2 = _row(
        2,
        [("shift", None, 1.5)] + [("char", c, 1.0) for c in ".,?!'"] + [("backspace", None, 1.5)],
    )
    row3 = _bottom_row(3, masked=masked)
    return row0 + row1 + row2 + row3


_ROW_COUNT = 4


def compute_osk_layout(safe_area: Rect, keys: list[OskKey]) -> list[Rect]:
    """Key rects for *keys* (one page's worth), laid out within
    *safe_area* — the OSK reserves its top ~18% for the text-field
    display, and divides the remainder evenly across ``_ROW_COUNT`` rows.
    Purely a function of geometry, like ``layout.compute_card_rects``.
    """
    text_area_h = int(safe_area.h * 0.18)
    grid_h = safe_area.h - text_area_h
    row_h = grid_h / _ROW_COUNT
    spacing = max(2, int(min(safe_area.w, safe_area.h) * 0.006))

    rects = []
    for key in keys:
        row_y = safe_area.y + text_area_h + key.row * row_h
        x = safe_area.x + key.start * safe_area.w
        w = key.width * safe_area.w
        rects.append(
            Rect(
                x=int(x + spacing / 2),
                y=int(row_y + spacing / 2),
                w=max(1, int(w - spacing)),
                h=max(1, int(row_h - spacing)),
            )
        )
    return rects


def compute_osk_text_rect(safe_area: Rect) -> Rect:
    text_area_h = int(safe_area.h * 0.18)
    return Rect(x=safe_area.x, y=safe_area.y, w=safe_area.w, h=max(1, text_area_h))


class OskState:
    """Text-entry state for a single field. Instantiate one per active
    text field; discard it when the field loses focus/closes.
    """

    def __init__(self, *, initial_text: str = "", masked: bool = False) -> None:
        self.text = initial_text
        self.masked = masked
        self.mask_revealed = False
        self.shift = False
        self.page = "letters"
        self.selected_index = 0
        self.confirmed = False
        self.cancelled = False
        self._letters_keys = _letter_rows(masked)
        self._symbols_keys = _symbol_rows(masked)

    @property
    def keys(self) -> list[OskKey]:
        return self._letters_keys if self.page == "letters" else self._symbols_keys

    @property
    def displayed_text(self) -> str:
        if self.masked and not self.mask_revealed:
            return MASK_CHAR * len(self.text)
        return self.text

    def key_label(self, key: OskKey) -> str:
        if key.kind == "char":
            return (key.char or "").upper() if self.shift else (key.char or "")
        if key.kind == "space":
            return "Space"
        if key.kind == "backspace":
            return "\u232b"
        if key.kind == "shift":
            return "\u21e7"
        if key.kind == "symbols":
            return "123" if self.page == "letters" else "ABC"
        if key.kind == "mask":
            return "Hide" if self.mask_revealed else "Show"
        if key.kind == "confirm":
            return "Confirm"
        if key.kind == "cancel":
            return "Cancel"
        return "?"

    # ── text mutation ──────────────────────────────────────────────────────

    def insert_text(self, text: str) -> None:
        """Insert *text* typed via a physical keyboard's ``TEXTINPUT``
        event — used as-is (the OS/keyboard layout already applied any
        shift/caps state), independent of the OSK's own on-screen shift."""
        self.text += text

    def backspace(self) -> None:
        self.text = self.text[:-1]

    def toggle_shift(self) -> None:
        self.shift = not self.shift

    def toggle_symbols(self) -> None:
        self.page = "symbols" if self.page == "letters" else "letters"
        self.selected_index = min(self.selected_index, len(self.keys) - 1)

    def toggle_mask_reveal(self) -> None:
        if self.masked:
            self.mask_revealed = not self.mask_revealed

    # ── activation (controller/touch/on-screen click) ──────────────────────

    def activate(self, index: int) -> None:
        """Perform the effect of pressing the on-screen key at *index* in
        the current page."""
        keys = self.keys
        if not (0 <= index < len(keys)):
            return
        key = keys[index]
        if key.kind == "char":
            self.insert_text((key.char or "").upper() if self.shift else (key.char or ""))
        elif key.kind == "space":
            self.insert_text(" ")
        elif key.kind == "backspace":
            self.backspace()
        elif key.kind == "shift":
            self.toggle_shift()
        elif key.kind == "symbols":
            self.toggle_symbols()
        elif key.kind == "mask":
            self.toggle_mask_reveal()
        elif key.kind == "confirm":
            self.confirmed = True
        elif key.kind == "cancel":
            self.cancelled = True

    def select(self, index: int) -> None:
        self.selected_index = max(0, min(index, len(self.keys) - 1))


def compute_layout_rects(state: OskState, screen_w: int, screen_h: int) -> list[Rect]:
    """Convenience: the safe-area-relative key rects for *state*'s current
    page at the given screen size."""
    return compute_osk_layout(compute_safe_area(screen_w, screen_h), state.keys)
