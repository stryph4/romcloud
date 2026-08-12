"""Unit tests for the pygame-free parts of `ports_gfx.app`.

`ports_gfx.app` defers its `import pygame` to inside `run_app`/`_run`, so
importing the module itself — and exercising `MENU_ITEMS` / `format_result`
— never requires pygame to be installed. The actual render/event loop
(`_run`, `_render`) needs a real display and is not covered here, mirroring
how `romcloud.ui.progress`/`romcloud.ui.maintenance` leave their curses
render loops untested.
"""

from __future__ import annotations

from types import SimpleNamespace

from ports_gfx.app import (
    MENU_CATEGORIES,
    MENU_ITEMS,
    _ControllerTestScreenState,
    _apply_direction,
    _handle_controller_test_event,
    _library_sync_body_lines,
    _handle_menu_event,
    _load_startup_backend_state,
    _open_display,
    _render_menu,
    _wizard_body_lines,
    classify_message_kind,
    format_result,
    initial_screen_for_status,
    menu_categories_for_mode,
    menu_categories_for_state,
    root_menu_items_for_state,
    completed_mode_transition_requires_exit,
    operation_summary_message,
    request_relaunch_for_completed_update,
    render_completed_mode_transition_exit,
    render_completed_update_relaunch,
    start_operation,
)
from ports_gfx.actions import Action
from ports_gfx.activity import ActivityLog
from ports_gfx.client import BackendResult
from ports_gfx.input_manager import InputEvent
from ports_gfx.layout import compute_layout
from ports_gfx.library_sync_screen import PREFLIGHT, LibrarySyncScreenState
from ports_gfx.menu import CONTROLLER_TEST_ACTION, EXIT_ACTION, MenuItem, MenuState
from ports_gfx.operation import OperationLine, OperationState
from ports_gfx.operation_screen import OPERATION_SCREEN
from ports_gfx.relaunch import GuiRelaunchCoordinator
from ports_gfx.update_state import UpdateCheckState
from ports_gfx.wizard import WizardState, WizardStep


class TestMenuItems:
    def test_render_uses_layout_rect_center_for_described_item(self):
        class RenderedText:
            def __init__(self) -> None:
                self.centers: list[tuple[float, float]] = []

            def get_rect(self, *, center):  # noqa: ANN001
                self.centers.append(center)
                return SimpleNamespace(bottom=center[1] + 10)

        rendered: list[RenderedText] = []

        class Font:
            def render(self, *_args):  # noqa: ANN002
                text = RenderedText()
                rendered.append(text)
                return text

        screen = SimpleNamespace(fill=lambda _color: None, blit=lambda *_args: None)
        pygame = SimpleNamespace(
            draw=SimpleNamespace(rect=lambda *_args, **_kwargs: None),
            display=SimpleNamespace(flip=lambda: None),
        )
        layout = compute_layout(800, 600, 1)
        state = MenuState([MenuItem("Direct", "connected", "Use configured storage")])

        _render_menu(
            pygame,
            screen,
            {"title": Font(), "body": Font(), "hint": Font()},
            layout,
            state,
            None,
            "info",
            ActivityLog(),
            UpdateCheckState(),
        )

        center_x, center_y = layout.card_rects[0].center
        assert rendered[1].centers == [
            (center_x, center_y - layout.fonts.hint // 2)
        ]
        assert rendered[2].centers[0][0] == center_x

    def test_contains_expected_actions_in_order(self):
        from ports_gfx import app as app_module

        actions = [item.action for item in MENU_ITEMS]
        assert actions == [
            app_module.SETUP_ACTION,
            "connection-status",
            "connection-mount",
            "connection-unmount",
            "status",
            "refresh",
            "healthcheck",
            "cache-status",
            app_module.SAVESYNC_ACTION,
            "update-check",
            CONTROLLER_TEST_ACTION,
            EXIT_ACTION,
        ]

    def test_exit_is_the_last_item(self):
        assert MENU_ITEMS[-1].action == EXIT_ACTION

    def test_savesync_is_first_class_top_level_entry(self):
        from ports_gfx import app as app_module

        savesync_items = [item for item in MENU_ITEMS if item.action == app_module.SAVESYNC_ACTION]
        assert len(savesync_items) == 1
        assert savesync_items[0].label == "SaveSync"

    def test_all_three_modes_are_available_from_one_authoritative_state(self):
        direct = menu_categories_for_mode("direct_nas")
        assert any(
            item.action == "cache-status"
            for items in direct.values()
            for item in items
        )
        assert all(
            not item.action.startswith("library-")
            for items in direct.values()
            for item in items
        )
        direct_roots = root_menu_items_for_state(
            {"game_access_mode": "direct_nas", "operating_mode": "connected",
             "offline_mode": False, "capabilities": {}}
        )
        assert [item.label for item in direct_roots if item.label in {"Direct", "Cached Storage", "Offline"}] == [
            "Direct", "Cached Storage", "Offline"
        ]
        assert next(item for item in direct_roots if item.label == "Direct").active

    def test_mode_control_is_prominent_and_exit_is_top_level(self):
        cache_state = {
            "game_access_mode": "smart_cache", "operating_mode": "cache", "offline_mode": False,
            "capabilities": {},
        }
        offline_state = {
            "game_access_mode": "smart_cache", "operating_mode": "offline", "offline_mode": True,
            "offline_mode_supported": True,
            "capabilities": {"catalog_refresh": False, "library_sync": False,
                             "save_sync": False, "update_network": False,
                             "remote_validation": False},
        }

        cache_roots = root_menu_items_for_state(cache_state)
        offline_roots = root_menu_items_for_state(offline_state)
        assert [item.label for item in cache_roots[1:4]] == [
            "Direct", "Cached Storage", "Offline"
        ]
        assert not cache_roots[1].active and cache_roots[2].active and not cache_roots[3].active
        assert [item.label for item in offline_roots[1:4]] == [
            "Direct", "Cached Storage", "Offline"
        ]
        assert not offline_roots[1].active and not offline_roots[2].active and offline_roots[3].active
        assert all(
            not item.action.startswith("library-")
            for item in menu_categories_for_state(offline_state, True)["Library"]
        )
        assert [item.label for item in offline_roots] == [
            "Library", "Direct", "Cached Storage", "Offline",
            "Storage", "Settings", "Exit"
        ]
        assert all(item.action != EXIT_ACTION for item in MENU_CATEGORIES["Settings"])
        assert offline_roots[-1].action == EXIT_ACTION
        assert all(
            item.action != "setup"
            for item in menu_categories_for_state(offline_state)["Storage"]
        )

    def test_library_sync_action_is_visible_only_when_enabled(self):
        disabled = menu_categories_for_mode("smart_cache", False, False)["Library"]
        enabled = menu_categories_for_mode("smart_cache", False, True)["Library"]

        assert all(item.action != "library-sync" for item in disabled)
        assert enabled[-1].action == "library-sync"
        assert enabled[-1].label == "Import Source Metadata"

    def test_metadata_import_preflight_copy_shows_cost_without_fake_estimate(self):
        screen = LibrarySyncScreenState(
            "romcloud",
            step=PREFLIGHT,
            preview={
                "games_eligible": 18200,
                "systems": ["ps2", "snes"],
                "gamelist_files": 2,
                "gamelist_bytes": 4096,
                "artwork_references": 8241,
                "video_references": 240,
                "other_media_references": 10,
                "duration_note": "Duration depends on library size and storage/network speed.",
            },
        )

        lines = _library_sync_body_lines(screen)

        assert "Eligible catalog games: 18,200" in lines
        assert "Artwork references: 8,241" in lines
        assert "Video references: 240" in lines
        assert "Transfer bytes: counted only as files are copied" in lines
        assert any("storage/network speed" in line for line in lines)
        assert any("hold for 3 seconds" in line for line in lines)

    def test_metadata_import_result_distinguishes_skip_hash_copy_and_bytes(self):
        screen = LibrarySyncScreenState(
            "romcloud",
            step="result",
            result={
                "media_examined": 20,
                "media_skipped": 18,
                "media_hashed": 3,
                "media_bytes_hashed": 2048,
                "media_transferred": 1,
                "media_bytes_transferred": 1024,
            },
        )

        lines = _library_sync_body_lines(screen)

        assert "Media examined: 20" in lines
        assert "Media skipped unchanged: 18" in lines
        assert "Full-file hashes: 3" in lines
        assert "Bytes fully hashed: 2.0 KB" in lines
        assert "Media copied: 1" in lines
        assert "Actual bytes transferred: 1.0 KB" in lines


class TestInitialScreen:
    def test_fresh_install_opens_wizard(self):
        status = BackendResult(ok=True, data={"state": "fresh"})
        assert initial_screen_for_status(status) == "wizard"

    def test_configured_install_opens_unchanged_dashboard(self):
        status = BackendResult(ok=True, data={"state": "configured"})
        assert initial_screen_for_status(status) == "menu"

    def test_partial_or_broken_install_opens_repair_wizard(self):
        partial = BackendResult(ok=True, data={"state": "partial"})
        assert initial_screen_for_status(partial) == "wizard"

    def test_failed_backend_call_never_opens_wizard(self):
        """A subprocess timeout/error (e.g. the backend is still starting up
        right after a suspend/resume) must not be treated as "unconfigured"
        — this is the real-hardware bug: a Steam Deck resume transiently
        failing the startup `setup-status` call must not route an already
        configured installation into the first-run wizard."""
        failed = BackendResult(ok=False, error="no output from romcloud (exit 1)")
        timed_out = BackendResult(ok=False, error="Command timed out after 20 seconds")
        assert initial_screen_for_status(failed) == "menu"
        assert initial_screen_for_status(timed_out) == "menu"

    def test_unknown_or_missing_state_field_never_opens_wizard(self):
        """Only an explicit "fresh"/"partial" state may open the wizard —
        an otherwise-ok response with an unexpected/missing state must not
        default to it either."""
        assert initial_screen_for_status(BackendResult(ok=True, data={})) == "menu"
        assert (
            initial_screen_for_status(BackendResult(ok=True, data={"state": "mounted"}))
            == "menu"
        )


class _RecordingSplash:
    def __init__(self, order: list[object]) -> None:
        self.order = order

    def render(self, title: str, status: str, progress: float) -> None:
        self.order.append(("splash", title, status, progress))


class TestStartupSplash:
    def test_initial_frame_is_rendered_before_blocking_setup_status(self, monkeypatch):
        from ports_gfx import app as app_module

        order: list[object] = []

        def backend(_binary: str, action: str):
            order.append(("backend", action))
            return BackendResult(ok=True, data={"state": "fresh"})

        monkeypatch.setattr(app_module, "call_backend", backend)
        _load_startup_backend_state(_RecordingSplash(order), "/opt/romcloud/bin/romcloud")

        assert order[0] == ("splash", "Starting ROMCloud…", "Display ready", 0.12)
        assert order.index(("backend", "setup-status")) > order.index(
            ("splash", "Starting ROMCloud…", "Loading setup and configuration…", 0.25)
        )

    def test_unconfigured_startup_uses_setup_message_and_skips_source_check(self, monkeypatch):
        from ports_gfx import app as app_module

        order: list[object] = []
        monkeypatch.setattr(
            app_module,
            "call_backend",
            lambda _binary, action: BackendResult(ok=True, data={"state": "fresh"}),
        )

        _load_startup_backend_state(_RecordingSplash(order), "romcloud")

        statuses = [entry[2] for entry in order if entry[0] == "splash"]
        assert "Preparing setup…" in statuses
        assert "Checking source availability…" not in statuses

    def test_configured_startup_checks_source_after_status_message(self, monkeypatch):
        from ports_gfx import app as app_module

        order: list[object] = []

        def backend(_binary: str, action: str):
            order.append(("backend", action))
            if action == "setup-status":
                return BackendResult(ok=True, data={"state": "configured"})
            return BackendResult(ok=True, data={"state": "mounted"})

        monkeypatch.setattr(app_module, "call_backend", backend)
        _load_startup_backend_state(_RecordingSplash(order), "romcloud")

        source_frame = ("splash", "Starting ROMCloud…", "Checking source availability…", 0.40)
        assert order.index(source_frame) < order.index(("backend", "connection-status"))


class _DisplaySurface:
    def __init__(self, size=(1280, 720)) -> None:
        self._size = size

    def get_size(self):
        return self._size


class _DisplayDiagnostics:
    def __init__(self) -> None:
        self.events = []

    def environment(self):
        return {"SDL_VIDEODRIVER": "x11", "DISPLAY": ":0"}

    def record(self, event, **fields):
        self.events.append((event, fields))


def test_borderless_desktop_is_selected_without_exclusive_fullscreen():
    class Display:
        def __init__(self) -> None:
            self.calls = []

        def set_mode(self, size, flags=None):
            self.calls.append((size, flags))
            return _DisplaySurface(size)

        def Info(self):  # noqa: N802 - pygame API spelling
            return type("Info", (), {"current_w": 1280, "current_h": 720})()

    pygame = type(
        "Pygame",
        (),
        {"NOFRAME": 2, "FULLSCREEN": 1, "display": Display()},
    )()

    diagnostics = _DisplayDiagnostics()
    surface = _open_display(pygame, 1280, 720, diagnostics)

    assert surface.get_size() == (1280, 720)
    assert pygame.display.calls == [((1280, 720), pygame.NOFRAME)]
    assert diagnostics.events[-1] == (
        "display_open_after",
        {
            "selected_path": "borderless-desktop",
            "selected_flags": pygame.NOFRAME,
            "surface_size": [1280, 720],
            "display_info_size": [1280, 720],
        },
    )


def test_borderless_failure_falls_back_to_exclusive_fullscreen(caplog):
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
    assert "borderless unavailable" in caplog.text


def test_exclusive_failure_is_logged_before_windowed_fallback(caplog):
    class Display:
        def __init__(self) -> None:
            self.calls = []

        def set_mode(self, size, flags=None):
            self.calls.append((size, flags))
            if flags is not None:
                raise RuntimeError(f"flags {flags} unavailable")
            return _DisplaySurface(size)

    pygame = type(
        "Pygame",
        (),
        {"NOFRAME": 2, "FULLSCREEN": 1, "display": Display()},
    )()

    surface = _open_display(pygame, 1280, 720)

    assert surface.get_size() == (1280, 720)
    assert pygame.display.calls == [
        ((1280, 720), 2),
        ((1280, 720), 1),
        ((1280, 720), None),
    ]
    assert "flags 1 unavailable" in caplog.text


class TestWizardValidationPresentation:
    def test_every_step_renders_its_primary_and_secondary_context(self):
        wizard = WizardState()
        for step in WizardStep:
            wizard.step = step
            lines = _wizard_body_lines(wizard)
            primary, secondary = wizard.context_lines
            assert primary in lines
            assert secondary in lines

    def test_review_labels_source_read_only_and_remote_read_write(self):
        wizard = WizardState()
        wizard.step = WizardStep.REVIEW
        wizard.server = "omnivault"
        wizard.share = "Roms"
        wizard.remote_data_type = "smb"
        wizard.remote_server = "omnivault"
        wizard.remote_share = "ROMCloud"
        wizard.remote_validation = {"connected": True, "read_verified": True}

        lines = _wizard_body_lines(wizard)

        assert "ROM library: //omnivault/Roms [Read only]" in lines
        assert "ROMCloud data: //omnivault/ROMCloud [Read/write]" in lines
        assert any("Write and cleanup will be verified" in line for line in lines)

    def test_done_shows_all_successful_probe_stages(self):
        wizard = WizardState()
        wizard.step = WizardStep.DONE
        wizard.remote_data_type = "smb"
        wizard.remote_server = "omnivault"
        wizard.remote_share = "ROMCloud"
        wizard.applied_summary = {
            "source_validation": {"connected": True, "read_verified": True},
            "remote_data_validation": {
                "connected": True,
                "read_verified": True,
                "write_verified": True,
                "cleanup_verified": True,
            },
        }

        lines = _wizard_body_lines(wizard)

        assert "\u2713 Write access verified" in lines
        assert "\u2713 Cleanup verified" in lines


class TestFormatResult:
    def test_success_includes_action_and_data(self):
        result = BackendResult(ok=True, data={"ok": True, "games_total": 5})
        line = format_result("status", result)
        assert line.startswith("status:")
        assert "games=5" in line
        assert "cached=0" in line
        assert "pinned=0" in line

    def test_failure_shows_error_message(self):
        result = BackendResult(ok=False, error="connection refused")
        line = format_result("healthcheck", result)
        assert line == "Error: connection refused"

    def test_status_result_formats_source_summary(self):
        result = BackendResult(
            ok=True,
            data={
                "source_type": "SMB",
                "source_description": "nas.local:ROMs",
                "games_total": 12,
                "cached": 3,
                "pinned": 1,
            },
        )
        line = format_result("status", result)
        assert "SMB" in line
        assert "nas.local:ROMs" in line
        assert "games=12" in line
        assert "cached=3" in line
        assert "pinned=1" in line

    def test_status_reports_cached_only_presentation(self):
        result = BackendResult(
            ok=True,
            data={
                "games_total": 12,
                "cached": 3,
                "pinned": 1,
                "offline_library_mode": True,
            },
        )

        assert "Offline" in format_result("status", result)

    def test_healthcheck_result_formats_source_summary(self):
        result = BackendResult(
            ok=True,
            data={
                "source_type": "Local filesystem",
                "source_description": "/userdata/roms",
                "source_reachable": True,
            },
        )
        line = format_result("healthcheck", result)
        assert "Local filesystem" in line
        assert "/userdata/roms" in line
        assert "reachable" in line

    def test_healthcheck_includes_remote_data_writability(self):
        result = BackendResult(
            ok=True,
            data={
                "source_type": "SMB",
                "source_description": "rom-nas:ROMs",
                "source_reachable": True,
                "remote_data_configured": True,
                "remote_data_reachable": False,
            },
        )

        line = format_result("healthcheck", result)

        assert "ROMCloud data: unreachable/read-only" in line


class TestClassifyMessageKind:
    def test_failed_call_is_error(self):
        result = BackendResult(ok=False, error="boom")
        assert classify_message_kind("status", result) == "error"

    def test_successful_status_call_is_success(self):
        result = BackendResult(ok=True, data={"ok": True, "games_total": 3})
        assert classify_message_kind("status", result) == "success"

    def test_healthcheck_unreachable_source_is_warning_not_error(self):
        result = BackendResult(ok=True, data={"ok": True, "source_reachable": False})
        assert classify_message_kind("healthcheck", result) == "warning"

    def test_healthcheck_reachable_source_is_success(self):
        result = BackendResult(ok=True, data={"ok": True, "source_reachable": True})
        assert classify_message_kind("healthcheck", result) == "success"

    def test_healthcheck_unwritable_remote_data_is_warning(self):
        result = BackendResult(
            ok=True,
            data={
                "source_reachable": True,
                "remote_data_configured": True,
                "remote_data_reachable": False,
            },
        )
        assert classify_message_kind("healthcheck", result) == "warning"


class TestRunAppHandlesMissingPygame:
    def test_returns_nonzero_and_prints_clear_message_without_pygame(self, monkeypatch, capsys):
        import builtins

        import ports_gfx.app as app_module

        orig_import = builtins.__import__

        def _blocked(name, globals=None, locals=None, fromlist=(), level=0):
            if name == "pygame":
                raise ImportError("No module named 'pygame'")
            return orig_import(name, globals, locals, fromlist, level)

        monkeypatch.setattr(builtins, "__import__", _blocked)

        exit_code = app_module.run_app("/opt/romcloud/bin/romcloud")

        assert exit_code == 1
        captured = capsys.readouterr()
        assert "pygame is not available" in captured.err


class TestApplyDirection:
    def test_moves_selection_toward_next_widget(self):
        state = MenuState(list(MENU_ITEMS))
        layout = compute_layout(1920, 1080, len(state.items))
        _apply_direction(state, layout, Action.RIGHT)
        assert state.selected_index == 1

    def test_non_directional_action_is_a_no_op(self):
        state = MenuState(list(MENU_ITEMS))
        layout = compute_layout(1920, 1080, len(state.items))
        before = state.selected_index
        _apply_direction(state, layout, Action.CONFIRM)
        assert state.selected_index == before


class TestHandleMenuEvent:
    def _state(self):
        return MenuState(list(MENU_ITEMS))

    def _layout(self, state):
        return compute_layout(1920, 1080, len(state.items))

    def test_confirm_on_exit_item_stops_running(self):
        state = self._state()
        layout = self._layout(state)
        exit_index = next(i for i, item in enumerate(state.items) if item.action == EXIT_ACTION)
        state.select(exit_index)

        running, screen, message, kind, operation = _handle_menu_event(
            InputEvent(action=Action.CONFIRM), state, layout, "/opt/romcloud/bin/romcloud", True, None, "info",
        )
        assert running is False
        assert screen == "menu"
        assert operation is None

    def test_confirm_on_controller_test_item_switches_screen(self):
        state = self._state()
        layout = self._layout(state)
        idx = next(i for i, item in enumerate(state.items) if item.action == CONTROLLER_TEST_ACTION)
        state.select(idx)

        running, screen, message, kind, operation = _handle_menu_event(
            InputEvent(action=Action.CONFIRM), state, layout, "/opt/romcloud/bin/romcloud", True, None, "info",
        )
        assert running is True
        assert screen == "controller_test"
        assert operation is None

    def test_back_action_quits_the_app(self):
        state = self._state()
        layout = self._layout(state)
        running, screen, message, kind, operation = _handle_menu_event(
            InputEvent(action=Action.BACK), state, layout, "/opt/romcloud/bin/romcloud", True, None, "info",
        )
        assert running is False
        assert operation is None

    def test_directional_action_moves_selection_via_layout(self):
        state = self._state()
        layout = self._layout(state)
        running, screen, message, kind, operation = _handle_menu_event(
            InputEvent(action=Action.RIGHT), state, layout, "/opt/romcloud/bin/romcloud", True, None, "info",
        )
        assert running is True
        assert screen == "menu"
        assert message is None
        assert state.selected_index == 1
        assert operation is None

    def test_touch_index_focuses_before_dispatch(self):
        state = self._state()
        layout = self._layout(state)
        exit_index = next(i for i, item in enumerate(state.items) if item.action == EXIT_ACTION)

        running, screen, message, kind, operation = _handle_menu_event(
            InputEvent(action=Action.CONFIRM, touch_index=exit_index),
            state, layout, "/opt/romcloud/bin/romcloud", True, None, "info",
        )
        assert state.selected_index == exit_index
        assert running is False
        assert operation is None

    def test_confirm_on_refresh_item_starts_an_operation_and_switches_screen(self, monkeypatch):
        import sys

        from ports_gfx import app as app_module

        state = self._state()
        layout = self._layout(state)
        refresh_index = next(i for i, item in enumerate(state.items) if item.action == "refresh")
        state.select(refresh_index)

        def fake_popen(argv, **kwargs):
            import subprocess

            return subprocess.Popen(
                [sys.executable, "-c", "pass"],
                stdout=kwargs.get("stdout"),
                stderr=kwargs.get("stderr"),
                text=True,
            )

        def fake_start_operation(action, romcloud_bin):
            spec = app_module._OPERATIONS[action]
            runner = app_module.OperationRunner([romcloud_bin, *spec.args], popen=fake_popen)
            runner.start()
            return app_module.OperationScreenState(title=spec.title, runner=runner)

        monkeypatch.setattr(app_module, "start_operation", fake_start_operation)

        running, screen, message, kind, operation = app_module._handle_menu_event(
            InputEvent(action=Action.CONFIRM), state, layout, "/opt/romcloud/bin/romcloud", True, None, "info",
        )

        assert running is True
        assert screen == OPERATION_SCREEN
        assert operation is not None

    def test_duplicate_mode_confirm_keeps_running_transition_without_spawning(
        self, monkeypatch
    ):
        from ports_gfx import app as app_module
        from ports_gfx.operation_screen import OperationScreenState

        state = MenuState([MenuItem("Cached Storage", "library-cache")])
        layout = self._layout(state)
        active = OperationScreenState(
            title="Cached Storage",
            runner=SimpleNamespace(
                state=OperationState.RUNNING,
                is_finished=False,
            ),
        )
        monkeypatch.setattr(
            app_module,
            "start_operation",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                AssertionError("duplicate worker spawned")
            ),
        )

        running, screen, message, kind, operation = app_module._handle_menu_event(
            InputEvent(action=Action.CONFIRM),
            state,
            layout,
            "/opt/romcloud/bin/romcloud",
            True,
            None,
            "info",
            active,
        )

        assert running is True
        assert screen == OPERATION_SCREEN
        assert operation is active


class TestHandleSavesyncEvent:
    def _screen(self, **kwargs):
        from ports_gfx.savesync_screen import SaveSyncScreenState

        return SaveSyncScreenState(romcloud_bin="/opt/romcloud/bin/romcloud", **kwargs)

    def test_dashboard_directional_navigation(self):
        from ports_gfx import app as app_module

        screen = self._screen()
        result = app_module._handle_savesync_event(InputEvent(action=Action.DOWN), screen)
        assert result == "savesync"
        assert screen.selected_index == 1

    def test_dashboard_back_leaves_screen(self):
        from ports_gfx import app as app_module

        screen = self._screen()
        assert app_module._handle_savesync_event(InputEvent(action=Action.BACK), screen) == "menu"

    def test_dashboard_confirm_on_back_item_leaves_screen(self):
        from ports_gfx import app as app_module

        screen = self._screen(selected_index=3)  # "Back"
        assert app_module._handle_savesync_event(InputEvent(action=Action.CONFIRM), screen) == "menu"

    def test_dashboard_confirm_on_settings_switches_step(self):
        from ports_gfx import app as app_module
        from ports_gfx.savesync_screen import SETTINGS

        screen = self._screen(selected_index=2)
        assert app_module._handle_savesync_event(InputEvent(action=Action.CONFIRM), screen) == "savesync"
        assert screen.step == SETTINGS

    def test_preview_confirm_begins_hold(self):
        from ports_gfx import app as app_module
        from ports_gfx.savesync_screen import CONFIRMING, PREVIEW

        screen = self._screen(step=PREVIEW)
        app_module._handle_savesync_event(InputEvent(action=Action.CONFIRM), screen)
        assert screen.step == CONFIRMING
        assert screen.confirm.pressed is True

    def test_confirming_uses_visual_progress_without_percentage_text(self):
        from ports_gfx import app as app_module
        from ports_gfx.savesync_screen import CONFIRMING

        screen = self._screen(step=CONFIRMING)
        screen.confirm.press()
        screen.confirm.update(1.0)

        lines = app_module._savesync_body_lines(screen)

        assert not any("%" in line for line in lines)

    def test_preview_back_returns_to_dashboard(self):
        from ports_gfx import app as app_module
        from ports_gfx.savesync_screen import DASHBOARD, PREVIEW

        screen = self._screen(step=PREVIEW)
        app_module._handle_savesync_event(InputEvent(action=Action.BACK), screen)
        assert screen.step == DASHBOARD

    def test_confirming_forwards_events_to_hold_state(self):
        from ports_gfx import app as app_module
        from ports_gfx.savesync_screen import CONFIRMING

        screen = self._screen(step=CONFIRMING)
        app_module._handle_savesync_event(InputEvent(action=Action.CONFIRM), screen)
        assert screen.confirm.pressed is True

    def test_rpc3_warning_requires_confirm_before_long_hold(self):
        from ports_gfx import app as app_module
        from ports_gfx.savesync_screen import RPCS3_CONFIRMING, RPCS3_WARNING

        screen = self._screen(step=RPCS3_WARNING)
        app_module._handle_savesync_event(InputEvent(action=Action.CONFIRM), screen)

        assert screen.step == RPCS3_CONFIRMING
        assert screen.confirm.pressed is True

    def test_local_game_warning_confirm_starts_settings_update(self):
        from ports_gfx import app as app_module
        from ports_gfx.savesync_screen import APPLYING_SETTINGS, LOCAL_GAMES_WARNING

        screen = self._screen(step=LOCAL_GAMES_WARNING)
        called = []

        def enable(value):
            called.append(value)
            screen.step = APPLYING_SETTINGS

        screen.set_include_local_games = enable
        app_module._handle_savesync_event(InputEvent(action=Action.CONFIRM), screen)

        assert screen.step == APPLYING_SETTINGS
        assert called == [True]

    def test_result_confirm_returns_to_dashboard(self):
        from ports_gfx import app as app_module
        from ports_gfx.savesync_screen import DASHBOARD, RESULT

        screen = self._screen(step=RESULT)
        app_module._handle_savesync_event(InputEvent(action=Action.CONFIRM), screen)
        assert screen.step == DASHBOARD

    def test_previewing_ignores_input(self):
        from ports_gfx import app as app_module
        from ports_gfx.savesync_screen import PREVIEWING

        screen = self._screen(step=PREVIEWING)
        result = app_module._handle_savesync_event(InputEvent(action=Action.BACK), screen)
        assert result == "savesync"
        assert screen.step == PREVIEWING


class TestHandleControllerTestEvent:
    def test_directional_action_moves_slot_selection(self):
        controller_test = _ControllerTestScreenState()
        from ports_gfx.input_manager import InputManager
        from tests.unit._pygame_fakes import make_fake_pygame

        manager = InputManager(make_fake_pygame(), "/opt/romcloud/bin/romcloud")
        before = controller_test.selected_index
        screen = _handle_controller_test_event(InputEvent(action=Action.DOWN), controller_test, manager)
        assert screen == "controller_test"
        assert controller_test.selected_index != before

    def test_back_returns_to_menu_and_cancels_remap(self):
        controller_test = _ControllerTestScreenState()
        from ports_gfx.input_manager import InputManager
        from tests.unit._pygame_fakes import make_fake_pygame

        manager = InputManager(make_fake_pygame(), "/opt/romcloud/bin/romcloud")
        controller_test.remap_instance_id = 1
        screen = _handle_controller_test_event(InputEvent(action=Action.BACK), controller_test, manager)
        assert screen == "menu"

    def test_confirm_with_no_controller_connected_is_a_no_op(self):
        controller_test = _ControllerTestScreenState()
        from ports_gfx.input_manager import InputManager
        from tests.unit._pygame_fakes import make_fake_pygame

        manager = InputManager(make_fake_pygame(), "/opt/romcloud/bin/romcloud")
        screen = _handle_controller_test_event(InputEvent(action=Action.CONFIRM), controller_test, manager)
        assert screen == "controller_test"
        assert controller_test.remap_instance_id is None


class TestStartOperation:
    def test_builds_argv_from_spec_and_starts_the_runner(self):
        import subprocess
        import sys

        captured = {}

        def fake_popen(argv, **kwargs):
            captured["argv"] = argv
            return subprocess.Popen(
                [sys.executable, "-c", "pass"], stdout=kwargs.get("stdout"), stderr=kwargs.get("stderr"), text=True,
            )

        operation = start_operation("refresh", "/opt/romcloud/bin/romcloud", popen=fake_popen)

        assert operation.title == "Refresh Catalog"
        assert captured["argv"] == [
            "/opt/romcloud/bin/romcloud",
            "uidata",
            "refresh-progress",
        ]
        assert operation.runner.state in (OperationState.RUNNING, OperationState.SUCCEEDED)

    def test_unknown_action_raises_key_error(self):
        import pytest

        with pytest.raises(KeyError):
            start_operation("not-a-real-operation", "/opt/romcloud/bin/romcloud")


class _FakeFinishedRunner:
    def __init__(self, state: OperationState, error: str = "") -> None:
        self.state = state
        self.error = error


class _FakeUpdateRunner:
    def __init__(
        self,
        state: OperationState,
        lines: list[OperationLine],
        *,
        finished: bool = True,
        error: str = "",
    ) -> None:
        self.state = state
        self.lines = lines
        self.is_finished = finished
        self.error = error


class TestUpdateRelaunchRequest:
    def _operation(
        self,
        state: OperationState,
        lines: list[OperationLine],
        *,
        finished: bool = True,
    ):
        from ports_gfx.operation_screen import OperationScreenState

        runner = _FakeUpdateRunner(state, lines, finished=finished)
        return OperationScreenState(
            title="Update ROMCloud", runner=runner, arms_gui_relaunch=True
        )

    def test_successful_final_result_enters_terminal_relaunch_state(self):
        coordinator = GuiRelaunchCoordinator("/opt/romcloud/bin/romcloud")
        operation = self._operation(
            OperationState.SUCCEEDED,
            [
                OperationLine(
                    "stderr",
                    '@romcloud-progress {"stage":"completed","status":"success"}',
                ),
                OperationLine("stdout", '{"ok":true,"restart_required":true}'),
            ],
        )

        requested = request_relaunch_for_completed_update(operation, coordinator)

        assert requested is True
        assert coordinator.progress_complete is True
        assert coordinator.terminal is True
        assert coordinator.relaunch_pending is True

    def test_running_update_does_not_relaunch_even_with_partial_output(self):
        coordinator = GuiRelaunchCoordinator("/opt/romcloud/bin/romcloud")
        operation = self._operation(
            OperationState.RUNNING,
            [OperationLine("stdout", '{"ok":true}')],
            finished=False,
        )

        assert request_relaunch_for_completed_update(operation, coordinator) is False
        assert coordinator.terminal is False

    def test_failed_update_does_not_relaunch(self):
        coordinator = GuiRelaunchCoordinator("/opt/romcloud/bin/romcloud")
        operation = self._operation(
            OperationState.FAILED,
            [OperationLine("stdout", '{"ok":false,"error":"install failed"}')],
        )

        assert request_relaunch_for_completed_update(operation, coordinator) is False
        assert coordinator.terminal is False
        assert coordinator.relaunch_pending is False

    def test_zero_exit_without_success_json_does_not_relaunch(self):
        coordinator = GuiRelaunchCoordinator("/opt/romcloud/bin/romcloud")
        operation = self._operation(
            OperationState.SUCCEEDED,
            [OperationLine("stdout", "not json")],
        )

        assert request_relaunch_for_completed_update(operation, coordinator) is False
        assert coordinator.terminal is False

    def test_success_renders_restart_splash_once(self):
        coordinator = GuiRelaunchCoordinator("/opt/romcloud/bin/romcloud")
        operation = self._operation(
            OperationState.SUCCEEDED,
            [OperationLine("stdout", '{"ok":true,"restart_required":true}')],
        )
        frames: list[object] = []
        splash = _RecordingSplash(frames)

        assert render_completed_update_relaunch(operation, coordinator, splash) is True
        assert render_completed_update_relaunch(operation, coordinator, splash) is False
        assert frames == [("splash", "Update complete", "Restarting ROMCloud…", 1.0)]

    def test_failure_never_renders_successful_restart_splash(self):
        coordinator = GuiRelaunchCoordinator("/opt/romcloud/bin/romcloud")
        operation = self._operation(
            OperationState.FAILED,
            [OperationLine("stdout", '{"ok":false,"error":"install failed"}')],
        )
        frames: list[object] = []

        assert render_completed_update_relaunch(
            operation, coordinator, _RecordingSplash(frames)
        ) is False
        assert frames == []
        assert coordinator.relaunch_pending is False

    def test_only_update_install_spec_arms_gui_relaunch(self):
        """Explicit-ownership regression: every non-self-update operation
        (mode transitions, catalog refresh, mount/reconnect, update-check)
        must never be able to arm the terminal GUI-relaunch handoff."""
        from ports_gfx.app import _OPERATIONS

        armed = {action for action, spec in _OPERATIONS.items() if spec.arms_gui_relaunch}
        assert armed == {"update-install"}

    def test_only_mode_operations_own_normal_terminal_exit(self):
        from ports_gfx.app import _OPERATIONS

        terminal = {
            action
            for action, spec in _OPERATIONS.items()
            if spec.exits_after_mode_change
        }
        assert terminal == {
            "library-connected",
            "library-cache",
            "library-offline",
        }
        assert all(
            not (spec.arms_gui_relaunch and spec.exits_after_mode_change)
            for spec in _OPERATIONS.values()
        )

    def test_finished_succeeded_non_update_operation_never_arms_relaunch(self):
        """Real-hardware regression: a completed Cached Storage/Direct/Offline
        transition (or any other operation) must never relaunch the
        GUI, even though it is finished and succeeded — only the explicit
        ``arms_gui_relaunch`` flag may authorize that, never the title or
        the fact that the operation succeeded."""
        from ports_gfx.operation_screen import OperationScreenState

        coordinator = GuiRelaunchCoordinator("/opt/romcloud/bin/romcloud")
        for title in ("Cached Storage", "Direct", "Offline", "Refresh Catalog"):
            runner = _FakeUpdateRunner(
                OperationState.SUCCEEDED,
                [OperationLine("stdout", '{"ok":true}')],
            )
            operation = OperationScreenState(
                title=title, runner=runner, arms_gui_relaunch=False
            )
            assert request_relaunch_for_completed_update(operation, coordinator) is False
        assert coordinator.terminal is False
        assert coordinator.relaunch_pending is False


class TestRunAppRelaunchBoundary:
    def test_launcher_runs_only_after_old_gui_returns_from_cleanup(self, monkeypatch):
        import sys

        from ports_gfx import app as app_module

        order: list[str] = []
        monkeypatch.setitem(sys.modules, "pygame", object())

        def fake_run(_pygame, _romcloud_bin, coordinator, _diagnostics):
            coordinator.mark_update_succeeded(progress_complete=True)
            order.append("gui-cleanup-complete")
            return 0

        def fake_popen(argv, **_kwargs):
            order.append(f"launch:{argv[0]}")
            return object()

        monkeypatch.setattr(app_module, "_run", fake_run)

        result = app_module.run_app(
            "/userdata/system/romcloud/bin/romcloud",
            relaunch_popen=fake_popen,
        )

        assert result == 0
        assert order == [
            "gui-cleanup-complete",
            "launch:/userdata/system/romcloud/bin/romcloud-ports",
        ]

    def test_relaunch_failure_exits_old_gui_and_reports_manual_fallback(
        self, monkeypatch, tmp_path, capsys
    ):
        import sys

        from ports_gfx import app as app_module

        monkeypatch.setitem(sys.modules, "pygame", object())

        def fake_run(_pygame, _romcloud_bin, coordinator, _diagnostics):
            coordinator.mark_update_succeeded(progress_complete=True)
            return 0

        def fail_popen(_argv, **_kwargs):
            raise OSError("launcher unavailable")

        monkeypatch.setattr(app_module, "_run", fake_run)
        log_path = tmp_path / "gui-relaunch.log"

        result = app_module.run_app(
            "/opt/romcloud/bin/romcloud",
            relaunch_popen=fail_popen,
            relaunch_failure_log=log_path,
        )

        assert result == 1
        assert "updated successfully" in capsys.readouterr().err
        assert "Reopen ROMCloud" in log_path.read_text()

    def test_failed_update_return_does_not_launch(self, monkeypatch):
        import sys

        from ports_gfx import app as app_module

        monkeypatch.setitem(sys.modules, "pygame", object())
        calls: list[list[str]] = []

        def fake_run(_pygame, _romcloud_bin, coordinator, _diagnostics):
            coordinator.mark_update_failed()
            return 0

        monkeypatch.setattr(app_module, "_run", fake_run)

        result = app_module.run_app(
            "/opt/romcloud/bin/romcloud",
            relaunch_popen=lambda argv, **_kwargs: calls.append(argv),
        )

        assert result == 0
        assert calls == []

    def test_normal_terminal_exit_never_launches_a_replacement(self, monkeypatch):
        import sys

        from ports_gfx import app as app_module

        monkeypatch.setitem(sys.modules, "pygame", object())
        calls: list[list[str]] = []
        monkeypatch.setattr(app_module, "_run", lambda *_args: 0)

        result = app_module.run_app(
            "/opt/romcloud/bin/romcloud",
            relaunch_popen=lambda argv, **_kwargs: calls.append(argv),
        )

        assert result == 0
        assert calls == []


class TestOperationSummaryMessage:
    def _operation(self, *, state: OperationState, error: str = ""):
        from ports_gfx.operation_screen import OperationScreenState

        return OperationScreenState(title="Refresh Catalog", runner=_FakeFinishedRunner(state, error))

    def test_succeeded_operation_reports_success(self):
        operation = self._operation(state=OperationState.SUCCEEDED)
        message, kind = operation_summary_message(operation)
        assert message == "Refresh Catalog: succeeded"
        assert kind == "success"

    def test_failed_operation_reports_error_with_detail(self):
        operation = self._operation(state=OperationState.FAILED, error="exited with code 1")
        message, kind = operation_summary_message(operation)
        assert message == "Refresh Catalog: failed (exited with code 1)"
        assert kind == "error"

    def test_failed_operation_without_detail_still_reports_error(self):
        operation = self._operation(state=OperationState.FAILED, error="")
        message, kind = operation_summary_message(operation)
        assert message == "Refresh Catalog: failed"
        assert kind == "error"


class TestModeTransitionExit:
    def _operation(
        self,
        *,
        state: OperationState,
        payload: str,
        title: str = "Cached Storage",
        owns_exit: bool = True,
    ):
        from ports_gfx.operation_screen import OperationScreenState

        return OperationScreenState(
            title=title,
            runner=_FakeUpdateRunner(
                state,
                [OperationLine("stdout", payload)],
            ),
            exits_after_mode_change=owns_exit,
        )

    def test_genuine_transition_renders_notice_and_requests_normal_exit(self):
        operation = self._operation(
            state=OperationState.SUCCEEDED,
            payload=(
                '{"ok":true,"mode_changed":true,'
                '"es_restart_requested":true}'
            ),
        )
        frames: list[object] = []

        assert completed_mode_transition_requires_exit(operation) is True
        assert render_completed_mode_transition_exit(
            operation, _RecordingSplash(frames)
        ) is True
        assert frames == [
            (
                "splash",
                "Mode changed successfully.",
                "Refreshing EmulationStation game list…",
                1.0,
            )
        ]

    def test_same_mode_success_stays_in_romcloud(self):
        operation = self._operation(
            state=OperationState.SUCCEEDED,
            payload=(
                '{"ok":true,"mode_changed":false,'
                '"es_restart_requested":false}'
            ),
        )

        assert completed_mode_transition_requires_exit(operation) is False

    def test_failed_transition_never_exits(self):
        operation = self._operation(
            state=OperationState.FAILED,
            payload='{"ok":false,"error":"boom"}',
        )

        assert completed_mode_transition_requires_exit(operation) is False

    def test_non_mode_operation_cannot_claim_mode_exit(self):
        operation = self._operation(
            state=OperationState.SUCCEEDED,
            payload=(
                '{"ok":true,"mode_changed":true,'
                '"es_restart_requested":true}'
            ),
            owns_exit=False,
        )

        assert completed_mode_transition_requires_exit(operation) is False
