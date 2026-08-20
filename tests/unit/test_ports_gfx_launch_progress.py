"""Unit tests for `ports_gfx.launch_progress` — pure protocol/state parts
only (no pygame required, mirrors `test_ports_gfx_app.py`'s precedent of
leaving the actual render/event loop untested)."""

from __future__ import annotations

import io

from ports_gfx.launch_progress import (
    LaunchProgressState,
    _is_cancel_input,
    _open_display,
    main,
    parse_event,
    progress_fraction,
    read_events,
)


class TestParseEvent:
    def test_valid_json_object(self):
        assert parse_event('{"phase": "downloading"}') == {"phase": "downloading"}

    def test_blank_line_is_none(self):
        assert parse_event("   ") is None
        assert parse_event("") is None

    def test_malformed_json_is_none(self):
        assert parse_event("not json") is None

    def test_non_object_json_is_none(self):
        assert parse_event("[1, 2, 3]") is None
        assert parse_event("42") is None


class TestLaunchProgressStateApply:
    def test_applies_phase_and_progress_fields_incrementally(self):
        state = LaunchProgressState()
        state.apply({"game_title": "Chrono Trigger", "system": "snes"})
        state.apply({"phase": "downloading", "done": 100, "total": 1000})
        assert state.game_title == "Chrono Trigger"
        assert state.system == "snes"
        assert state.phase == "downloading"
        assert state.bytes_done == 100
        assert state.bytes_total == 1000

    def test_launching_event_sets_flag_without_touching_other_fields(self):
        state = LaunchProgressState()
        state.apply({"phase": "downloading", "done": 5, "total": 10})
        state.apply({"event": "launching"})
        assert state.launching is True
        assert state.bytes_done == 5
        assert state.bytes_total == 10

    def test_error_event_sets_message(self):
        state = LaunchProgressState()
        state.apply({"event": "error", "message": "disk full"})
        assert state.error == "disk full"

    def test_error_event_without_message_defaults(self):
        state = LaunchProgressState()
        state.apply({"event": "error"})
        assert state.error == "unknown error"

    def test_is_finished_transitions(self):
        state = LaunchProgressState()
        assert state.is_finished is False

        state.apply({"event": "launching"})
        assert state.is_finished is True

    def test_is_finished_true_after_error(self):
        state = LaunchProgressState()
        state.apply({"event": "error", "message": "x"})
        assert state.is_finished is True

    def test_is_finished_true_after_cancel_request(self):
        state = LaunchProgressState()
        state.request_cancel()
        assert state.is_finished is True
        assert state.cancelled is True


class TestProgressFraction:
    def test_zero_total_is_zero(self):
        state = LaunchProgressState()
        assert progress_fraction(state) == 0.0

    def test_normal_fraction(self):
        state = LaunchProgressState()
        state.apply({"done": 250, "total": 1000})
        assert progress_fraction(state) == 0.25

    def test_clamped_to_one_if_done_exceeds_total(self):
        state = LaunchProgressState()
        state.apply({"done": 2000, "total": 1000})
        assert progress_fraction(state) == 1.0


class TestCancelInput:
    def test_escape_requests_the_shared_cancel_action(self):
        pygame = type(
            "Pygame",
            (),
            {
                "KEYDOWN": 1,
                "K_ESCAPE": 27,
                "K_q": 113,
                "JOYBUTTONDOWN": 2,
                "CONTROLLERBUTTONDOWN": 3,
            },
        )()
        event = type("Event", (), {"type": pygame.KEYDOWN, "key": pygame.K_ESCAPE})()

        assert _is_cancel_input(pygame, event) is True

    def test_controller_b_requests_the_shared_cancel_action(self):
        pygame = type(
            "Pygame",
            (),
            {
                "KEYDOWN": 1,
                "K_ESCAPE": 27,
                "K_q": 113,
                "JOYBUTTONDOWN": 2,
                "CONTROLLERBUTTONDOWN": 3,
            },
        )()
        for event_type in (pygame.JOYBUTTONDOWN, pygame.CONTROLLERBUTTONDOWN):
            event = type("Event", (), {"type": event_type, "button": 1})()
            assert _is_cancel_input(pygame, event) is True


class TestReadEvents:
    def test_applies_events_until_finished(self):
        stream = io.StringIO(
            '{"phase": "connecting", "game_title": "Test Game", "system": "ps2"}\n'
            '{"phase": "downloading", "done": 10, "total": 100}\n'
            '{"event": "launching"}\n'
        )
        state = LaunchProgressState()
        read_events(stream, state)

        assert state.game_title == "Test Game"
        assert state.bytes_done == 10
        assert state.launching is True

    def test_ignores_malformed_lines(self):
        stream = io.StringIO("not json\n" + '{"phase": "downloading", "done": 1, "total": 10}\n')
        state = LaunchProgressState()
        read_events(stream, state)
        assert state.bytes_done == 1

    def test_stops_reading_once_stream_closes(self):
        stream = io.StringIO("")
        state = LaunchProgressState()
        read_events(stream, state)  # must return promptly, not hang
        assert state.is_finished is False


class TestMainHandlesMissingPygame:
    def test_returns_nonzero_and_prints_clear_message_without_pygame(self, monkeypatch, capsys):
        import builtins

        orig_import = builtins.__import__

        def _blocked(name, globals=None, locals=None, fromlist=(), level=0):
            if name == "pygame":
                raise ImportError("No module named 'pygame'")
            return orig_import(name, globals, locals, fromlist, level)

        monkeypatch.setattr(builtins, "__import__", _blocked)

        exit_code = main(stdin=io.StringIO(""))

        assert exit_code == 1
        captured = capsys.readouterr()
        assert "pygame is not available" in captured.err


class _DisplaySurface:
    def __init__(self, size=(1280, 720)) -> None:
        self._size = size

    def get_size(self):
        return self._size


class TestDisplayModeSelection:
    def test_prefers_borderless_desktop_before_fullscreen(self):
        class Display:
            def __init__(self) -> None:
                self.calls = []

            def set_mode(self, size, flags=None):
                self.calls.append((size, flags))
                return _DisplaySurface(size)

        pygame = type(
            "Pygame",
            (),
            {"NOFRAME": 2, "FULLSCREEN": 1, "display": Display()},
        )()

        surface = _open_display(pygame, 1280, 720)

        assert surface.get_size() == (1280, 720)
        assert pygame.display.calls == [((1280, 720), pygame.NOFRAME)]

    def test_borderless_failure_falls_back_to_exclusive_fullscreen(self):
        class Display:
            def __init__(self) -> None:
                self.calls = []

            def set_mode(self, size, flags=None):
                self.calls.append((size, flags))
                if flags == 2:
                    raise RuntimeError("borderless unavailable")
                return _DisplaySurface(size)

        pygame = type(
            "Pygame",
            (),
            {"NOFRAME": 2, "FULLSCREEN": 1, "display": Display()},
        )()

        surface = _open_display(pygame, 1280, 720)

        assert surface.get_size() == (1280, 720)
        assert pygame.display.calls == [((1280, 720), 2), ((1280, 720), 1)]
