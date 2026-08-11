"""Reusable long-running "operation" screen — state and pure helpers.

This is the shared graphical pattern for any backend action that may take
significant time (catalog refresh today; update/repair/diagnostics/mount
reconnect/sync are expected to reuse it later — see module docstring in
``operation.py``). Everything here is plain Python: no ``pygame`` import,
so it is fully unit-tested directly. ``app.py`` owns the actual rendering
and wires this state into the event loop, the same split already used for
``menu.py``/``layout.py``.

An :class:`OperationSpec` names one reusable action (a title plus the
extra ``romcloud`` argv to run); adding a new operation-screen-driven
action later is just adding an entry to a lookup table, never touching
this module or the rendering code.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Sequence

from ports_gfx.actions import Action
from ports_gfx.activity import parse_progress_line
from ports_gfx.input_manager import InputEvent
from ports_gfx.operation import OperationLine, OperationRunner, OperationState

OPERATION_SCREEN = "operation"
MENU_SCREEN = "menu"


@dataclass(frozen=True)
class OperationSpec:
    """One reusable operation-screen action: a display title and the argv
    appended after the ``romcloud`` binary path (e.g. ``("refresh",)`` or
    ``("update", "--check")``) — never a new backend command, only an
    existing CLI entry point already safe to run directly."""

    title: str
    args: tuple[str, ...]
    # Explicit ownership flag for the terminal GUI-relaunch handoff (see
    # ``ports_gfx.relaunch.GuiRelaunchCoordinator``): True only for the
    # self-update action. A completed mode transition, catalog refresh,
    # mount/reconnect, or any other operation must never arm a replacement
    # GUI launch — this is a data field, not a display-title string
    # comparison, so it can't drift if titles are ever renamed or reused.
    arms_gui_relaunch: bool = False


@dataclass
class OperationScreenState:
    """Per-session UI state for one operation screen: which
    :class:`OperationRunner` it owns, and how far the user has scrolled
    back through its output."""

    title: str
    runner: OperationRunner
    scroll_offset: int = 0
    """Rows back from the most recent line (0 = pinned to the bottom)."""
    auto_scroll: bool = True
    """While True, new output keeps the view pinned to the bottom;
    scrolling up disables it until the user scrolls back down (or the
    view is reset by starting a new operation)."""
    details_expanded: bool = False
    arms_gui_relaunch: bool = False

    @property
    def state(self) -> OperationState:
        return self.runner.state


    @property
    def is_finished(self) -> bool:
        return self.runner.is_finished

    @property
    def succeeded(self) -> bool:
        return self.runner.state == OperationState.SUCCEEDED

    def poll(self) -> list[OperationLine]:
        drained = self.runner.poll()
        if self.auto_scroll:
            self.scroll_offset = 0
        return drained


def display_lines(runner: OperationRunner, *, details: bool = True) -> list[str]:
    """Format captured output for display — a stderr line is prefixed so
    it reads as visually distinct from ordinary stdout progress without
    the renderer needing to track per-line color state itself."""
    rendered: list[str] = []
    for line in runner.lines:
        event = parse_progress_line(line.text)
        if event is not None:
            rendered.append(event.display_line)
            if details and event.detail:
                rendered.append(f"  {event.detail}")
            continue
        if line.stream == "stdout" and not details:
            try:
                payload = json.loads(line.text)
            except ValueError:
                payload = None
            if isinstance(payload, dict) and "ok" in payload:
                continue
        if line.stream == "stdout" or details:
            rendered.append(f"! {line.text}" if line.stream == "stderr" else line.text)
    return rendered


def wrap_line(text: str, max_chars: int) -> list[str]:
    """Word-wrap one line of output to at most *max_chars* columns.

    Prefers breaking at spaces; a single "word" longer than *max_chars*
    (e.g. a long path) is hard-split so it never clips off the edge of the
    screen instead of wrapping.
    """
    if max_chars <= 0 or len(text) <= max_chars:
        return [text]

    words = text.split(" ")
    lines: list[str] = []
    current = ""
    for word in words:
        candidate = f"{current} {word}".strip()
        if len(candidate) <= max_chars:
            current = candidate
            continue
        if current:
            lines.append(current)
        while len(word) > max_chars:
            lines.append(word[:max_chars])
            word = word[max_chars:]
        current = word
    if current:
        lines.append(current)
    return lines or [""]


def wrap_lines(lines: Sequence[str], max_chars: int) -> list[str]:
    wrapped: list[str] = []
    for line in lines:
        wrapped.extend(wrap_line(line, max_chars))
    return wrapped


def visible_window(total_lines: int, viewport_rows: int, scroll_offset: int) -> tuple[int, int]:
    """Return the ``[start, end)`` slice of a full line list to display,
    given how many rows fit on screen and the current scroll offset (0 =
    pinned to the bottom/most recent output)."""
    if viewport_rows <= 0 or total_lines <= 0:
        return 0, 0
    if total_lines <= viewport_rows:
        return 0, total_lines
    max_offset = total_lines - viewport_rows
    offset = max(0, min(scroll_offset, max_offset))
    end = total_lines - offset
    start = max(0, end - viewport_rows)
    return start, end


def handle_operation_event(ievent: InputEvent, screen: OperationScreenState) -> str:
    """Translate one semantic input event into scroll/dismiss behavior.

    Returns the screen name to switch to next (``OPERATION_SCREEN`` to
    stay, ``MENU_SCREEN`` to return to the dashboard). Dismissing back to
    the menu is only ever allowed once the operation has actually
    finished — this is the safety rule that keeps a genuinely long-running
    backend action from being abandoned mid-flight by an impatient BACK
    press, matching "a legitimately long operation must not be treated as
    failed/interruptible just because it takes a while".
    """
    action = ievent.action

    if action == Action.UP:
        screen.auto_scroll = False
        screen.scroll_offset += 1
        return OPERATION_SCREEN

    if action == Action.DOWN:
        screen.scroll_offset = max(0, screen.scroll_offset - 1)
        if screen.scroll_offset == 0:
            screen.auto_scroll = True
        return OPERATION_SCREEN

    if action in (Action.LEFT, Action.RIGHT):
        screen.details_expanded = not screen.details_expanded
        screen.scroll_offset = 0
        screen.auto_scroll = True
        return OPERATION_SCREEN

    if action in (Action.BACK, Action.CONFIRM, Action.MENU):
        return MENU_SCREEN if screen.runner.is_finished else OPERATION_SCREEN

    return OPERATION_SCREEN
