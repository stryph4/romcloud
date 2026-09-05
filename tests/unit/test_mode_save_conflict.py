from __future__ import annotations

from types import SimpleNamespace

from ports_gfx.actions import Action
from ports_gfx.app import mode_save_conflict_from_operation, root_menu_items_for_state
from ports_gfx.input_manager import InputEvent
from ports_gfx.mode_save_conflict import ModeSaveConflictState
from ports_gfx.operation import OperationLine


def test_conflict_decision_resolves_or_cancels_without_mutating_saves() -> None:
    state = ModeSaveConflictState(("one",))

    assert state.handle_event(InputEvent(action=Action.CONFIRM)) == "resolve"
    assert state.handle_event(InputEvent(action=Action.BACK)) == "cancel"


def test_remote_wins_requires_a_continuous_hold() -> None:
    state = ModeSaveConflictState(("one",))
    state.handle_event(InputEvent(action=Action.DOWN))
    state.handle_event(InputEvent(action=Action.CONFIRM))

    assert state.update(2.9) is None
    assert state.update(0.1) == "remote-wins"


def test_releasing_remote_wins_resets_confirmation() -> None:
    state = ModeSaveConflictState(("one",))
    state.select(1)
    state.handle_event(InputEvent(action=Action.CONFIRM))
    state.update(2.9)
    state.handle_event(InputEvent(action=Action.CONFIRM_RELEASED))

    assert state.update(1.0) is None
    assert state.confirm.progress == 0.0


def test_only_structured_save_authority_failure_opens_conflict_decision() -> None:
    payload = (
        '{"ok": false, "save_authority_conflict": true, '
        '"conflict_ids": ["one", "two"]}'
    )
    operation = SimpleNamespace(
        is_finished=True,
        succeeded=False,
        runner=SimpleNamespace(
            lines=[OperationLine("stdout", payload)], error="transition failed"
        ),
    )

    state = mode_save_conflict_from_operation(operation)

    assert state is not None
    assert state.conflict_ids == ("one", "two")

    operation.runner.lines = [
        OperationLine("stdout", '{"ok": false, "error": "mount failed"}')
    ]
    assert mode_save_conflict_from_operation(operation) is None


def test_direct_menu_distinguishes_remote_and_local_save_behavior() -> None:
    base = {
        "game_management_enabled": True,
        "operating_mode": "connected",
        "capabilities": {},
    }
    remote = root_menu_items_for_state(
        {**base, "direct_save_storage_capable": True}
    )
    local = root_menu_items_for_state(
        {**base, "direct_save_storage_capable": False}
    )
    remote_direct = next(item for item in remote if item.label == "Direct")
    local_direct = next(item for item in local if item.label == "Direct")

    assert "use remote storage directly" in remote_direct.description
    assert "saves remain local" in local_direct.description
    assert "manual SaveSync" in local_direct.description
