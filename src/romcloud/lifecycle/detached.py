"""Entry point for the staged, runtime-independent lifecycle worker."""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import time
from dataclasses import asdict
from pathlib import Path

# Preload every module used after the installed venv/package can disappear.
from romcloud.infrastructure.config import (  # noqa: E402
    AppConfig,
    CacheConfig,
    LoggingConfig,
    SourceConfig,
    load_config_read_only,
)
from romcloud.lifecycle import manage  # noqa: E402
from romcloud.integrations.batocera import game_access  # noqa: E402,F401
from romcloud.web import browser_runtime, lifecycle as browser_lifecycle  # noqa: E402,F401


def _wait_for_exit(pid: int, timeout: float = 30.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        except PermissionError as exc:
            raise RuntimeError("Cannot verify graphical lifecycle handoff") from exc
        time.sleep(0.05)
    raise RuntimeError("Graphical ROMCloud process did not exit; no lifecycle changes were made")


def _paths(config_path: Path) -> tuple[Path, AppConfig, bool]:
    home = config_path.parent.parent
    try:
        return home, load_config_read_only(str(config_path)), True
    except Exception as exc:  # keep the missing-config path non-mutating
        from romcloud.core.exceptions import ConfigurationNotFoundError

        if not isinstance(exc, ConfigurationNotFoundError):
            raise
        unknown = Path("/.__romcloud_missing_config__")
        return home, AppConfig(
            source=SourceConfig(provider="local", rom_root=str(unknown / "source")),
            cache=CacheConfig(path=str(unknown / "cache")),
            local_roms_path=str(unknown / "roms"),
            data_path=str(unknown / "data"),
            logging=LoggingConfig(path=None),
        ), False


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 6:
        return 2
    operation, raw_config, raw_pid, raw_stage, raw_ready, raw_result = args
    if operation not in {"uninstall", "purge"}:
        return 2
    stage = Path(raw_stage)
    ready = Path(raw_ready)
    result = Path(raw_result)
    payload: dict[str, object]
    try:
        pid = int(raw_pid)
        # This is the readiness barrier observed by the still-running GUI.
        ready.write_text("ready\n", encoding="ascii")
        _wait_for_exit(pid)
        home, config, trusted = _paths(Path(raw_config))
        report = (
            manage.uninstall(config=config, romcloud_home=home, config_trusted=trusted)
            if operation == "uninstall"
            else manage.purge(config=config, romcloud_home=home, config_trusted=trusted)
        )
        payload = {"status": "success", "operation": operation, "report": asdict(report)}
        code = 0
    except Exception as exc:  # noqa: BLE001 - durable detached result
        report = getattr(exc, "report", None)
        payload = {
            "status": "failed",
            "operation": operation,
            "error": str(exc),
            "report": asdict(report) if report is not None else None,
        }
        code = 1
    try:
        result.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
    finally:
        os.chdir(tempfile.gettempdir())
        shutil.rmtree(stage, ignore_errors=True)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
