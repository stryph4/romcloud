"""Short-lived, single-window game-stop SaveSync conflict prompt."""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from ports_gfx.actions import Action
from ports_gfx.client import operation_result, start_backend_operation
from ports_gfx.hold_confirm import HoldToConfirmState, handle_hold_to_confirm_event
from ports_gfx.input_manager import InputEvent, InputManager
from ports_gfx.layout import Rect, compute_layout, compute_vertical_control_rects
from ports_gfx.operation import OperationRunner

ACTION_LABELS = (
    "Upload Local Save",
    "Download Remote Save",
    "Resolve Later",
)
_ACTION_NAMES = ("upload-local", "download-remote", "resolve-later")
_DESTRUCTIVE = frozenset({0, 1})
_OPERATION_TIMEOUT = 120.0
_FOCUS_TIMEOUT_SECONDS = 1.5
_FOCUS_COMMAND_TIMEOUT_SECONDS = 0.75

LOADING = "loading"
DISPLAYING = "displaying"
APPLYING = "applying"
DONE = "done"


@dataclass
class ConflictPopupState:
    romcloud_bin: str
    source: str = "automatic"
    step: str = LOADING
    selected_index: int = 0
    conflict: dict[str, Any] = field(default_factory=dict)
    error: str = ""
    confirm: HoldToConfirmState = field(default_factory=HoldToConfirmState)
    popen: Optional[Callable[..., object]] = None
    clock: Optional[Callable[[], float]] = None
    record: Optional[Callable[..., None]] = None
    _runner: Optional[OperationRunner] = field(default=None, repr=False)
    _conflicts: list[dict[str, Any]] = field(default_factory=list, repr=False)

    def start(self) -> None:
        self.error = ""
        self._record("conflict_load_start", source=self.source)
        self._start_operation("savesync-conflicts", {"source": self.source})
        self.step = LOADING

    def select(self, index: int) -> None:
        selected = max(0, min(index, len(ACTION_LABELS) - 1))
        if selected != self.selected_index:
            self.confirm.reset()
        self.selected_index = selected

    @property
    def hold_label(self) -> str:
        if self.selected_index == 0:
            return "Hold to upload local save…"
        if self.selected_index == 1:
            return "Hold to download remote save…"
        return "Resolve Later does not modify either copy."

    def handle_event(self, event: InputEvent) -> None:
        if self.step != DISPLAYING:
            return
        if event.touch_index is not None:
            self.select(event.touch_index)
        if event.action == Action.UP:
            self.select(self.selected_index - 1)
            return
        if event.action == Action.DOWN:
            self.select(self.selected_index + 1)
            return
        if event.action == Action.BACK:
            self._apply("resolve-later")
            return
        if self.selected_index in _DESTRUCTIVE:
            handle_hold_to_confirm_event(event, self.confirm)
        elif event.action == Action.CONFIRM:
            self._apply("resolve-later")

    def update(self, dt: float) -> None:
        if self.step == DISPLAYING and self.selected_index in _DESTRUCTIVE:
            self.confirm.update(dt)
            if self.confirm.confirmed:
                self._apply(_ACTION_NAMES[self.selected_index])
        self.poll()

    def poll(self) -> None:
        if self._runner is None:
            return
        self._runner.poll()
        if not self._runner.is_finished:
            return
        result = operation_result(self._runner)
        operation_step = self.step
        self._runner = None
        if not result.ok:
            self._record(
                "conflict_backend_failed",
                source=self.source,
                step=operation_step,
                error=result.error,
            )
            self.error = result.error or "SaveSync conflict operation failed."
            self.confirm.reset()
            self.step = DISPLAYING if self.conflict else DONE
            return
        if operation_step == LOADING:
            conflicts = result.data.get("conflicts", [])
            if not isinstance(conflicts, list):
                self.error = "Unexpected SaveSync conflict response."
                self.step = DONE
                return
            self._conflicts = [item for item in conflicts if isinstance(item, dict)]
            self._record(
                "conflict_load_complete",
                source=self.source,
                count=len(self._conflicts),
            )
            if not self._conflicts:
                self.step = DONE
                return
            self.conflict = self._conflicts[0]
            self.selected_index = 0
            self.confirm.reset()
            self.step = DISPLAYING
            return
        handled = str(self.conflict.get("conflict_id", ""))
        self._record(
            "conflict_action_complete",
            source=self.source,
            conflict_id=handled,
        )
        self._conflicts = [
            item for item in self._conflicts if item.get("conflict_id") != handled
        ]
        if self._conflicts:
            self.conflict = self._conflicts[0]
            self.selected_index = 0
            self.confirm.reset()
            self.step = DISPLAYING
        elif self.source == "automatic":
            # A second gameStop can enqueue while this singleton owns the
            # display. Reload once through the same window to drain it.
            self.conflict = {}
            self.start()
        else:
            self.conflict = {}
            self.step = DONE

    def cancel_pending(self) -> None:
        if self._runner is not None:
            self._runner.cancel(reason="popup closed")
            self._runner = None

    def _apply(self, action: str) -> None:
        if self.step != DISPLAYING or not self.conflict:
            return
        self.error = ""
        self._record(
            "conflict_action_start",
            source=self.source,
            conflict_id=self.conflict["conflict_id"],
            action=action,
        )
        self._start_operation(
            "savesync-conflict-action",
            {
                "conflict_id": self.conflict["conflict_id"],
                "action": action,
                "source": self.source,
                "progress": True,
            },
        )
        self.step = APPLYING

    def _start_operation(
        self, action: str, payload: Optional[dict[str, Any]] = None
    ) -> None:
        kwargs: dict[str, Any] = {
            "max_runtime": _OPERATION_TIMEOUT,
            "timeout_message": "SaveSync conflict operation timed out.",
        }
        if self.popen is not None:
            kwargs["popen"] = self.popen
        if self.clock is not None:
            kwargs["clock"] = self.clock
        self._runner = start_backend_operation(
            self.romcloud_bin,
            action,
            payload,
            **kwargs,
        )

    def _record(self, event: str, **fields: object) -> None:
        if self.record is not None:
            self.record(event, **fields)


def action_rects(layout) -> list[Rect]:  # noqa: ANN001
    line_height = layout.fonts.body + 6
    top = layout.navigation_rect.y + line_height * 4
    bottom = layout.navigation_rect.bottom - line_height * 3
    area = Rect(
        layout.navigation_rect.x,
        top,
        layout.navigation_rect.w,
        max(1, bottom - top),
    )
    return compute_vertical_control_rects(area, len(ACTION_LABELS))


def run_conflict_popup(romcloud_bin: str) -> int:
    """Open one display, drain the queued exact IDs, then exit."""
    pygame = None
    from ports_gfx.display_diagnostics import DisplayDiagnostics

    diagnostics = DisplayDiagnostics(romcloud_bin)
    diagnostics.record(
        "conflict_popup_start",
        environment=diagnostics.environment(),
        cwd=os.getcwd(),
        parent_pid=os.getppid(),
    )
    try:
        import pygame

        from ports_gfx.app import (
            _BG_COLOR,
            _CARD_BG,
            _ERROR_COLOR,
            _FG_COLOR,
            _HINT_COLOR,
            _SELECTED_BG,
            _SUCCESS_COLOR,
            _build_fonts,
            _detect_screen_size,
            _draw_progress_bar,
            _open_display,
        )

        os.environ.setdefault("SDL_VIDEO_MINIMIZE_ON_FOCUS_LOSS", "0")
        diagnostics.record("conflict_pygame_imported", pygame_version=pygame.version.ver)
        pygame.init()
        diagnostics.record("conflict_pygame_initialized")
        screen_w, screen_h = _detect_screen_size(pygame, diagnostics)
        # Set the unique title before SDL maps the window so X11/Wayland focus
        # helpers can identify it immediately.
        pygame.display.set_caption("ROMCloud Save Conflict")
        screen = _open_display(pygame, screen_w, screen_h, diagnostics)
        diagnostics.record(
            "conflict_display_ready",
            surface_size=list(screen.get_size()),
            **_focus_state(pygame),
        )
        try:
            pygame.event.set_grab(True)
        except Exception as exc:  # noqa: BLE001 - focus is verified separately
            diagnostics.record("conflict_input_grab_failed", error=str(exc))
        if not _acquire_popup_focus(pygame, diagnostics.record):
            diagnostics.record(
                "conflict_popup_failed",
                reason="keyboard_focus_timeout",
                queue_preserved=True,
            )
            print(
                "error: SaveSync conflict popup could not acquire keyboard focus; "
                "the conflict remains available for manual resolution"
            )
            return 1
        layout = compute_layout(screen_w, screen_h, len(ACTION_LABELS))
        fonts = _build_fonts(pygame, layout)
        inputs = InputManager(pygame, romcloud_bin)
        state = ConflictPopupState(
            romcloud_bin,
            source="automatic",
            record=diagnostics.record,
        )
        state.start()
        clock = pygame.time.Clock()
        running = True
        rendered = False
        rendered_frames = 0
        while running and state.step != DONE:
            dt = clock.tick(30) / 1000.0
            rects = action_rects(layout)
            now = pygame.time.get_ticks() / 1000.0
            for event in pygame.event.get():
                if event.type == pygame.QUIT:
                    running = False
                    break
                translated = inputs.handle_event(
                    event,
                    screen_w=screen_w,
                    screen_h=screen_h,
                    rects=rects,
                    now=now,
                )
                state.handle_event(translated)
            for action in inputs.update(dt):
                state.handle_event(InputEvent(action=action, source="controller"))
            state.update(dt)
            render_conflict_resolver(
                pygame,
                screen,
                fonts,
                layout,
                state,
                rects,
                colors={
                    "bg": _BG_COLOR,
                    "card": _CARD_BG,
                    "error": _ERROR_COLOR,
                    "fg": _FG_COLOR,
                    "hint": _HINT_COLOR,
                    "selected": _SELECTED_BG,
                    "success": _SUCCESS_COLOR,
                },
                draw_progress=_draw_progress_bar,
            )
            if not rendered:
                rendered = True
                diagnostics.record(
                    "conflict_first_frame_rendered",
                    source=state.source,
                    **_focus_state(pygame),
                )
            rendered_frames += 1
            if rendered_frames == 15:
                diagnostics.record(
                    "conflict_focus_after_half_second",
                    **_focus_state(pygame),
                )
        state.cancel_pending()
        diagnostics.record(
            "conflict_popup_exit",
            queue_drained=state.step == DONE,
            window_closed=not running,
        )
        return 0
    except Exception as exc:  # noqa: BLE001 - never crash the lifecycle worker
        diagnostics.record("conflict_popup_failed", error=str(exc))
        print(f"error: SaveSync conflict popup failed: {exc}")
        return 1
    finally:
        if pygame is not None:
            try:
                pygame.event.set_grab(False)
            except Exception:  # noqa: BLE001 - cleanup is best effort
                pass
            pygame.quit()


def _acquire_popup_focus(
    pygame,  # noqa: ANN001
    record: Callable[..., None],
    *,
    timeout: float = _FOCUS_TIMEOUT_SECONDS,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    request_focus: Optional[Callable[[], str]] = None,
) -> bool:
    """Boundedly raise the popup and prove that it owns keyboard input."""
    started = clock()
    deadline = started + max(0.0, timeout)
    delay = 0.05
    attempts = 0
    last_request = "not-requested"
    focus_request = request_focus or (lambda: _request_popup_focus(pygame))
    record("conflict_focus_wait_started", timeout=timeout)

    while True:
        try:
            pygame.event.pump()
        except Exception:  # noqa: BLE001 - get_focused remains authoritative
            pass
        try:
            focused = bool(pygame.key.get_focused())
        except Exception:  # noqa: BLE001 - a missing focus signal must fail safe
            focused = False
        if focused:
            record(
                "conflict_focus_acquired",
                attempts=attempts,
                elapsed=round(max(0.0, clock() - started), 6),
                request=last_request,
                **_focus_state(pygame),
            )
            return True

        now = clock()
        if now >= deadline:
            record(
                "conflict_focus_timed_out",
                attempts=attempts,
                elapsed=round(max(0.0, now - started), 6),
                request=last_request,
                **_focus_state(pygame),
            )
            return False
        attempts += 1
        last_request = focus_request()
        sleep(min(delay, max(0.0, deadline - now)))
        delay = min(0.25, delay * 1.5)


def _request_popup_focus(pygame) -> str:  # noqa: ANN001
    """Request focus through SDL plus the active Batocera compositor path."""
    results: list[str] = []
    try:
        from pygame._sdl2.video import Window

        Window.from_display_module().focus()
        results.append("sdl-window-focus")
    except Exception as exc:  # noqa: BLE001 - external helpers remain available
        results.append(f"sdl-window-focus-failed:{exc}")

    environment = os.environ.copy()
    if environment.get("WAYLAND_DISPLAY"):
        wlrctl = shutil.which("wlrctl")
        if wlrctl:
            try:
                result = subprocess.run(
                    [
                        wlrctl,
                        "toplevel",
                        "focus",
                        "title:ROMCloud Save Conflict",
                    ],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    check=False,
                    timeout=_FOCUS_COMMAND_TIMEOUT_SECONDS,
                    env=environment,
                )
                results.append(f"wlrctl-focus:{result.returncode}")
            except (OSError, subprocess.SubprocessError) as exc:
                results.append(f"wlrctl-focus-failed:{exc}")

    if environment.get("DISPLAY"):
        xdotool = shutil.which("xdotool")
        try:
            window_id = int(pygame.display.get_wm_info().get("window", 0))
        except (AttributeError, TypeError, ValueError):
            window_id = 0
        if xdotool and window_id > 0:
            for action in (
                [xdotool, "windowraise", str(window_id)],
                [xdotool, "windowactivate", "--sync", str(window_id)],
            ):
                try:
                    result = subprocess.run(
                        action,
                        stdin=subprocess.DEVNULL,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        check=False,
                        timeout=_FOCUS_COMMAND_TIMEOUT_SECONDS,
                        env=environment,
                    )
                    results.append(f"xdotool-{action[1]}:{result.returncode}")
                except (OSError, subprocess.SubprocessError) as exc:
                    results.append(f"xdotool-{action[1]}-failed:{exc}")

    return ",".join(results) or "no-focus-helper"


def _focus_state(pygame) -> dict[str, object]:  # noqa: ANN001
    result: dict[str, object] = {}
    for name, owner, method_name in (
        ("display_active", pygame.display, "get_active"),
        ("keyboard_focused", pygame.key, "get_focused"),
    ):
        try:
            result[name] = bool(getattr(owner, method_name)())
        except Exception as exc:  # noqa: BLE001 - diagnostics are best effort
            result[f"{name}_error"] = str(exc)
    return result


def render_conflict_resolver(  # noqa: ANN001
    pygame,
    screen,
    fonts,
    layout,
    state: ConflictPopupState,
    rects: list[Rect],
    *,
    colors: dict,
    draw_progress,
) -> None:
    screen.fill(colors["bg"])
    title = fonts["title"].render("Save Conflict Detected", True, colors["fg"])
    screen.blit(title, (layout.header_rect.x, layout.header_rect.y))
    x = layout.navigation_rect.x
    y = layout.navigation_rect.y
    line_h = layout.fonts.body + 6
    if state.conflict:
        local = state.conflict.get("local", {})
        remote = state.conflict.get("remote", {})
        info = (
            str(state.conflict.get("group_label") or state.conflict.get("group_id", "Save group")),
            f"Layout: {state.conflict.get('layout_id', 'unknown')}",
            "Local: "
            f"{int(local.get('artifact_count', 0))} file(s), "
            f"{_size(int(local.get('total_bytes', 0)))}",
            "Remote: "
            f"{int(remote.get('artifact_count', 0))} file(s), "
            f"{_size(int(remote.get('total_bytes', 0)))}",
        )
        for line in info:
            screen.blit(fonts["body"].render(line, True, colors["fg"]), (x, y))
            y += line_h
    else:
        status = "Loading conflict…" if state.step == LOADING else "Working…"
        screen.blit(fonts["body"].render(status, True, colors["fg"]), (x, y))

    for index, (label, rect) in enumerate(zip(ACTION_LABELS, rects)):
        color = colors["selected"] if index == state.selected_index else colors["card"]
        pygame.draw.rect(screen, color, (rect.x, rect.y, rect.w, rect.h), border_radius=6)
        text = fonts["body"].render(label, True, colors["fg"])
        screen.blit(text, (rect.x + 16, rect.y + max(0, (rect.h - text.get_height()) // 2)))

    if rects:
        label_y = rects[-1].bottom + max(8, layout.fonts.hint // 2)
        label = state.hold_label if state.step == DISPLAYING else "Applying safely…"
        screen.blit(fonts["hint"].render(label, True, colors["hint"]), (x, label_y))
        bar = Rect(
            x,
            label_y + layout.fonts.hint + 6,
            min(layout.navigation_rect.w, 560),
            max(8, layout.fonts.body // 2),
        )
        progress = state.confirm.progress if state.step == DISPLAYING else 0.0
        draw_progress(pygame, screen, bar, progress)
        if state.error:
            error = fonts["hint"].render(state.error[:100], True, colors["error"])
            screen.blit(error, (x, bar.bottom + 8))

    hint = fonts["hint"].render(
        "Up/Down select   Hold A/Enter for overwrite   B/Esc resolve later",
        True,
        colors["hint"],
    )
    screen.blit(hint, (layout.hint_rect.x, layout.hint_rect.y))
    pygame.display.flip()


def _size(value: int) -> str:
    size = float(max(0, value))
    for unit in ("B", "KiB", "MiB", "GiB"):
        if size < 1024.0 or unit == "GiB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024.0
    return f"{size:.1f} GiB"
