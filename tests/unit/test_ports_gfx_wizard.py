from __future__ import annotations

from ports_gfx.actions import Action
from ports_gfx.activity import ActivityEvent
from ports_gfx.client import BackendResult
from ports_gfx.input_manager import InputEvent
from ports_gfx.layout import Rect
from ports_gfx.osk import MASK_CHAR
from ports_gfx.wizard import STEP_CONTEXT, STEPS, WizardState, WizardStep


RECTS = [Rect(0, index * 20, 100, 18) for index in range(8)]


class _Runner:
    def __init__(self, *, finished=True):
        self.is_finished = finished
        self.cancelled = False

    def poll(self):
        return []

    def cancel(self):
        self.cancelled = True


def _confirm_key_index(wizard):
    return next(index for index, key in enumerate(wizard.osk.keys) if key.kind == "confirm")


def test_fresh_and_partial_welcome_states():
    fresh = WizardState(BackendResult(True, {"state": "fresh"}))
    partial = WizardState(BackendResult(True, {"state": "partial", "issues": ["credentials missing"]}))
    assert fresh.options == ["Start Setup"]
    assert partial.options == ["Resume / Repair Setup"]
    assert partial.issues == ["credentials missing"]


def test_every_wizard_step_has_short_user_facing_context():
    assert set(STEP_CONTEXT) == set(STEPS)
    for step in STEPS:
        wizard = WizardState()
        wizard.step = step
        primary, secondary = wizard.context_lines
        assert primary
        assert secondary
        combined = f"{primary} {secondary}".lower()
        assert "provider abstraction" not in combined
        assert "mount worker" not in combined
        assert "sqlite" not in combined


def test_local_folder_context_matches_the_folder_purpose():
    wizard = WizardState()
    wizard.step = WizardStep.LOCAL_BROWSE

    wizard.local_browse_purpose = "source"
    assert "system folders" in wizard.context_lines[0]
    wizard.local_browse_purpose = "cache"
    assert "local game copies" in wizard.context_lines[0]
    wizard.local_browse_purpose = "remote_data"
    assert "synchronized saves" in wizard.context_lines[0]


def test_running_wizard_progress_is_indeterminate_until_a_total_is_reported():
    wizard = WizardState()
    wizard.step = WizardStep.APPLY
    wizard.runner = _Runner(finished=False)

    assert wizard.progress is not None
    assert wizard.progress.fraction is None
    assert wizard.progress.message == "Saving configuration…"


def test_wizard_progress_tracks_phase_totals_without_finishing_early():
    wizard = WizardState()
    wizard.step = WizardStep.APPLY
    wizard.runner = _Runner(finished=False)
    wizard._progress_event = ActivityEvent(  # noqa: SLF001 - pure progress model
        "", "catalog_refresh", "system_progress", "running", "Scanning games", current=8, total=10
    )

    assert wizard.progress is not None
    assert wizard.progress.label == "Scanning games — 8 / 10"
    assert wizard.progress.fraction == 0.8

    wizard._progress_event = ActivityEvent(  # noqa: SLF001
        "", "catalog_refresh", "overall_progress", "running", "Finishing scan", current=10, total=10
    )
    assert wizard.progress.fraction == 0.99

    wizard._progress_event = ActivityEvent(  # noqa: SLF001
        "", "catalog_refresh", "refresh_completed", "success", "Library scan complete", current=10, total=10
    )
    assert wizard.progress.fraction == 1.0
    assert wizard.progress.message == "Library scan complete"


def test_controller_keyboard_and_touch_use_the_same_next_back_actions():
    for event in (
        InputEvent(Action.CONFIRM),
        InputEvent(Action.CONFIRM, touch_index=0),
    ):
        wizard = WizardState()
        wizard.handle_event(event, RECTS[:1], "romcloud")
        assert wizard.step == WizardStep.SOURCE
        wizard.handle_event(InputEvent(Action.BACK), RECTS[:2], "romcloud")
        assert wizard.step == WizardStep.WELCOME


def test_server_port_and_username_are_entered_with_osk():
    wizard = WizardState()
    wizard.enter_text_step(WizardStep.SERVER)
    wizard.handle_event(InputEvent(Action.TEXT_INPUT, text="nas.local"), RECTS, "romcloud")
    wizard.osk.select(_confirm_key_index(wizard))
    wizard.handle_event(InputEvent(Action.CONFIRM), RECTS, "romcloud")
    assert wizard.server == "nas.local"
    assert wizard.step == WizardStep.PORT
    wizard.osk.select(_confirm_key_index(wizard))
    wizard.handle_event(InputEvent(Action.CONFIRM), RECTS, "romcloud")
    assert wizard.step == WizardStep.USERNAME
    assert wizard.osk is not None


def test_password_is_masked_and_cancel_returns_without_saving():
    wizard = WizardState()
    wizard.enter_text_step(WizardStep.PASSWORD)
    wizard.handle_event(InputEvent(Action.TEXT_INPUT, text="secret"), RECTS, "romcloud")
    assert wizard.osk.displayed_text == MASK_CHAR * 6
    wizard.handle_event(InputEvent(Action.BACK), RECTS, "romcloud")
    assert wizard.step == WizardStep.USERNAME
    assert wizard.password == ""
    assert "secret" not in repr(wizard)


def test_share_discovery_success_and_failure(monkeypatch):
    wizard = WizardState()
    wizard.step = WizardStep.DISCOVER
    wizard.runner = _Runner()
    monkeypatch.setattr(
        "ports_gfx.wizard.operation_result",
        lambda runner: BackendResult(True, {"shares": [{"name": "ROMs", "comment": ""}]}),
    )
    wizard.poll()
    assert wizard.step == WizardStep.SHARE
    assert wizard.options == ["ROMs"]

    wizard.step = WizardStep.DISCOVER
    wizard.runner = _Runner()
    monkeypatch.setattr(
        "ports_gfx.wizard.operation_result",
        lambda runner: BackendResult(False, error="authentication failed"),
    )
    wizard.poll()
    assert wizard.step == WizardStep.DISCOVER
    assert wizard.error == "Could not connect. Check the ROM server and account, then retry."
    assert wizard.technical_error == "authentication failed"
    assert wizard.options == ["Retry"]


def test_multiple_or_no_detected_systems_are_reviewable(monkeypatch):
    for systems in (["psx", "snes"], []):
        wizard = WizardState()
        wizard.step = WizardStep.DETECT
        wizard.runner = _Runner()
        monkeypatch.setattr(
            "ports_gfx.wizard.operation_result",
            lambda runner, systems=systems: BackendResult(True, {"systems": systems}),
        )
        wizard.poll()
        assert wizard.step == WizardStep.SYSTEMS
        assert wizard.systems == systems
        assert wizard.options == ["Continue"]


def test_detected_systems_lead_to_explicit_game_access_and_remote_data_choices():
    wizard = WizardState()
    wizard.step = WizardStep.SYSTEMS

    wizard._confirm("romcloud")  # noqa: SLF001 - pure navigation test

    assert wizard.step == WizardStep.GAME_ACCESS
    assert wizard.options == ["Smart Cache", "Direct / NAS"]

    wizard._confirm("romcloud")  # Smart Cache

    assert wizard.step == WizardStep.REMOTE_DATA
    assert wizard.options == [
        "SMB network location",
        "Local / external directory",
        "Skip (sync features unavailable)",
    ]


def test_skipping_remote_data_makes_choice_explicit():
    wizard = WizardState()
    wizard.step = WizardStep.REMOTE_DATA
    wizard.selected_index = 2

    wizard._confirm("romcloud")  # noqa: SLF001 - pure navigation test

    assert wizard.remote_data_type == "none"
    assert wizard.library_sync_enabled is False
    assert wizard.step == WizardStep.CACHE


def test_writable_storage_leads_to_explicit_library_sync_opt_in():
    wizard = WizardState()
    wizard.remote_data_type = "local"
    wizard.remote_data_root = "/userdata/romcloud/remote"
    wizard.step = WizardStep.LIBRARY_SYNC

    assert wizard.options == ["Enable Library Sync", "Keep Library Sync disabled"]
    wizard.selected_index = 0
    wizard._confirm("romcloud")  # noqa: SLF001

    assert wizard.library_sync_enabled is True
    assert wizard.step == WizardStep.CACHE
    assert wizard.request_payload()["library_sync_enabled"] is True


def test_direct_mode_skips_cache_settings_and_explains_source_requirement():
    wizard = WizardState()
    wizard.step = WizardStep.GAME_ACCESS
    wizard.selected_index = 1
    wizard._confirm("romcloud")  # noqa: SLF001
    wizard.selected_index = 2
    wizard._confirm("romcloud")  # noqa: SLF001

    assert wizard.game_access_mode == "direct_nas"
    assert wizard.step == WizardStep.REVIEW
    assert "game_access_mode" in wizard.request_payload()


def test_remote_smb_can_reuse_source_credentials_without_second_password(monkeypatch):
    wizard = WizardState()
    wizard.server = "omnivault"
    wizard.username = "stryph"
    wizard.password = "source-secret"
    wizard.step = WizardStep.REMOTE_DATA
    wizard.selected_index = 0

    wizard._confirm("romcloud")  # noqa: SLF001
    assert wizard.step == WizardStep.REMOTE_AUTH

    started = []
    monkeypatch.setattr(
        wizard,
        "_start_operation",
        lambda step, action, binary: started.append((step, action)),
    )
    wizard.selected_index = 0
    wizard._confirm("romcloud")  # noqa: SLF001

    assert started == [(WizardStep.REMOTE_DISCOVER, "setup-discover")]
    assert wizard.remote_reuse_source_credentials is True
    assert wizard.remote_server == "omnivault"
    assert wizard.remote_username == "stryph"
    assert wizard.remote_password == ""
    payload = wizard.request_payload()
    assert payload["password"] == "source-secret"
    assert payload["remote_password"] == ""
    assert payload["remote_reuse_source_credentials"] is True


def test_remote_smb_supports_different_server_and_credentials():
    wizard = WizardState()
    wizard.step = WizardStep.REMOTE_AUTH
    wizard.selected_index = 1

    wizard._confirm("romcloud")  # noqa: SLF001

    assert wizard.remote_reuse_source_credentials is False
    assert wizard.step == WizardStep.REMOTE_SERVER
    assert wizard.osk is not None


def test_remote_smb_payload_is_independent_from_rom_source():
    wizard = WizardState()
    wizard.server = "rom-nas.local"
    wizard.share = "ROMs"
    wizard.username = "reader"
    wizard.password = "rom-secret"
    wizard.remote_data_type = "smb"
    wizard.remote_server = "data-nas.local"
    wizard.remote_share = "ROMCloud"
    wizard.remote_username = "writer"
    wizard.remote_password = "data-secret"
    wizard.step = WizardStep.REMOTE_VALIDATE

    payload = wizard.request_payload()

    assert payload["server"] == "rom-nas.local"
    assert payload["remote_server"] == "data-nas.local"
    assert payload["remote_share"] == "ROMCloud"
    assert payload["purpose"] == "remote_data"


def test_apply_failure_retries_and_success_clears_password(monkeypatch):
    wizard = WizardState()
    wizard.step = WizardStep.APPLY
    wizard.password = "secret"
    wizard.remote_password = "remote-secret"
    wizard.runner = _Runner()
    monkeypatch.setattr(
        "ports_gfx.wizard.operation_result",
        lambda runner: BackendResult(False, error="refresh catalog failed"),
    )
    wizard.poll()
    assert wizard.options == ["Retry"]
    assert wizard.runner is None
    assert wizard.progress is None
    assert "review details and retry" in wizard.error
    assert wizard.technical_error == "refresh catalog failed"
    assert wizard.activity.events[-1].detail == "refresh catalog failed"
    assert wizard.password == "secret"

    wizard.runner = _Runner()
    monkeypatch.setattr(
        "ports_gfx.wizard.operation_result",
        lambda runner: BackendResult(True, {"system_count": 2, "max_size_gb": 50}),
    )
    wizard.poll()
    assert wizard.step == WizardStep.DONE
    assert wizard.notice == "ROMCloud setup is complete."
    assert wizard.password == ""
    assert wizard.remote_password == ""


def test_back_cancels_running_operation():
    wizard = WizardState()
    wizard.step = WizardStep.DISCOVER
    runner = _Runner(finished=False)
    wizard.runner = runner
    wizard.back()
    assert runner.cancelled is True
    assert wizard.step == WizardStep.PASSWORD


def test_local_source_choice_opens_non_blocking_folder_browser(monkeypatch):
    wizard = WizardState()
    wizard.step = WizardStep.SOURCE
    wizard.selected_index = 1
    started = []
    monkeypatch.setattr(
        "ports_gfx.wizard.start_backend_operation",
        lambda binary, action, payload: started.append((action, payload)) or _Runner(finished=False),
    )

    wizard._confirm("romcloud")  # noqa: SLF001

    assert wizard.source_type == "local"
    assert wizard.step == WizardStep.LOCAL_BROWSE
    assert started[0][0] == "setup-browse-local"


def test_remote_browser_only_offers_directories_as_selectable_targets(monkeypatch):
    wizard = WizardState()
    wizard.step = WizardStep.SOURCE_BROWSE
    wizard.runner = _Runner()
    monkeypatch.setattr(
        "ports_gfx.wizard.operation_result",
        lambda runner: BackendResult(
            True,
            {
                "path": "Libraries",
                "entries": [
                    {"name": "Roms", "is_directory": True},
                    {"name": "README.txt", "is_directory": False},
                ],
            },
        ),
    )

    wizard.poll()

    assert wizard.options == ["Select this folder", "Up one folder", "Folder: Roms"]
    assert wizard.browser_entries[-1]["is_directory"] is False
