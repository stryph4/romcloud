from __future__ import annotations

import json
from pathlib import Path

import pytest

from romcloud.web.browser_runtime import (
    activate_staged_runtime,
    managed_browser,
    remove_managed_runtime,
    request_managed_install,
    runtime_root,
    runtime_status,
    staging_version_path,
)


def test_absent_runtime_reports_hardware_block_without_creating_files(tmp_path: Path) -> None:
    status = runtime_status(tmp_path / "data")
    assert status["installed"] is False
    assert status["candidate"]["installation_enabled"] is False
    assert "sandbox" in status["candidate"]["blocked_reason"]
    assert not runtime_root(tmp_path / "data").exists()


def test_versioned_manifest_activates_only_executable_inside_owned_root(tmp_path: Path) -> None:
    data = tmp_path / "data"
    root = runtime_root(data)
    executable = root / "versions" / "152.0" / "chrome-linux64" / "chrome"
    executable.parent.mkdir(parents=True)
    executable.write_text("browser")
    executable.chmod(0o755)
    (root / "current.json").write_text(json.dumps({"version": "152.0", "executable": "chrome-linux64/chrome"}))
    assert managed_browser(data) == str(executable)
    assert runtime_status(data)["version"] == "152.0"


def test_remove_deletes_only_managed_browser_root(tmp_path: Path) -> None:
    data = tmp_path / "data"
    root = runtime_root(data); root.mkdir(parents=True)
    (root / "current.json").write_text("{}")
    unrelated = tmp_path / "keep"; unrelated.write_text("yes")
    assert remove_managed_runtime(data)
    assert not root.exists()
    assert unrelated.read_text() == "yes"


def test_manifest_cannot_escape_owned_version_directory(tmp_path: Path) -> None:
    data = tmp_path / "data"; root = runtime_root(data); root.mkdir(parents=True)
    outside = tmp_path / "outside"; outside.write_text("browser"); outside.chmod(0o755)
    (root / "current.json").write_text(json.dumps({"version": "../..", "executable": "outside"}))
    assert managed_browser(data) is None


def test_declined_or_blocked_install_never_mutates_runtime(tmp_path: Path) -> None:
    data = tmp_path / "data"
    assert request_managed_install(accepted=False)["declined"] is True
    with pytest.raises(RuntimeError, match="sandbox"):
        request_managed_install(accepted=True)
    assert not runtime_root(data).exists()


def test_staged_activation_keeps_old_version_for_rollback_and_switches_atomically(tmp_path: Path) -> None:
    data = tmp_path / "data"
    root = runtime_root(data)
    old = root / "versions" / "1" / "chrome"
    old.parent.mkdir(parents=True)
    old.write_text("old")
    old.chmod(0o755)
    (root / "current.json").write_text('{"version":"1","executable":"chrome"}')
    staged = staging_version_path(data, "2") / "chrome"
    staged.parent.mkdir(parents=True)
    staged.write_text("new")
    staged.chmod(0o755)

    result = activate_staged_runtime(
        data,
        version="2",
        executable="chrome",
        smoke_test=lambda _: {"compatible": True, "version": "Chromium 2"},
    )
    assert result["version"] == "2"
    assert managed_browser(data) == str(root / "versions" / "2" / "chrome")
    assert old.read_text() == "old"


def test_failed_stage_smoke_test_leaves_current_runtime_untouched(tmp_path: Path) -> None:
    data = tmp_path / "data"
    root = runtime_root(data)
    old = root / "versions" / "1" / "chrome"
    old.parent.mkdir(parents=True)
    old.write_text("old")
    old.chmod(0o755)
    (root / "current.json").write_text('{"version":"1","executable":"chrome"}')
    staged = staging_version_path(data, "2") / "chrome"
    staged.parent.mkdir(parents=True)
    staged.write_text("bad")
    staged.chmod(0o755)

    with pytest.raises(RuntimeError, match="smoke test"):
        activate_staged_runtime(
            data,
            version="2",
            executable="chrome",
            smoke_test=lambda _: {"compatible": False, "reason": "missing library"},
        )
    assert managed_browser(data) == str(old)
    assert staged.exists()
