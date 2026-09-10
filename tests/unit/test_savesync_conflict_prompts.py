from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from ports_gfx.actions import Action
from ports_gfx.client import BackendResult
from ports_gfx.input_manager import InputEvent
from ports_gfx.savesync_conflict_popup import (
    APPLYING,
    ConflictPopupState,
    DISPLAYING,
    LOADING,
    _acquire_popup_focus,
)
from romcloud.core.exceptions import SaveSyncVerificationError
from romcloud.core.models.savesync import SaveConflictResolution
from romcloud.core.storage import StorageProvider
from romcloud.infrastructure import savesync_prompts
from romcloud.services.saves import SaveSyncService

from tests.unit._savesync_protocol_helpers import (
    mutate_remote_out_of_band,
    seed_peer_commit,
)


class _Provider(StorageProvider):
    @property
    def provider_id(self) -> str:
        return "test"

    @property
    def capabilities(self):
        from romcloud.core.storage import ProviderCapabilities

        return ProviderCapabilities(
            has_filesystem_semantics=True, supports_durable_transactions=True
        )

    def is_reachable(self, root: str) -> bool:
        return True

    def list_systems(self, rom_root: str):
        raise NotImplementedError

    def list_entries(self, rom_root: str, system: str):
        raise NotImplementedError

    def get_size(self, path: str):
        raise NotImplementedError

    def read_text(self, path: str):
        raise NotImplementedError

    def transfer_to(self, source_path: str, dest_path: str, on_progress=None):
        raise NotImplementedError


def _service(tmp_path: Path) -> SaveSyncService:
    (tmp_path / "local").mkdir()
    (tmp_path / "remote").mkdir()
    return SaveSyncService(
        provider=_Provider(),
        connectivity_root=str(tmp_path / "remote"),
        local_root=str(tmp_path / "local"),
        remote_root=str(tmp_path / "remote"),
        state_path=tmp_path / "data" / "savesync-state.json",
    )


def _write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)


def _conflict(tmp_path: Path) -> tuple[SaveSyncService, str]:
    service = _service(tmp_path)
    local = tmp_path / "local" / "psx" / "Game.srm"
    _write(local, b"base")
    service.full_sync()
    _write(local, b"local-progress")
    # The remote divergence is another upgraded device's committed upload,
    # so payload and authoritative index agree — the ordinary precondition
    # for resolving a conflict. Manual/out-of-band divergence is a different
    # scenario and is covered by TestOutOfBandRemoteMutation.
    seed_peer_commit(
        service,
        remote_root=tmp_path / "remote",
        relative_path="psx/Game.srm",
        content=b"remote-progress",
    )
    service.reconcile()
    conflict = service.get_state().active_conflicts[0]
    return service, conflict.conflict_id


def test_prompt_queue_preserves_exact_ids_and_deduplicates(tmp_path: Path):
    data_root = tmp_path / "data"

    assert savesync_prompts.enqueue(data_root, ("old", "new", "old")) == (
        "old",
        "new",
    )
    assert savesync_prompts.enqueue(data_root, ("new", "last")) == (
        "old",
        "new",
        "last",
    )
    assert savesync_prompts.complete(data_root, "new") == ("old", "last")


def test_popup_process_lock_rejects_a_duplicate_window(tmp_path: Path):
    with savesync_prompts.popup_process_lock(tmp_path) as first:
        with savesync_prompts.popup_process_lock(tmp_path) as second:
            assert first is True
            assert second is False


def test_targeted_local_wins_resolution_preserves_unrelated_save(tmp_path: Path):
    service, conflict_id = _conflict(tmp_path)
    unrelated_local = tmp_path / "local" / "snes" / "Other.srm"
    unrelated_remote = tmp_path / "remote" / "snes" / "Other.srm"
    _write(unrelated_local, b"local-only-unrelated")
    _write(unrelated_remote, b"remote-only-unrelated")

    record = service.resolve_conflict(
        conflict_id, SaveConflictResolution.KEEP_LOCAL
    )

    assert (tmp_path / "remote" / "psx" / "Game.srm").read_bytes() == b"local-progress"
    assert unrelated_local.read_bytes() == b"local-only-unrelated"
    assert unrelated_remote.read_bytes() == b"remote-only-unrelated"
    assert record.artifact_count == 1
    state = service.get_state()
    resolved = next(item for item in state.conflicts if item.conflict_id == conflict_id)
    assert resolved.resolution is SaveConflictResolution.KEEP_LOCAL
    assert not state.active_conflicts


def test_targeted_remote_wins_resolution_replaces_only_local_group(tmp_path: Path):
    service, conflict_id = _conflict(tmp_path)

    service.resolve_conflict(conflict_id, SaveConflictResolution.KEEP_REMOTE)

    assert (tmp_path / "local" / "psx" / "Game.srm").read_bytes() == b"remote-progress"
    resolved = service.get_state().conflicts[0]
    assert resolved.resolution is SaveConflictResolution.KEEP_REMOTE


def test_targeted_local_wins_can_verify_and_commit_group_deletion(tmp_path: Path):
    service = _service(tmp_path)
    local = tmp_path / "local" / "psx" / "Game.srm"
    remote = tmp_path / "remote" / "psx" / "Game.srm"
    _write(local, b"base")
    service.full_sync()
    local.unlink()
    seed_peer_commit(
        service,
        remote_root=tmp_path / "remote",
        relative_path="psx/Game.srm",
        content=b"remote-progress",
    )
    service.reconcile()
    conflict_id = service.get_state().active_conflicts[0].conflict_id

    record = service.resolve_conflict(
        conflict_id, SaveConflictResolution.KEEP_LOCAL
    )

    assert not remote.exists()
    assert record.manifest == ()
    group = service.get_state().groups[0]
    assert group.condition.value == "clean"
    assert group.baseline is not None and group.baseline.artifacts == ()


def test_targeted_resolution_refuses_changed_conflict_content(tmp_path: Path):
    service, conflict_id = _conflict(tmp_path)
    _write(tmp_path / "local" / "psx" / "Game.srm", b"newer-local")

    with pytest.raises(SaveSyncVerificationError, match="changed after"):
        service.resolve_conflict(conflict_id, SaveConflictResolution.KEEP_LOCAL)

    assert (tmp_path / "remote" / "psx" / "Game.srm").read_bytes() == b"remote-progress"
    assert service.get_state().active_conflicts[0].conflict_id == conflict_id


class TestOutOfBandRemoteMutation:
    """Manual filesystem divergence on an OWNED dataset requires Full Sync.

    Once the commit protocol owns a dataset, its index is the shared record
    of what is committed. If remote bytes change without the index changing,
    that record is provably stale and must not be allowed to authorize an
    overwrite or a deletion: the operation fails closed and Full Sync
    re-establishes truth from the actual files.
    """

    def test_conflict_resolution_refuses_and_changes_nothing(self, tmp_path: Path):
        """Conflict resolution has its own earlier guard for this case.

        The stored conflict snapshot is rescanned before staging, so a hand
        edit is refused there rather than reaching the index cross-check.
        Both are fail-closed and non-destructive; what matters is that no
        side is mutated and the conflict evidence survives.
        """
        service, conflict_id = _conflict(tmp_path)
        remote = tmp_path / "remote" / "psx" / "Game.srm"
        local = tmp_path / "local" / "psx" / "Game.srm"
        mutate_remote_out_of_band(
            tmp_path / "remote", "psx/Game.srm", b"edited-by-hand"
        )

        with pytest.raises(SaveSyncVerificationError):
            service.resolve_conflict(conflict_id, SaveConflictResolution.KEEP_LOCAL)

        assert remote.read_bytes() == b"edited-by-hand"
        assert local.read_bytes() == b"local-progress"
        active = service.get_state().active_conflicts
        assert [item.conflict_id for item in active] == [conflict_id]

    def test_force_upload_refuses_against_a_stale_index(self, tmp_path: Path):
        service = _service(tmp_path)
        local = tmp_path / "local" / "psx" / "Game.srm"
        remote = tmp_path / "remote" / "psx" / "Game.srm"
        _write(local, b"base")
        service.full_sync()
        seed_peer_commit(
            service,
            remote_root=tmp_path / "remote",
            relative_path="psx/Game.srm",
            content=b"peer-v2",
        )
        mutate_remote_out_of_band(tmp_path / "remote", "psx/Game.srm", b"base")
        _write(local, b"forced-local")

        preview = service.preview_upload()
        with pytest.raises(SaveSyncVerificationError, match="Run a Full Sync"):
            service.commit_upload(preview)

        assert remote.read_bytes() == b"base"
        assert local.read_bytes() == b"forced-local"

    def test_reconcile_upload_refuses_when_index_is_stale(self, tmp_path: Path):
        """A peer commit later reverted by hand leaves the index ahead of
        the bytes; an upload planned against those bytes must not proceed."""
        service = _service(tmp_path)
        local = tmp_path / "local" / "psx" / "Game.srm"
        remote = tmp_path / "remote" / "psx" / "Game.srm"
        _write(local, b"base")
        service.full_sync()
        seed_peer_commit(
            service,
            remote_root=tmp_path / "remote",
            relative_path="psx/Game.srm",
            content=b"peer-v2",
        )
        # Someone restores the previous file behind the protocol's back.
        mutate_remote_out_of_band(tmp_path / "remote", "psx/Game.srm", b"base")
        _write(local, b"local-v3")
        service.mark_local_dirty("psx/Game.srm")

        with pytest.raises(SaveSyncVerificationError, match="Run a Full Sync"):
            service.reconcile()

        assert remote.read_bytes() == b"base"
        assert local.read_bytes() == b"local-v3"

    def test_full_sync_repairs_the_dataset_and_unblocks_mutation(self, tmp_path: Path):
        service, conflict_id = _conflict(tmp_path)
        mutate_remote_out_of_band(
            tmp_path / "remote", "psx/Game.srm", b"edited-by-hand"
        )
        with pytest.raises(SaveSyncVerificationError):
            service.resolve_conflict(conflict_id, SaveConflictResolution.KEEP_LOCAL)

        service.full_sync()

        # Full Sync is the truth-discovery operation, so it re-establishes an
        # index that matches the real bytes and mutation is possible again.
        current = service.get_state().active_conflicts
        assert current, "the hand edit still diverges from local"
        service.resolve_conflict(
            current[0].conflict_id, SaveConflictResolution.KEEP_LOCAL
        )
        assert (
            tmp_path / "remote" / "psx" / "Game.srm"
        ).read_bytes() == b"local-progress"


def _popup() -> ConflictPopupState:
    state = ConflictPopupState("/romcloud")
    state.step = DISPLAYING
    state.conflict = {"conflict_id": "conflict"}
    return state


def test_popup_uses_central_hold_and_early_release_resets(monkeypatch):
    state = _popup()
    actions = []
    monkeypatch.setattr(state, "_apply", actions.append)

    state.handle_event(InputEvent(Action.CONFIRM, source="controller"))
    state.update(2.5)
    state.handle_event(InputEvent(Action.CONFIRM_RELEASED, source="controller"))
    state.update(1.0)

    assert state.confirm.progress == 0.0
    assert actions == []


def test_popup_hold_resolves_selected_side_and_selection_change_resets(monkeypatch):
    state = _popup()
    actions = []
    monkeypatch.setattr(state, "_apply", actions.append)
    state.handle_event(InputEvent(Action.CONFIRM, source="keyboard"))
    state.update(1.5)

    state.handle_event(InputEvent(Action.DOWN, source="keyboard"))
    assert state.confirm.progress == 0.0
    state.handle_event(InputEvent(Action.CONFIRM, source="keyboard"))
    state.update(3.0)

    assert actions == ["download-remote"]


def test_popup_resolve_later_and_back_need_no_hold(monkeypatch):
    for event in (
        InputEvent(Action.CONFIRM, touch_index=2, source="touch"),
        InputEvent(Action.BACK, source="controller"),
    ):
        state = _popup()
        actions = []
        monkeypatch.setattr(state, "_apply", actions.append)
        state.handle_event(event)
        assert actions == ["resolve-later"]


def test_focused_entrypoint_does_not_launch_main_gui(monkeypatch):
    from ports_gfx import __main__ as entrypoint
    from ports_gfx import savesync_conflict_popup

    calls = []
    monkeypatch.setenv("ROMCLOUD_BIN", "/installed/bin/romcloud")
    monkeypatch.setattr(
        savesync_conflict_popup,
        "run_conflict_popup",
        lambda binary: calls.append(binary) or 0,
    )

    assert entrypoint.main(["--savesync-conflicts"]) == 0
    assert calls == ["/installed/bin/romcloud"]


def test_popup_focus_wait_raises_until_keyboard_focus_is_observed():
    now = 0.0
    focus_values = iter((False, False, True))
    requests = []
    records = []
    pygame = SimpleNamespace(
        event=SimpleNamespace(pump=lambda: None),
        key=SimpleNamespace(get_focused=lambda: next(focus_values)),
        display=SimpleNamespace(get_active=lambda: True),
    )

    def clock():
        return now

    def sleep(seconds):
        nonlocal now
        now += seconds

    assert _acquire_popup_focus(
        pygame,
        lambda event, **fields: records.append((event, fields)),
        timeout=1.0,
        clock=clock,
        sleep=sleep,
        request_focus=lambda: requests.append(True) or "requested",
    )
    assert len(requests) == 2
    assert records[0][0] == "conflict_focus_wait_started"
    assert records[-1][0] == "conflict_focus_acquired"


def test_popup_focus_timeout_is_bounded_and_reported():
    now = 0.0
    requests = []
    records = []
    pygame = SimpleNamespace(
        event=SimpleNamespace(pump=lambda: None),
        key=SimpleNamespace(get_focused=lambda: False),
        display=SimpleNamespace(get_active=lambda: True),
    )

    def clock():
        return now

    def sleep(seconds):
        nonlocal now
        now += seconds

    assert not _acquire_popup_focus(
        pygame,
        lambda event, **fields: records.append((event, fields)),
        timeout=0.2,
        clock=clock,
        sleep=sleep,
        request_focus=lambda: requests.append(True) or "requested",
    )
    assert 0 < len(requests) < 10
    assert records[-1][0] == "conflict_focus_timed_out"
    assert records[-1][1]["elapsed"] == pytest.approx(0.2)


def test_successful_action_loads_next_conflict_in_same_window(monkeypatch):
    from ports_gfx import savesync_conflict_popup

    class _FinishedRunner:
        is_finished = True

        def poll(self):
            return []

    state = _popup()
    state.step = APPLYING
    state._runner = _FinishedRunner()  # type: ignore[assignment]
    starts = []
    monkeypatch.setattr(
        savesync_conflict_popup,
        "operation_result",
        lambda _runner: BackendResult(ok=True, data={"remaining": 1}),
    )
    monkeypatch.setattr(
        state,
        "_start_operation",
        lambda action, payload=None: starts.append((action, payload)),
    )

    state.poll()

    assert state.step == LOADING
    assert state.conflict == {}
    assert starts == [("savesync-conflicts", {"source": "automatic"})]


def test_manual_resolver_advances_same_component_without_reloading(monkeypatch):
    from ports_gfx import savesync_conflict_popup

    class _FinishedRunner:
        is_finished = True

        def poll(self):
            return []

    state = ConflictPopupState("/romcloud", source="manual")
    state.step = APPLYING
    state.conflict = {"conflict_id": "one"}
    state._conflicts = [  # noqa: SLF001 - exact in-window queue behavior
        {"conflict_id": "one"},
        {"conflict_id": "two"},
    ]
    state._runner = _FinishedRunner()  # type: ignore[assignment]
    starts = []
    monkeypatch.setattr(
        savesync_conflict_popup,
        "operation_result",
        lambda _runner: BackendResult(ok=True, data={"remaining": 0}),
    )
    monkeypatch.setattr(
        state,
        "_start_operation",
        lambda action, payload=None: starts.append((action, payload)),
    )

    state.poll()

    assert state.step == DISPLAYING
    assert state.conflict == {"conflict_id": "two"}
    assert starts == []
