"""Unit tests for `ports_gfx.operation_screen` — pure state/helpers for the
reusable long-running operation screen (no pygame)."""

from __future__ import annotations

from dataclasses import dataclass, field

from ports_gfx.actions import Action
from ports_gfx.input_manager import InputEvent, InputManager
from ports_gfx.layout import Rect, compute_layout
from ports_gfx.operation import OperationLine, OperationState
from ports_gfx.operation_screen import (
    MENU_SCREEN,
    OPERATION_SCREEN,
    OperationScreenState,
    OperationSpec,
    display_lines,
    handle_operation_event,
    visible_window,
    wrap_line,
    wrap_lines,
)
from tests.unit._pygame_fakes import FakeEvent, FakeJoystick, make_fake_pygame


@dataclass
class _FakeRunner:
    """Minimal stand-in for `OperationRunner` — only what operation_screen
    actually touches: `.state`, `.is_finished`, `.lines`, `.poll()`."""

    state: OperationState = OperationState.RUNNING
    lines: list[OperationLine] = field(default_factory=list)
    poll_return: list[OperationLine] = field(default_factory=list)
    cancelled: bool = False

    @property
    def is_finished(self) -> bool:
        return self.state in (OperationState.SUCCEEDED, OperationState.FAILED)

    def poll(self) -> list[OperationLine]:
        return self.poll_return

    def cancel(self) -> None:
        self.cancelled = True
        self.state = OperationState.FAILED


class TestOperationSpec:
    def test_holds_title_and_args(self):
        spec = OperationSpec(title="Refresh Catalog", args=("refresh",))
        assert spec.title == "Refresh Catalog"
        assert spec.args == ("refresh",)


class TestWrapLine:
    def test_short_line_is_unchanged(self):
        assert wrap_line("hello", 20) == ["hello"]

    def test_exact_length_boundary_is_unchanged(self):
        assert wrap_line("12345", 5) == ["12345"]

    def test_wraps_at_word_boundaries(self):
        result = wrap_line("the quick brown fox jumps", 10)
        assert all(len(line) <= 10 for line in result)
        assert " ".join(result) == "the quick brown fox jumps"

    def test_single_overlong_word_is_hard_split(self):
        result = wrap_line("a" * 25, 10)
        assert result == ["a" * 10, "a" * 10, "a" * 5]

    def test_empty_line_returns_single_empty_string(self):
        assert wrap_line("", 10) == [""]


class TestWrapLines:
    def test_wraps_each_line_independently(self):
        result = wrap_lines(["short", "a" * 12], 5)
        assert result == ["short", "a" * 5, "a" * 5, "a" * 2]


class TestVisibleWindow:
    def test_fewer_lines_than_viewport_shows_everything(self):
        assert visible_window(total_lines=3, viewport_rows=10, scroll_offset=0) == (0, 3)

    def test_pinned_to_bottom_shows_most_recent_rows(self):
        assert visible_window(total_lines=100, viewport_rows=10, scroll_offset=0) == (90, 100)

    def test_scroll_offset_moves_the_window_back(self):
        assert visible_window(total_lines=100, viewport_rows=10, scroll_offset=5) == (85, 95)

    def test_scroll_offset_is_clamped_to_the_top(self):
        assert visible_window(total_lines=100, viewport_rows=10, scroll_offset=1000) == (0, 10)

    def test_non_positive_viewport_or_lines_returns_empty_window(self):
        assert visible_window(total_lines=0, viewport_rows=10, scroll_offset=0) == (0, 0)
        assert visible_window(total_lines=10, viewport_rows=0, scroll_offset=0) == (0, 0)


class TestDisplayLines:
    def test_stdout_lines_have_no_prefix(self):
        runner = _FakeRunner(lines=[OperationLine(stream="stdout", text="hello")])
        assert display_lines(runner) == ["hello"]

    def test_stderr_lines_are_prefixed(self):
        runner = _FakeRunner(lines=[OperationLine(stream="stderr", text="oops")])
        assert display_lines(runner) == ["! oops"]

    def test_mixed_stream_order_is_preserved(self):
        runner = _FakeRunner(
            lines=[
                OperationLine(stream="stdout", text="a"),
                OperationLine(stream="stderr", text="b"),
                OperationLine(stream="stdout", text="c"),
            ]
        )
        assert display_lines(runner) == ["a", "! b", "c"]


class TestOperationScreenStatePoll:
    def test_poll_resets_scroll_offset_when_auto_scrolling(self):
        screen = OperationScreenState(title="Refresh Catalog", runner=_FakeRunner(), scroll_offset=5, auto_scroll=True)
        screen.poll()
        assert screen.scroll_offset == 0

    def test_poll_leaves_scroll_offset_when_auto_scroll_disabled(self):
        screen = OperationScreenState(title="Refresh Catalog", runner=_FakeRunner(), scroll_offset=5, auto_scroll=False)
        screen.poll()
        assert screen.scroll_offset == 5

    def test_succeeded_property_reflects_runner_state(self):
        screen = OperationScreenState(title="x", runner=_FakeRunner(state=OperationState.SUCCEEDED))
        assert screen.succeeded is True
        assert screen.is_finished is True

    def test_failed_state_is_not_succeeded(self):
        screen = OperationScreenState(title="x", runner=_FakeRunner(state=OperationState.FAILED))
        assert screen.succeeded is False
        assert screen.is_finished is True


class TestHandleOperationEvent:
    def _screen(self, *, finished: bool) -> OperationScreenState:
        state = OperationState.SUCCEEDED if finished else OperationState.RUNNING
        return OperationScreenState(title="Refresh Catalog", runner=_FakeRunner(state=state))

    def test_up_scrolls_back_and_disables_auto_scroll(self):
        screen = self._screen(finished=False)
        result = handle_operation_event(InputEvent(action=Action.UP), screen)
        assert result == OPERATION_SCREEN
        assert screen.scroll_offset == 1
        assert screen.auto_scroll is False

    def test_down_scrolls_forward_and_reenables_auto_scroll_at_bottom(self):
        screen = self._screen(finished=False)
        screen.scroll_offset = 1
        screen.auto_scroll = False
        result = handle_operation_event(InputEvent(action=Action.DOWN), screen)
        assert result == OPERATION_SCREEN
        assert screen.scroll_offset == 0
        assert screen.auto_scroll is True

    def test_down_past_zero_does_not_go_negative(self):
        screen = self._screen(finished=False)
        result = handle_operation_event(InputEvent(action=Action.DOWN), screen)
        assert result == OPERATION_SCREEN
        assert screen.scroll_offset == 0

    def test_back_while_running_cancels_and_returns_to_menu(self):
        screen = self._screen(finished=False)
        result = handle_operation_event(InputEvent(action=Action.BACK), screen)
        assert result == MENU_SCREEN
        assert screen.runner.cancelled is True

    def test_confirm_while_running_is_ignored(self):
        screen = self._screen(finished=False)
        result = handle_operation_event(InputEvent(action=Action.CONFIRM), screen)
        assert result == OPERATION_SCREEN

    def test_back_after_finished_returns_to_menu(self):
        screen = self._screen(finished=True)
        result = handle_operation_event(InputEvent(action=Action.BACK), screen)
        assert result == MENU_SCREEN

    def test_confirm_after_finished_returns_to_menu(self):
        screen = self._screen(finished=True)
        result = handle_operation_event(InputEvent(action=Action.CONFIRM), screen)
        assert result == MENU_SCREEN

    def test_menu_action_after_finished_returns_to_menu(self):
        screen = self._screen(finished=True)
        result = handle_operation_event(InputEvent(action=Action.MENU), screen)
        assert result == MENU_SCREEN

    def test_unrelated_action_is_a_no_op(self):
        screen = self._screen(finished=False)
        result = handle_operation_event(InputEvent(action=Action.TEXT_INPUT), screen)
        assert result == OPERATION_SCREEN
        assert screen.scroll_offset == 0

    def test_left_or_right_toggles_technical_details(self):
        screen = self._screen(finished=False)
        handle_operation_event(InputEvent(action=Action.RIGHT), screen)
        assert screen.details_expanded is True
        handle_operation_event(InputEvent(action=Action.LEFT), screen)
        assert screen.details_expanded is False


class TestDeviceAgnosticNavigation:
    """The operation screen must be fully controller/keyboard/touch
    accessible — it never reasons about a raw pygame event itself, only
    the `Action`/`InputEvent` produced by `InputManager` (same as every
    other screen), so these drive real device input through the full
    translation stack rather than constructing an `InputEvent` by hand."""

    def _finished_screen(self) -> OperationScreenState:
        from ports_gfx.operation import OperationState as _State

        @dataclass
        class _FakeRunner:
            state: _State = _State.SUCCEEDED
            error: str = ""

            @property
            def is_finished(self) -> bool:
                return True

        return OperationScreenState(title="Refresh Catalog", runner=_FakeRunner())

    def _running_screen(self) -> OperationScreenState:
        @dataclass
        class _FakeRunner:
            state: OperationState = OperationState.RUNNING

            @property
            def is_finished(self) -> bool:
                return False

        return OperationScreenState(title="Refresh Catalog", runner=_FakeRunner())

    def test_controller_dpad_up_scrolls_back(self):
        pygame = make_fake_pygame(joysticks={0: FakeJoystick(guid="g1")}, controller_indices=frozenset())
        manager = InputManager(pygame, "/opt/romcloud/bin/romcloud")
        manager.handle_event(FakeEvent(type=pygame.JOYDEVICEADDED, device_index=0), screen_w=1920, screen_h=1080)

        raw_up = FakeEvent(type=pygame.JOYHATMOTION, instance_id=1, value=(0, 1))
        ievent = manager.handle_event(raw_up, screen_w=1920, screen_h=1080)
        assert ievent.action == Action.UP

        screen = self._running_screen()
        result = handle_operation_event(ievent, screen)
        assert result == OPERATION_SCREEN
        assert screen.scroll_offset == 1
        assert screen.auto_scroll is False

    def test_keyboard_back_dismisses_when_finished(self):
        pygame = make_fake_pygame()
        manager = InputManager(pygame, "/opt/romcloud/bin/romcloud")

        key_event = FakeEvent(type=pygame.KEYDOWN, key=pygame.K_ESCAPE)
        ievent = manager.handle_event(key_event, screen_w=1920, screen_h=1080)
        assert ievent.action == Action.BACK

        screen = self._finished_screen()
        result = handle_operation_event(ievent, screen)
        assert result == MENU_SCREEN

    def test_touch_tap_dismisses_when_finished(self):
        pygame = make_fake_pygame()
        manager = InputManager(pygame, "/opt/romcloud/bin/romcloud")
        safe_area = compute_layout(1920, 1080, 1).safe_area
        rects = (Rect(x=safe_area.x, y=safe_area.y, w=safe_area.w, h=safe_area.h),)

        tap = FakeEvent(type=pygame.FINGERDOWN, x=0.5, y=0.5)
        ievent = manager.handle_event(tap, screen_w=1920, screen_h=1080, rects=rects, now=1.0)
        assert ievent.action == Action.CONFIRM
        assert ievent.touch_index == 0

        screen = self._finished_screen()
        result = handle_operation_event(ievent, screen)
        assert result == MENU_SCREEN

    def test_touch_tap_is_a_no_op_while_running(self):
        pygame = make_fake_pygame()
        manager = InputManager(pygame, "/opt/romcloud/bin/romcloud")
        safe_area = compute_layout(1920, 1080, 1).safe_area
        rects = (Rect(x=safe_area.x, y=safe_area.y, w=safe_area.w, h=safe_area.h),)

        tap = FakeEvent(type=pygame.FINGERDOWN, x=0.5, y=0.5)
        ievent = manager.handle_event(tap, screen_w=1920, screen_h=1080, rects=rects, now=1.0)

        screen = self._running_screen()
        result = handle_operation_event(ievent, screen)
        assert result == OPERATION_SCREEN
