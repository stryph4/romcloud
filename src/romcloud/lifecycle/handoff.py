"""Stage a lifecycle worker outside the installed runtime before GUI exit."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path


def stage_lifecycle_helper(
    *,
    operation: str,
    config_path: Path,
    wait_for_pid: int,
    popen=subprocess.Popen,
    timeout: float = 15.0,
) -> Path:
    """Copy the Python lifecycle payload, launch it, and await readiness.

    No installed path is mutated here.  Returning means the child has loaded
    the staged package and is waiting for the GUI PID to disappear.
    """

    if operation not in {"uninstall", "purge"}:
        raise ValueError(f"Unsupported lifecycle operation: {operation}")
    if wait_for_pid <= 1 or wait_for_pid == os.getpid():
        raise RuntimeError("Invalid graphical lifecycle handoff PID")

    stage = Path(tempfile.mkdtemp(prefix="romcloud-lifecycle-"))
    ready = stage / "ready"
    result = Path(tempfile.gettempdir()) / f"romcloud-lifecycle-result-{uuid.uuid4().hex}.json"
    package_root = Path(__file__).resolve().parents[1]
    try:
        shutil.copytree(
            package_root,
            stage / "romcloud",
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo"),
        )
        env = os.environ.copy()
        env["PYTHONPATH"] = os.fspath(stage)
        process = popen(
            [
                sys.executable,
                "-m",
                "romcloud.lifecycle.detached",
                operation,
                os.fspath(config_path),
                str(wait_for_pid),
                os.fspath(stage),
                os.fspath(ready),
                os.fspath(result),
            ],
            cwd=tempfile.gettempdir(),
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=os.name == "posix",
        )
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if ready.is_file():
                return result
            if process.poll() is not None:
                raise RuntimeError("Staged lifecycle helper exited before readiness")
            time.sleep(0.05)
        process.terminate()
        raise RuntimeError("Staged lifecycle helper did not become ready")
    except Exception:
        shutil.rmtree(stage, ignore_errors=True)
        raise
