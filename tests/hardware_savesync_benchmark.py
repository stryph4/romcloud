"""Safety boundary for the opt-in SaveSync CIFS hardware benchmark."""

from __future__ import annotations

import os
import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path


MARKER_NAME = ".romcloud-hardware-benchmark"
RUNS_NAME = "runs"


class HardwareBenchmarkSafetyError(RuntimeError):
    pass


@dataclass(frozen=True)
class HardwareBenchmarkRun:
    mount_root: Path
    runs_root: Path
    run_root: Path


def prepare_hardware_benchmark_run(environment: dict[str, str] | None = None) -> HardwareBenchmarkRun:
    env = os.environ if environment is None else environment
    if env.get("ROMCLOUD_RUN_HARDWARE_TESTS") != "1":
        raise HardwareBenchmarkSafetyError(
            "ROMCLOUD_RUN_HARDWARE_TESTS must be exactly 1"
        )
    configured = env.get("ROMCLOUD_HARDWARE_BENCH_REMOTE", "").strip()
    if not configured:
        raise HardwareBenchmarkSafetyError(
            "ROMCLOUD_HARDWARE_BENCH_REMOTE is required"
        )
    mount_root = Path(configured)
    try:
        mount_root = mount_root.resolve(strict=True)
    except OSError as exc:
        raise HardwareBenchmarkSafetyError(
            f"hardware benchmark mount does not exist: {mount_root}"
        ) from exc
    if not mount_root.is_dir() or not os.access(mount_root, os.W_OK):
        raise HardwareBenchmarkSafetyError(
            f"hardware benchmark mount is not a writable directory: {mount_root}"
        )
    marker = mount_root / MARKER_NAME
    if not marker.is_file() or marker.is_symlink():
        raise HardwareBenchmarkSafetyError(
            f"hardware benchmark safety marker is missing: {marker}"
        )

    runs_candidate = mount_root / RUNS_NAME
    try:
        runs_candidate.mkdir(mode=0o700, exist_ok=True)
        runs_root = runs_candidate.resolve(strict=True)
    except OSError as exc:
        raise HardwareBenchmarkSafetyError(
            f"hardware benchmark mount is not writable: {mount_root}"
        ) from exc
    if runs_root.parent != mount_root or not runs_root.is_dir():
        raise HardwareBenchmarkSafetyError(
            f"hardware benchmark runs directory escapes the mount: {runs_candidate}"
        )

    run_candidate = runs_root / uuid.uuid4().hex
    try:
        run_candidate.mkdir(mode=0o700)
        run_root = run_candidate.resolve(strict=True)
    except OSError as exc:
        raise HardwareBenchmarkSafetyError(
            f"hardware benchmark runs directory is not writable: {runs_root}"
        ) from exc
    if run_root.parent != runs_root:
        raise HardwareBenchmarkSafetyError(
            f"hardware benchmark run directory escapes runs/: {run_candidate}"
        )
    return HardwareBenchmarkRun(mount_root, runs_root, run_root)


def cleanup_hardware_benchmark_run(run: HardwareBenchmarkRun) -> None:
    """Remove exactly one owned run directory, refusing every wider target."""
    runs_root = run.runs_root.resolve(strict=True)
    run_root = run.run_root.resolve(strict=True)
    if runs_root.parent != run.mount_root.resolve(strict=True):
        raise HardwareBenchmarkSafetyError("benchmark runs root is no longer beneath the mount")
    if run_root == runs_root or run_root.parent != runs_root:
        raise HardwareBenchmarkSafetyError(
            f"refusing recursive cleanup outside the unique run directory: {run_root}"
        )
    shutil.rmtree(run_root)
