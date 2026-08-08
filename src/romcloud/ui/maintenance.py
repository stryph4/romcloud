"""Maintenance UI — controller-friendly menu launched from Batocera Ports.

Provides a simple full-screen curses interface equivalent to the CLI commands.
Users can navigate with D-pad / arrow keys.  All business logic is delegated
to the Container services — the UI contains no game logic.

Intended to be launched as a Batocera Port script:
    /userdata/roms/ports/ROMCloud.sh → romcloud-ui
"""

from __future__ import annotations

try:
    import curses
except Exception:
    curses = None  # type: ignore[assignment]
import sys
from typing import Callable, Optional


def run_maintenance_ui(container) -> None:  # type: ignore[no-untyped-def]
    """Entry point for the maintenance TUI."""
    if not sys.stdout.isatty():
        print("Maintenance UI requires an interactive terminal.")
        return
    try:
        if curses is not None:
            curses.wrapper(_main_menu, container)
        else:
            print("Maintenance UI requires the curses module, which is not available on this system.")
    except KeyboardInterrupt:
        pass


# ── menu framework ────────────────────────────────────────────────────────────


class MenuItem:
    def __init__(self, label: str, action: Callable) -> None:
        self.label = label
        self.action = action


def _main_menu(stdscr: "curses.window", container) -> None:
    curses.curs_set(0)

    def show_message(msg: str) -> None:
        stdscr.clear()
        h, w = stdscr.getmaxyx()
        lines = msg.splitlines()
        start = max(0, h // 2 - len(lines) // 2)
        for i, line in enumerate(lines):
            if start + i < h:
                stdscr.addstr(start + i, 2, line[: w - 4])
        stdscr.addstr(min(start + len(lines) + 1, h - 1), 2, "Press any key to return...")
        stdscr.refresh()
        stdscr.nodelay(False)
        stdscr.getch()
        stdscr.nodelay(True)

    items = [
        MenuItem("Catalog Status", lambda: show_message(_status_text(container))),
        MenuItem("Refresh Catalog", lambda: show_message(_refresh_text(container))),
        MenuItem("Health Check", lambda: show_message(_health_text(container))),
        MenuItem("Cache Status", lambda: show_message(_cache_text(container))),
        MenuItem("Exit", None),
    ]

    selected = 0
    stdscr.nodelay(True)

    while True:
        stdscr.clear()
        h, w = stdscr.getmaxyx()

        stdscr.addstr(1, 2, "ROMCloud — Maintenance", curses.A_BOLD)
        stdscr.addstr(2, 2, "─" * (w - 4))

        for i, item in enumerate(items):
            attr = curses.A_REVERSE if i == selected else 0
            label = f"  {item.label}"
            row = 4 + i
            if row < h:
                stdscr.addstr(row, 2, label.ljust(min(40, w - 4)), attr)

        stdscr.addstr(min(4 + len(items) + 1, h - 1), 2, "↑↓ navigate   Enter select")
        stdscr.refresh()

        stdscr.nodelay(False)
        ch = stdscr.getch()
        stdscr.nodelay(True)

        if ch in (curses.KEY_UP, ord("k")):
            selected = (selected - 1) % len(items)
        elif ch in (curses.KEY_DOWN, ord("j")):
            selected = (selected + 1) % len(items)
        elif ch in (curses.KEY_ENTER, ord("\n"), ord("\r")):
            item = items[selected]
            if item.action is None:
                break
            item.action()
        elif ch in (ord("q"), ord("Q"), 27):
            break


# ── action text generators ────────────────────────────────────────────────────


def _status_text(container) -> str:
    games = container.catalog.list_games()
    summary = container.cache.status_summary()
    return (
        f"Catalog: {len(games)} games\n"
        f"Cached:  {summary['complete']} / {len(games)}\n"
        f"Pinned:  {summary['pinned']}\n"
    )


def _refresh_text(container) -> str:
    try:
        result = container.catalog.refresh()
        return str(result)
    except Exception as exc:  # noqa: BLE001
        return f"Error: {exc}"


def _health_text(container) -> str:
    cfg = container.config
    lines = []
    reachable = container.provider.is_reachable(cfg.source.rom_root)
    lines.append(f"Source: {'OK' if reachable else 'UNREACHABLE'}")
    return "\n".join(lines)


def _cache_text(container) -> str:
    summary = container.cache.status_summary()

    def fmt(n: int) -> str:
        return f"{n / 1024**3:.1f} GB" if n >= 1024**3 else f"{n / 1024**2:.0f} MB"

    return (
        f"Cached:  {summary['complete']} games ({fmt(summary['total_bytes'])})\n"
        f"Pinned:  {summary['pinned']}\n"
        f"Free:    {fmt(summary['free_bytes'])}\n"
        f"Quota:   {fmt(summary['total_bytes'])} / {fmt(summary['max_bytes'])}\n"
    )
