from __future__ import annotations

from ports_gfx.actions import Action
from ports_gfx.activity import ActivityEvent
from ports_gfx.client import BackendResult
from ports_gfx.input_manager import InputEvent
from ports_gfx.layout import Rect
from ports_gfx.osk import MASK_FALLBACK_CHAR
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
    assert wizard.progress.message == "Saving configuration and initializing SaveSync…"


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
    assert wizard.osk.displayed_text == MASK_FALLBACK_CHAR * 6
    wizard.handle_event(InputEvent(Action.BACK), RECTS, "romcloud")
    assert wizard.step == WizardStep.USERNAME
    assert wizard.password == ""
    assert "secret" not in repr(wizard)


def test_keyboard_enter_submits_wizard_password_without_confirm_focus(monkeypatch):
    wizard = WizardState()
    wizard.enter_text_step(WizardStep.PASSWORD)
    wizard.handle_event(
        InputEvent(Action.TEXT_INPUT, text="secret", source="keyboard"),
        RECTS,
        "romcloud",
    )
    started = []
    monkeypatch.setattr(
        wizard,
        "_start_operation",
        lambda step, action, binary: started.append((step, action)),
    )

    wizard.handle_event(
        InputEvent(Action.TEXT_SUBMIT, source="keyboard"), RECTS, "romcloud"
    )

    assert wizard.password == "secret"
    assert wizard.osk is None
    assert started == [(WizardStep.DISCOVER, "setup-discover")]


def test_controller_start_submits_wizard_text_without_confirm_focus():
    wizard = WizardState()
    wizard.enter_text_step(WizardStep.SERVER)
    wizard.handle_event(
        InputEvent(Action.TEXT_INPUT, text="nas.local", source="keyboard"),
        RECTS,
        "romcloud",
    )

    wizard.handle_event(
        InputEvent(Action.MENU, source="controller"), RECTS, "romcloud"
    )

    assert wizard.server == "nas.local"
    assert wizard.step == WizardStep.PORT


def test_controller_l3_toggles_wizard_osk_shift():
    wizard = WizardState()
    wizard.enter_text_step(WizardStep.USERNAME)

    wizard.handle_event(
        InputEvent(Action.TEXT_TOGGLE_SHIFT, source="controller"),
        RECTS,
        "romcloud",
    )

    assert wizard.osk is not None
    assert wizard.osk.shift is True


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
        assert wizard.selected_systems == set(systems)
        assert wizard.options == [
            "Select All",
            "Clear All",
            *[f"[x] {system}" for system in systems],
            "Continue",
        ]


def test_detected_systems_lead_to_explicit_game_access_and_remote_data_choices():
    wizard = WizardState()
    wizard.step = WizardStep.SYSTEMS
    wizard.systems = ["ps2"]
    wizard.selected_systems = {"ps2"}
    wizard.selected_index = len(wizard.options) - 1

    wizard._confirm("romcloud")  # noqa: SLF001 - pure navigation test

    assert wizard.step == WizardStep.GAME_ACCESS
    assert wizard.options == ["Cached Storage", "Direct"]

    wizard._confirm("romcloud")  # Cached Storage

    assert wizard.step == WizardStep.REMOTE_DATA
    assert wizard.options == [
        "SMB network location",
        "Local / external directory",
        "SFTP server",
        "Skip (sync features unavailable)",
    ]


def test_sftp_is_a_source_option_and_only_offers_cached_storage():
    wizard = WizardState()
    wizard.step = WizardStep.SOURCE

    assert "SFTP server" in wizard.options
    wizard.selected_index = wizard.options.index("SFTP server")
    wizard._confirm("romcloud", show_osk=False)  # noqa: SLF001

    assert wizard.source_type == "sftp"
    assert wizard.step == WizardStep.SERVER
    wizard.step = WizardStep.GAME_ACCESS
    assert wizard.options == ["Cached Storage"]


def test_sftp_host_key_requires_explicit_fingerprint_trust(monkeypatch):
    started = []
    monkeypatch.setattr(
        "ports_gfx.wizard.start_backend_operation",
        lambda binary, action, payload: started.append((action, dict(payload)))
        or _Runner(finished=False),
    )
    wizard = WizardState()
    wizard.source_type = "sftp"
    wizard.step = WizardStep.SFTP_TRUST
    wizard.runner = _Runner()
    monkeypatch.setattr(
        "ports_gfx.wizard.operation_result",
        lambda runner: BackendResult(
            True,
            {
                "host_key_type": "ssh-ed25519",
                "host_key_fingerprint": "SHA256:trusted-key",
            },
        ),
    )

    wizard.poll()

    assert wizard.sftp_host_key_type == "ssh-ed25519"
    assert wizard.sftp_host_key_fingerprint == "SHA256:trusted-key"
    assert wizard.step == WizardStep.SFTP_TRUST
    assert wizard.options == ["Trust this host key"]
    wizard._confirm("romcloud", show_osk=False)  # noqa: SLF001
    assert wizard.step == WizardStep.SFTP_BROWSE
    assert started[-1][0] == "setup-browse-sftp"
    assert started[-1][1]["sftp_browse_path"] == "/"


def test_sftp_source_and_remote_payloads_remain_independent():
    wizard = WizardState()
    wizard.source_type = "sftp"
    wizard.server = "roms.example"
    wizard.port = 2222
    wizard.username = "rom-reader"
    wizard.password = "source-secret"
    wizard.source_remote_path = "/srv/roms"
    wizard.sftp_host_key_fingerprint = "SHA256:source-key"
    wizard.remote_data_type = "sftp"
    wizard.remote_server = "data.example"
    wizard.remote_port = 2200
    wizard.remote_username = "data-reader"
    wizard.remote_password = "remote-secret"
    wizard.remote_data_root = "/srv/data"
    wizard.remote_sftp_host_key_fingerprint = "SHA256:remote-key"
    wizard.step = WizardStep.REMOTE_VALIDATE

    payload = wizard.request_payload()

    assert payload["purpose"] == "remote_data"
    assert (payload["server"], payload["port"], payload["username"]) == (
        "roms.example",
        2222,
        "rom-reader",
    )
    assert payload["source_remote_path"] == "/srv/roms"
    assert payload["sftp_host_key_fingerprint"] == "SHA256:source-key"
    assert (
        payload["remote_server"],
        payload["remote_port"],
        payload["remote_username"],
    ) == ("data.example", 2200, "data-reader")
    assert payload["remote_data_root"] == "/srv/data"
    assert payload["remote_sftp_host_key_fingerprint"] == "SHA256:remote-key"


def test_later_back_returns_to_the_previous_sftp_wizard_step():
    wizard = WizardState()
    wizard.source_type = "sftp"
    wizard.step = WizardStep.SFTP_PATH

    wizard.back()

    assert wizard.step == WizardStep.SFTP_BROWSE


def test_sftp_source_browser_lists_root_and_enters_case_sensitive_folder(monkeypatch):
    calls = []
    monkeypatch.setattr(
        "ports_gfx.wizard.start_backend_operation",
        lambda binary, action, payload: calls.append((action, dict(payload)))
        or _Runner(finished=False),
    )
    wizard = WizardState()
    wizard.source_type = "sftp"
    wizard.server = "roms.example"
    wizard.port = 2222
    wizard.username = "reader"
    wizard.password = "secret"
    wizard.sftp_host_key_fingerprint = "SHA256:key"
    wizard.step = WizardStep.SFTP_BROWSE
    wizard.source_sftp_browse_path = "/"
    wizard.source_sftp_browse_entries = [
        {"name": "Roms", "is_directory": True},
        {"name": "lowercase", "is_directory": True},
    ]
    wizard.selected_index = 2

    wizard._confirm("romcloud")  # noqa: SLF001

    assert wizard.source_sftp_browse_path == "/Roms"
    assert calls[-1][0] == "setup-browse-sftp"
    assert calls[-1][1]["sftp_browse_path"] == "/Roms"
    assert calls[-1][1]["purpose"] == "source"


def test_sftp_browser_failure_surfaces_backend_diagnostic(monkeypatch):
    wizard = WizardState()
    wizard.step = WizardStep.SFTP_BROWSE
    wizard.runner = _Runner()
    monkeypatch.setattr(
        "ports_gfx.wizard.operation_result",
        lambda _runner: BackendResult(
            False,
            error=(
                "SFTP browser ProviderAuthError: credentials rejected; "
                "password_present=False; path=/"
            ),
        ),
    )

    wizard.poll()

    assert "Could not open that SFTP folder" in wizard.error
    assert "ProviderAuthError: credentials rejected" in wizard.error
    assert "password_present=False" in wizard.error
    assert wizard.technical_error in wizard.error


def test_sftp_browser_parent_and_root_boundary_are_safe(monkeypatch):
    calls = []
    monkeypatch.setattr(
        "ports_gfx.wizard.start_backend_operation",
        lambda binary, action, payload: calls.append(dict(payload))
        or _Runner(finished=False),
    )
    wizard = WizardState()
    wizard.step = WizardStep.SFTP_BROWSE
    wizard.source_sftp_browse_path = "/Roms/PS2"
    wizard.browser_path = "/Roms/PS2"

    wizard.back()
    assert wizard.source_sftp_browse_path == "/Roms"
    wizard.back()
    assert wizard.source_sftp_browse_path == "/"
    assert "Up one folder" not in wizard.options
    wizard.back()
    assert wizard.step == WizardStep.SFTP_TRUST
    assert calls == []


def test_sftp_select_folder_persists_path_and_starts_source_validation(monkeypatch):
    started = []
    wizard = WizardState()
    wizard.step = WizardStep.SFTP_BROWSE
    wizard.source_sftp_browse_path = "/CaseSensitive/Roms"
    monkeypatch.setattr(
        wizard,
        "_start_operation",
        lambda step, action, binary: started.append((step, action)),
    )

    wizard._confirm("romcloud")  # noqa: SLF001

    assert wizard.source_remote_path == "/CaseSensitive/Roms"
    assert started == [(WizardStep.DETECT, "setup-validate")]


def test_sftp_manual_path_fallback_accepts_paths_and_rejects_urls(monkeypatch):
    wizard = WizardState()
    wizard.step = WizardStep.SFTP_BROWSE
    wizard.selected_index = 1
    wizard._confirm("romcloud", show_osk=False)  # noqa: SLF001
    assert wizard.step == WizardStep.SFTP_PATH

    wizard.osk.text = "sftp://roms.example/Roms"
    wizard._commit_osk("romcloud")  # noqa: SLF001
    assert "without sftp:// or a server name" in wizard.error
    assert wizard.step == WizardStep.SFTP_PATH

    started = []
    monkeypatch.setattr(
        wizard,
        "_start_operation",
        lambda step, action, binary: started.append((step, action)),
    )
    wizard.osk.text = "/Roms/PS2"
    wizard._commit_osk("romcloud")  # noqa: SLF001
    assert wizard.source_remote_path == "/Roms/PS2"
    assert started == [(WizardStep.DETECT, "setup-validate")]


def test_remote_sftp_browser_state_and_credentials_are_independent(monkeypatch):
    calls = []
    monkeypatch.setattr(
        "ports_gfx.wizard.start_backend_operation",
        lambda binary, action, payload: calls.append((action, dict(payload)))
        or _Runner(finished=False),
    )
    wizard = WizardState()
    wizard.source_type = "sftp"
    wizard.server = "roms.example"
    wizard.username = "rom-reader"
    wizard.password = "source-secret"
    wizard.source_sftp_browse_path = "/Roms/PS2"
    wizard.source_sftp_browse_entries = [{"name": "Games", "is_directory": True}]
    wizard.sftp_host_key_fingerprint = "SHA256:source"
    wizard.remote_data_type = "sftp"
    wizard.remote_server = "data.example"
    wizard.remote_port = 2200
    wizard.remote_username = "data-reader"
    wizard.remote_password = "remote-secret"
    wizard.remote_sftp_host_key_fingerprint = "SHA256:remote"

    wizard._start_sftp_browse("remote_data", "/SharedData", "romcloud")  # noqa: SLF001

    assert wizard.remote_sftp_browse_path == "/SharedData"
    assert wizard.source_sftp_browse_path == "/Roms/PS2"
    assert wizard.source_sftp_browse_entries == [
        {"name": "Games", "is_directory": True}
    ]
    assert calls[-1][1]["purpose"] == "remote_data"
    assert calls[-1][1]["remote_server"] == "data.example"
    assert calls[-1][1]["server"] == "roms.example"


def test_remote_sftp_browser_appears_after_host_key_trust(monkeypatch):
    monkeypatch.setattr(
        "ports_gfx.wizard.start_backend_operation",
        lambda binary, action, payload: _Runner(finished=False),
    )
    wizard = WizardState()
    wizard.remote_data_type = "sftp"
    wizard.remote_sftp_host_key_fingerprint = "SHA256:remote"
    wizard.step = WizardStep.REMOTE_SFTP_TRUST

    wizard._confirm("romcloud")  # noqa: SLF001

    assert wizard.step == WizardStep.REMOTE_SFTP_BROWSE
    assert wizard.remote_sftp_browse_path == "/"


def test_remote_sftp_select_folder_starts_remote_validation(monkeypatch):
    started = []
    wizard = WizardState()
    wizard.remote_data_type = "sftp"
    wizard.step = WizardStep.REMOTE_SFTP_BROWSE
    wizard.remote_sftp_browse_path = "/Shared/Data"
    monkeypatch.setattr(
        wizard,
        "_start_operation",
        lambda step, action, binary: started.append((step, action)),
    )

    wizard._confirm("romcloud")  # noqa: SLF001

    assert wizard.remote_data_root == "/Shared/Data"
    assert started == [(WizardStep.REMOTE_VALIDATE, "setup-validate")]


def test_system_multi_select_persists_canonical_ids_in_request():
    wizard = WizardState()
    wizard.step = WizardStep.SYSTEMS
    wizard.systems = ["nes", "ps2", "snes"]
    wizard.selected_systems = set(wizard.systems)

    wizard.selected_index = 3  # ps2 row
    wizard._confirm("romcloud")  # noqa: SLF001
    assert wizard.selected_systems == {"nes", "snes"}

    payload = wizard.request_payload()
    assert payload["selected_systems"] == ["nes", "snes"]


def test_skipping_remote_data_makes_choice_explicit():
    wizard = WizardState()
    wizard.step = WizardStep.REMOTE_DATA
    wizard.selected_index = 3

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
    wizard.selected_index = 3
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
