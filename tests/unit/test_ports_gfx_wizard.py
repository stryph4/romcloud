from __future__ import annotations

from ports_gfx.actions import Action
from ports_gfx.client import BackendResult
from ports_gfx.input_manager import InputEvent
from ports_gfx.layout import Rect
from ports_gfx.osk import MASK_CHAR
from ports_gfx.wizard import WizardState, WizardStep


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


def test_server_and_username_are_entered_with_osk():
    wizard = WizardState()
    wizard.enter_text_step(WizardStep.SERVER)
    wizard.handle_event(InputEvent(Action.TEXT_INPUT, text="nas.local"), RECTS, "romcloud")
    wizard.osk.select(_confirm_key_index(wizard))
    wizard.handle_event(InputEvent(Action.CONFIRM), RECTS, "romcloud")
    assert wizard.server == "nas.local"
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
    assert wizard.error == "authentication failed"
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


def test_detected_systems_lead_to_explicit_remote_data_choice():
    wizard = WizardState()
    wizard.step = WizardStep.SYSTEMS

    wizard._confirm("romcloud")  # noqa: SLF001 - pure navigation test

    assert wizard.step == WizardStep.REMOTE_DATA
    assert wizard.options == [
        "SMB network location",
        "Local / external directory",
        "Skip (SaveSync unavailable)",
    ]


def test_skipping_remote_data_makes_choice_explicit():
    wizard = WizardState()
    wizard.step = WizardStep.REMOTE_DATA
    wizard.selected_index = 2

    wizard._confirm("romcloud")  # noqa: SLF001 - pure navigation test

    assert wizard.remote_data_type == "none"
    assert wizard.step == WizardStep.CACHE


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
    assert wizard.password == "secret"

    wizard.runner = _Runner()
    monkeypatch.setattr(
        "ports_gfx.wizard.operation_result",
        lambda runner: BackendResult(True, {"system_count": 2, "max_size_gb": 50}),
    )
    wizard.poll()
    assert wizard.step == WizardStep.DONE
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
