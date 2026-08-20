from __future__ import annotations

from ports_gfx.client import BackendResult
from ports_gfx.system_selection_screen import (
    APPLYING,
    RESULT,
    SELECTING,
    SystemSelectionScreenState,
)
from ports_gfx.app import SELECT_SYSTEMS_ACTION, menu_categories_for_state


class _Runner:
    is_finished = True

    def poll(self):
        return []

    def cancel(self):
        pass


def test_post_setup_selection_loads_toggles_and_applies(monkeypatch):
    state = SystemSelectionScreenState("romcloud")
    state._runner = _Runner()  # noqa: SLF001
    monkeypatch.setattr(
        "ports_gfx.system_selection_screen.operation_result",
        lambda _runner: BackendResult(
            True,
            {
                "detected_systems": ["nes", "ps2"],
                "selected_systems": ["ps2"],
            },
        ),
    )
    state.poll()
    assert state.step == SELECTING
    assert state.selected_systems == {"ps2"}

    state.selected_index = 2  # nes
    state.activate()
    assert state.selected_systems == {"nes", "ps2"}

    started = []
    monkeypatch.setattr(
        "ports_gfx.system_selection_screen.start_backend_operation",
        lambda binary, action, payload, popen=None: started.append(
            (binary, action, payload)
        ) or _Runner(),
    )
    state.selected_index = len(state.options) - 1
    state.activate()
    assert state.step == APPLYING
    assert started[0][1] == "system-selection-apply"
    assert started[0][2]["selected_systems"] == ["nes", "ps2"]

    monkeypatch.setattr(
        "ports_gfx.system_selection_screen.operation_result",
        lambda _runner: BackendResult(True, {"selected_systems": ["nes", "ps2"]}),
    )
    state.poll()
    assert state.step == RESULT


def test_select_systems_is_exposed_under_library():
    categories = menu_categories_for_state({"capabilities": {}})
    assert SELECT_SYSTEMS_ACTION in {
        item.action for item in categories["Library"]
    }
