"""Modification-time evidence shown while resolving a SaveSync conflict.

Everything here is presentation metadata: no test may assert that a timestamp
influences conflict classification, winner selection, or any transaction.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

from ports_gfx.savesync_conflict_popup import (
    ACTION_LABELS,
    DISPLAYING,
    UNKNOWN_TIMESTAMP,
    ConflictPopupState,
    action_rects,
    comparison_line,
    conflict_detail_lines,
    format_local_timestamp,
)
from ports_gfx.actions import Action
from ports_gfx.input_manager import InputEvent
from ports_gfx.layout import compute_layout
from romcloud.core.models.savesync import (
    SaveArtifact,
    SaveConflictRecord,
    SaveGroupSnapshot,
)
from romcloud.core.storage import ProviderCapabilities, StorageProvider
from romcloud.services.saves import SaveSyncService

_PSX_PATH = "psx/Game.srm"
_MEMCARD_PATH = "duckstation/memcards/Tony Hawk's Pro Skater 2 (USA)_1.mcd"
_FOLDER_CARD_PATH = "ps2/pcsx2/Mcd001/_pcsx2_superblock"


class _Provider(StorageProvider):
    @property
    def provider_id(self) -> str:
        return "test"

    @property
    def capabilities(self):
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


def _write(path: Path, content: bytes, *, mtime: float | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    if mtime is not None:
        os.utime(path, (mtime, mtime))


def _snapshot(relative_path: str, layout_id: str, size: int) -> SaveGroupSnapshot:
    return SaveGroupSnapshot(
        group_id=relative_path,
        layout_id=layout_id,
        artifacts=(SaveArtifact(relative_path, size, "0" * 64),),
        observed_at="2026-01-01T00:00:00Z",
    )


def _record(relative_path: str, layout_id: str, size: int = 4) -> SaveConflictRecord:
    snapshot = _snapshot(relative_path, layout_id, size)
    return SaveConflictRecord(
        conflict_id="conflict-1",
        group_id=snapshot.group_id,
        layout_id=layout_id,
        detected_at="2026-01-02T00:00:00Z",
        last_seen_at="2026-01-02T00:00:00Z",
        baseline=None,
        local=snapshot,
        remote=snapshot,
    )


def _both_sides(tmp_path: Path, *, local_mtime: float, remote_mtime: float):
    service = _service(tmp_path)
    _write(tmp_path / "local" / _PSX_PATH, b"aaaa", mtime=local_mtime)
    _write(tmp_path / "remote" / _PSX_PATH, b"bbbb", mtime=remote_mtime)
    return service, _record(_PSX_PATH, "retroarch-root-psx")


class TestServiceEvidence:
    def test_both_timestamps_come_from_the_physical_files(self, tmp_path: Path):
        service, conflict = _both_sides(
            tmp_path, local_mtime=1_757_362_440.0, remote_mtime=1_757_362_020.0
        )

        evidence = service.conflict_modification_evidence(conflict)

        assert evidence["local"]["modified_epoch"] == (
            tmp_path / "local" / _PSX_PATH
        ).stat().st_mtime
        assert evidence["remote"]["modified_epoch"] == (
            tmp_path / "remote" / _PSX_PATH
        ).stat().st_mtime
        assert evidence["local"]["timestamp_source"] == "local-filesystem"
        assert evidence["remote"]["timestamp_source"].startswith("remote-provider:")

    def test_local_newer(self, tmp_path: Path):
        service, conflict = _both_sides(
            tmp_path, local_mtime=2_000_000_000.0, remote_mtime=1_999_999_000.0
        )

        evidence = service.conflict_modification_evidence(conflict)

        assert (
            evidence["local"]["modified_epoch"] > evidence["remote"]["modified_epoch"]
        )

    def test_remote_newer(self, tmp_path: Path):
        service, conflict = _both_sides(
            tmp_path, local_mtime=1_999_999_000.0, remote_mtime=2_000_000_000.0
        )

        evidence = service.conflict_modification_evidence(conflict)

        assert (
            evidence["remote"]["modified_epoch"] > evidence["local"]["modified_epoch"]
        )

    def test_equal_timestamps(self, tmp_path: Path):
        service, conflict = _both_sides(
            tmp_path, local_mtime=2_000_000_000.0, remote_mtime=2_000_000_000.0
        )

        evidence = service.conflict_modification_evidence(conflict)

        assert (
            evidence["local"]["modified_epoch"]
            == evidence["remote"]["modified_epoch"]
        )

    def test_missing_remote_file_reports_unknown_without_borrowing_local(
        self, tmp_path: Path
    ):
        service = _service(tmp_path)
        _write(tmp_path / "local" / _PSX_PATH, b"aaaa", mtime=2_000_000_000.0)
        conflict = _record(_PSX_PATH, "retroarch-root-psx")

        evidence = service.conflict_modification_evidence(conflict)

        assert evidence["local"]["modified_epoch"] == 2_000_000_000.0
        assert evidence["remote"]["modified_epoch"] is None
        assert evidence["remote"]["timestamp_source"] == ""

    def test_both_sides_missing_report_unknown(self, tmp_path: Path):
        service = _service(tmp_path)
        conflict = _record(_PSX_PATH, "retroarch-root-psx")

        evidence = service.conflict_modification_evidence(conflict)

        assert evidence["local"]["modified_epoch"] is None
        assert evidence["remote"]["modified_epoch"] is None

    def test_duckstation_card_is_labelled_as_a_container_timestamp(
        self, tmp_path: Path
    ):
        service = _service(tmp_path)
        _write(tmp_path / "local" / _MEMCARD_PATH, b"x" * 16, mtime=2_000_000_000.0)
        _write(tmp_path / "remote" / _MEMCARD_PATH, b"y" * 16, mtime=1_999_999_580.0)
        conflict = _record(_MEMCARD_PATH, "duckstation-memory-cards", size=16)

        evidence = service.conflict_modification_evidence(conflict)

        # No container adapter exposes a logical per-save time, so the card's
        # own mtime must never be presented as a save modification time.
        assert evidence["local"]["timestamp_kind"] == "container"
        assert evidence["remote"]["timestamp_kind"] == "container"
        assert evidence["local"]["modified_epoch"] == 2_000_000_000.0

    def test_pcsx2_folder_card_is_labelled_as_a_container_timestamp(
        self, tmp_path: Path
    ):
        service = _service(tmp_path)
        _write(tmp_path / "local" / _FOLDER_CARD_PATH, b"z", mtime=2_000_000_000.0)
        conflict = _record(_FOLDER_CARD_PATH, "pcsx2-folder-memory-cards", size=1)

        evidence = service.conflict_modification_evidence(conflict)

        assert evidence["local"]["timestamp_kind"] == "container"

    def test_plain_save_file_is_labelled_as_a_file_timestamp(self, tmp_path: Path):
        service, conflict = _both_sides(
            tmp_path, local_mtime=2_000_000_000.0, remote_mtime=2_000_000_000.0
        )

        evidence = service.conflict_modification_evidence(conflict)

        assert evidence["local"]["timestamp_kind"] == "file"
        assert evidence["remote"]["timestamp_kind"] == "file"

    def test_evidence_lookup_does_not_change_conflict_state(self, tmp_path: Path):
        service = _service(tmp_path)
        local = tmp_path / "local" / _PSX_PATH
        remote = tmp_path / "remote" / _PSX_PATH
        _write(local, b"base")
        service.full_sync()
        _write(local, b"local-progress")
        _write(remote, b"remote-progress")
        service.reconcile()
        before = service.get_state()
        conflict = before.active_conflicts[0]

        service.conflict_modification_evidence(conflict)

        after = service.get_state()
        assert after == before
        assert [item.conflict_id for item in after.active_conflicts] == [
            conflict.conflict_id
        ]
        assert local.read_bytes() == b"local-progress"
        assert remote.read_bytes() == b"remote-progress"


class TestPromptPayload:
    def test_prompt_dict_carries_both_timestamps_to_the_graphical_ui(
        self, tmp_path: Path
    ):
        from romcloud.cli.commands.uidata import _conflict_prompt_dict

        service, conflict = _both_sides(
            tmp_path, local_mtime=2_000_000_000.0, remote_mtime=1_999_999_580.0
        )

        payload = _conflict_prompt_dict(conflict, service)

        assert payload["local"]["modified_epoch"] == 2_000_000_000.0
        assert payload["remote"]["modified_epoch"] == 1_999_999_580.0
        assert payload["local"]["timestamp_kind"] == "file"
        assert payload["local"]["artifact_count"] == 1
        assert (
            conflict_detail_lines(payload)[8] == "Local is 7 minutes newer"
        )

    def test_prompt_dict_without_a_service_omits_timestamps(self, tmp_path: Path):
        from romcloud.cli.commands.uidata import _conflict_prompt_dict

        payload = _conflict_prompt_dict(_record(_PSX_PATH, "retroarch-root-psx"))

        assert "modified_epoch" not in payload["local"]
        assert conflict_detail_lines(payload)[3] == f"  Modified: {UNKNOWN_TIMESTAMP}"


class TestTimestampFormatting:
    def test_renders_in_the_supplied_local_time_zone(self):
        # 2026-09-08T20:14:00 local, whatever the machine time zone is.
        captured = []

        def localtime(value: float):
            captured.append(value)
            return time.struct_time((2026, 9, 8, 20, 14, 0, 1, 251, 0))

        assert (
            format_local_timestamp(1_757_362_440.0, localtime=localtime)
            == "Sep 8, 2026 8:14 PM"
        )
        assert captured == [1_757_362_440.0]

    def test_midnight_and_noon_use_twelve_hour_clock(self):
        midnight = lambda _: time.struct_time((2026, 1, 1, 0, 5, 0, 3, 1, 0))  # noqa: E731
        noon = lambda _: time.struct_time((2026, 1, 1, 12, 5, 0, 3, 1, 0))  # noqa: E731

        assert format_local_timestamp(0.0, localtime=midnight) == "Jan 1, 2026 12:05 AM"
        assert format_local_timestamp(0.0, localtime=noon) == "Jan 1, 2026 12:05 PM"

    def test_missing_timestamp_is_unknown_not_invented(self):
        assert format_local_timestamp(None) == UNKNOWN_TIMESTAMP
        assert format_local_timestamp("2026-09-08") == UNKNOWN_TIMESTAMP
        assert format_local_timestamp(True) == UNKNOWN_TIMESTAMP


class TestComparisonLine:
    def test_local_newer_is_stated_neutrally(self):
        line = comparison_line(
            {"modified_epoch": 1_757_362_440.0},
            {"modified_epoch": 1_757_362_020.0},
        )

        assert line == "Local is 7 minutes newer"
        assert "recommend" not in line.lower()
        assert "best" not in line.lower()

    def test_remote_newer(self):
        assert (
            comparison_line(
                {"modified_epoch": 1_757_362_020.0},
                {"modified_epoch": 1_757_369_220.0},
            )
            == "Remote is 2 hours newer"
        )

    def test_equal_timestamps_do_not_claim_a_newer_side(self):
        line = comparison_line(
            {"modified_epoch": 1_757_362_440.0},
            {"modified_epoch": 1_757_362_440.0},
        )

        assert "newer" not in line

    def test_unknown_timestamp_suppresses_the_comparison(self):
        assert comparison_line({"modified_epoch": None}, {"modified_epoch": 1.0}) == ""
        assert comparison_line({"modified_epoch": 1.0}, {}) == ""


class TestConflictDetailLines:
    def _duckstation_conflict(self) -> dict:
        return {
            "group_label": "duckstation/memcards/Tony Hawk's Pro Skater 2 (USA)_1",
            "layout_id": "duckstation-memory-cards",
            "local": {
                "artifact_count": 1,
                "total_bytes": 131072,
                "modified_epoch": 1_757_362_440.0,
                "timestamp_kind": "container",
            },
            "remote": {
                "artifact_count": 1,
                "total_bytes": 131072,
                "modified_epoch": 1_757_362_020.0,
                "timestamp_kind": "container",
            },
        }

    def _localtime(self, base: float):
        def localtime(value: float):
            minute = int((value - 1_757_362_020.0) // 60)
            return time.struct_time((2026, 9, 8, 20, 7 + minute, 0, 1, 251, 0))

        return localtime

    def test_duckstation_example_shows_both_sides_with_useful_timestamps(self):
        lines = conflict_detail_lines(
            self._duckstation_conflict(), localtime=self._localtime(0)
        )

        assert lines[0] == (
            "duckstation/memcards/Tony Hawk's Pro Skater 2 (USA)_1"
        )
        assert lines[1] == "Layout: duckstation-memory-cards"
        assert lines[2] == "Local Save"
        assert lines[3] == "  Container modified: Sep 8, 2026 8:14 PM"
        assert lines[4] == "  1 file, 128.0 KiB"
        assert lines[5] == "Remote Save"
        assert lines[6] == "  Container modified: Sep 8, 2026 8:07 PM"
        assert lines[7] == "  1 file, 128.0 KiB"
        assert lines[8] == "Local is 7 minutes newer"

    def test_container_timestamps_are_never_labelled_as_save_times(self):
        lines = conflict_detail_lines(
            self._duckstation_conflict(), localtime=self._localtime(0)
        )

        assert not any(line.strip().startswith("Modified:") for line in lines)

    def test_plain_file_conflict_uses_the_modified_label(self):
        lines = conflict_detail_lines(
            {
                "group_label": "psx/Game",
                "layout_id": "retroarch-root-psx",
                "local": {
                    "artifact_count": 2,
                    "total_bytes": 2048,
                    "modified_epoch": 1_757_362_440.0,
                    "timestamp_kind": "file",
                },
                "remote": {"artifact_count": 2, "total_bytes": 2048},
            },
            localtime=self._localtime(0),
        )

        assert lines[3] == "  Modified: Sep 8, 2026 8:14 PM"
        assert lines[4] == "  2 files, 2.0 KiB"
        assert lines[6] == f"  Modified: {UNKNOWN_TIMESTAMP}"
        # One-sided evidence must not produce a comparison.
        assert len(lines) == 8

    def test_backward_compatible_payload_without_timestamps(self):
        lines = conflict_detail_lines(
            {"group_id": "psx/Game", "layout_id": "retroarch-root-psx"}
        )

        assert lines[3] == f"  Modified: {UNKNOWN_TIMESTAMP}"
        assert lines[6] == f"  Modified: {UNKNOWN_TIMESTAMP}"

    def test_no_conflict_produces_no_lines(self):
        assert conflict_detail_lines({}) == ()

    def test_lines_stay_readable_on_a_controller(self):
        lines = conflict_detail_lines(
            self._duckstation_conflict(), localtime=self._localtime(0)
        )

        # The save/layout name is never truncated; the derived lines stay short.
        assert all(len(line) <= 64 for line in lines[1:])


class TestConflictScreenLayout:
    def _layout(self, w: int = 1280, h: int = 800):
        return compute_layout(w, h, len(ACTION_LABELS))

    def test_actions_stay_below_the_detail_block_and_inside_the_screen(self):
        for width, height in ((1280, 720), (1280, 800), (1920, 1080), (3840, 2160)):
            layout = self._layout(width, height)
            line_h = layout.fonts.body + 6
            rects = action_rects(layout, 9)

            assert len(rects) == len(ACTION_LABELS)
            assert rects[0].y >= layout.navigation_rect.y + line_h * 9
            assert rects[-1].bottom <= layout.hint_rect.y
            assert all(rect.h >= 44 for rect in rects)
            assert all(rect.x >= layout.safe_area.x for rect in rects)
            assert all(rect.right <= layout.safe_area.right for rect in rects)

    def test_rects_never_overlap(self):
        rects = action_rects(self._layout(), 9)

        for first, second in zip(rects, rects[1:]):
            assert not first.intersects(second)

    def test_short_payload_keeps_the_previous_geometry(self):
        layout = self._layout()

        assert action_rects(layout, 0) == action_rects(layout, 4)


class TestFocusAndHoldToConfirmUnchanged:
    def _state(self) -> ConflictPopupState:
        state = ConflictPopupState("/romcloud")
        state.step = DISPLAYING
        state.conflict = {
            "conflict_id": "conflict-1",
            "group_label": "psx/Game",
            "layout_id": "retroarch-root-psx",
            "local": {
                "artifact_count": 1,
                "total_bytes": 4,
                "modified_epoch": 2_000_000_000.0,
                "timestamp_kind": "file",
            },
            "remote": {"artifact_count": 1, "total_bytes": 4},
        }
        return state

    def test_selection_still_moves_and_resets_the_hold(self):
        state = self._state()
        state.confirm.press()

        state.handle_event(InputEvent(action=Action.DOWN))

        assert state.selected_index == 1
        assert state.confirm.progress == 0.0

    def test_hold_to_confirm_still_requires_a_full_hold(self):
        state = self._state()
        started: list[tuple[str, dict]] = []
        state._start_operation = lambda action, payload=None: started.append(  # type: ignore[method-assign]
            (action, payload or {})
        )

        state.handle_event(InputEvent(action=Action.CONFIRM))
        state.update(0.2)
        assert started == []

        state.update(5.0)
        assert started[0][0] == "savesync-conflict-action"
        assert started[0][1]["action"] == "upload-local"

    def test_detail_lines_track_the_displayed_conflict(self):
        state = self._state()

        assert len(state.detail_lines) == 8
        assert state.detail_lines[5] == "Remote Save"
