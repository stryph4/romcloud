from __future__ import annotations

from pathlib import Path

import pytest

from tests.hardware_savesync_benchmark import (
    HardwareBenchmarkRun,
    HardwareBenchmarkSafetyError,
    cleanup_hardware_benchmark_run,
    prepare_hardware_benchmark_run,
)


def _environment(root: Path) -> dict[str, str]:
    return {
        "ROMCLOUD_RUN_HARDWARE_TESTS": "1",
        "ROMCLOUD_HARDWARE_BENCH_REMOTE": str(root),
    }


def test_requires_explicit_opt_in_and_remote_path(tmp_path: Path) -> None:
    with pytest.raises(HardwareBenchmarkSafetyError, match="must be exactly 1"):
        prepare_hardware_benchmark_run({})
    with pytest.raises(HardwareBenchmarkSafetyError, match="is required"):
        prepare_hardware_benchmark_run({"ROMCLOUD_RUN_HARDWARE_TESTS": "1"})


def test_requires_safety_marker(tmp_path: Path) -> None:
    with pytest.raises(HardwareBenchmarkSafetyError, match="marker is missing"):
        prepare_hardware_benchmark_run(_environment(tmp_path))


def test_creates_and_cleans_only_a_unique_run_directory(tmp_path: Path) -> None:
    (tmp_path / ".romcloud-hardware-benchmark").write_text("test\n")
    run = prepare_hardware_benchmark_run(_environment(tmp_path))
    assert run.run_root.parent == tmp_path / "runs"
    assert run.run_root.is_dir()
    cleanup_hardware_benchmark_run(run)
    assert not run.run_root.exists()
    assert (tmp_path / ".romcloud-hardware-benchmark").is_file()
    assert (tmp_path / "runs").is_dir()


def test_cleanup_refuses_mount_root_or_runs_root(tmp_path: Path) -> None:
    marker = tmp_path / ".romcloud-hardware-benchmark"
    marker.write_text("test\n")
    runs = tmp_path / "runs"
    runs.mkdir()
    unsafe = HardwareBenchmarkRun(tmp_path, runs, runs)
    with pytest.raises(HardwareBenchmarkSafetyError, match="refusing recursive cleanup"):
        cleanup_hardware_benchmark_run(unsafe)
    assert marker.is_file()
    assert runs.is_dir()

