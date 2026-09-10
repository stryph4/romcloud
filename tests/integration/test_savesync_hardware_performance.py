"""Opt-in Ubuntu -> kernel CIFS -> NAS SaveSync performance benchmark."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from romcloud.core.save_selection import DEFAULT_SAVE_SELECTION_POLICY
from romcloud.infrastructure import diagnostics
from romcloud.infrastructure.providers.local import WritableLocalFilesystemProvider
from romcloud.services.saves import SaveSyncService
from tests.hardware_savesync_benchmark import (
    HardwareBenchmarkSafetyError,
    cleanup_hardware_benchmark_run,
    prepare_hardware_benchmark_run,
)


pytestmark = [pytest.mark.hardware, pytest.mark.performance]

_BASELINE = bytes(range(256)) * 32
_REMOTE_NEWER = bytes((index * 17 + 29) % 256 for index in range(8192))
_UNRELATED = b"unrelated-gba-save" * 64
_LAYOUT_ID = "retroarch-root-snes"
_ROM_NAME = "Super Metroid.sfc"


def _write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)


def _service(local_root: Path, state_root: Path, remote_data_root: Path) -> SaveSyncService:
    local_root.mkdir(parents=True)
    state_root.mkdir(parents=True)
    return SaveSyncService(
        provider=WritableLocalFilesystemProvider(),
        connectivity_root=str(remote_data_root),
        local_root=str(local_root),
        remote_root=str(remote_data_root / "saves"),
        state_path=state_root / "savesync-state.json",
    )


def _measure(service: SaveSyncService, group_id: str, scenario: str):
    with diagnostics.operation(
        f"hardware targeted gameStart {scenario}",
        subsystem="savesync",
        source="Ubuntu CIFS hardware benchmark",
    ):
        result = service.targeted_game_start_sync({group_id: _LAYOUT_ID})
        timing = diagnostics.current_timing_snapshot()
    payload = {
        "scenario": scenario,
        "status": result.status,
        "reason": result.reason,
        "report": result.report.to_dict() if result.report is not None else None,
        "timing": timing,
    }
    print("ROMCloud SaveSync hardware benchmark " + json.dumps(payload, sort_keys=True))
    return result, timing


@pytest.fixture
def hardware_run():
    if os.environ.get("ROMCLOUD_RUN_HARDWARE_TESTS") != "1":
        pytest.skip("set ROMCLOUD_RUN_HARDWARE_TESTS=1 to run NAS hardware benchmarks")
    try:
        run = prepare_hardware_benchmark_run()
    except HardwareBenchmarkSafetyError as exc:
        pytest.fail(str(exc))
    print(f"ROMCloud SaveSync hardware benchmark run_root={run.run_root}")
    try:
        yield run
    finally:
        cleanup_hardware_benchmark_run(run)


def test_targeted_game_start_clean_and_remote_newer(hardware_run, tmp_path: Path) -> None:
    """Measure the two minimum production-like targeted gameStart scenarios."""
    remote_data_root = hardware_run.run_root
    device_a = _service(tmp_path / "device-a-saves", tmp_path / "device-a-state", remote_data_root)
    local_save = tmp_path / "device-a-saves/snes/Super Metroid.srm"
    unrelated_local = tmp_path / "device-a-saves/gba/Unrelated.srm"
    _write(local_save, _BASELINE)
    _write(unrelated_local, _UNRELATED)
    device_a.full_sync()

    group_id = DEFAULT_SAVE_SELECTION_POLICY.group_id_for_rom(_LAYOUT_ID, _ROM_NAME)
    assert group_id is not None
    clean_result, clean_timing = _measure(device_a, group_id, "clean-current")
    assert clean_result.status == "synchronized"
    assert clean_result.reason == "already-current"
    assert local_save.read_bytes() == _BASELINE
    assert unrelated_local.read_bytes() == _UNRELATED
    assert clean_timing["counters"].get("head_reads", 0) >= 1

    # A second real SaveSyncService represents a peer device. Its own local
    # state is isolated, while its upload uses the same NAS-backed payload,
    # HEAD/shards, journal, lock, staging, promotion and verification stack.
    device_b = _service(tmp_path / "device-b-saves", tmp_path / "device-b-state", remote_data_root)
    peer_save = tmp_path / "device-b-saves/snes/Super Metroid.srm"
    peer_unrelated = tmp_path / "device-b-saves/gba/Unrelated.srm"
    _write(peer_save, _BASELINE)
    _write(peer_unrelated, _UNRELATED)
    device_b.full_sync()
    peer_save.write_bytes(_REMOTE_NEWER)
    device_b.detect_and_mark_local_changes(
        frozenset({_LAYOUT_ID}), changed_since=0.0
    )
    peer_publish = device_b.targeted_game_start_sync({group_id: _LAYOUT_ID})
    assert peer_publish.status == "synchronized"
    assert peer_publish.report is not None
    assert peer_publish.report.uploaded == 1

    remote_save = remote_data_root / "saves/snes/Super Metroid.srm"
    unrelated_remote = remote_data_root / "saves/gba/Unrelated.srm"
    assert remote_save.read_bytes() == _REMOTE_NEWER
    unrelated_before = unrelated_remote.read_bytes()

    remote_result, remote_timing = _measure(device_a, group_id, "remote-newer")
    assert remote_result.status == "synchronized"
    assert remote_result.reason == "reconciled"
    assert remote_result.report is not None
    assert remote_result.report.downloaded == 1
    assert remote_result.report.uploaded == 0
    assert remote_result.report.conflicts == 0
    assert local_save.read_bytes() == _REMOTE_NEWER
    assert unrelated_local.read_bytes() == _UNRELATED
    assert unrelated_remote.read_bytes() == unrelated_before
    assert remote_timing["stages"].get("scan-remote")
    assert remote_timing["stages"].get("final-verify")
    assert remote_timing["counters"].get("remote_manifest_observations", 0) >= 1
