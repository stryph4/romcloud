from __future__ import annotations

import json
from pathlib import Path

from romcloud.web.browser_runtime import managed_browser, remove_managed_runtime, runtime_root, runtime_status


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
