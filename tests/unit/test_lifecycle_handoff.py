from __future__ import annotations

import shutil
from pathlib import Path

from romcloud.lifecycle.handoff import stage_lifecycle_helper


def test_staged_helper_lives_outside_runtime_and_survives_runtime_deletion(tmp_path: Path) -> None:
    captured: dict[str, object] = {}

    class Process:
        def poll(self):
            return None

    def popen(argv, **kwargs):
        captured["argv"] = argv
        captured["kwargs"] = kwargs
        Path(argv[7]).write_text("ready\n")
        return Process()

    home = tmp_path / "installed"
    home.mkdir()
    (home / "runtime").write_text("installed payload")
    result = stage_lifecycle_helper(
        operation="uninstall",
        config_path=home / "config" / "romcloud.toml",
        wait_for_pid=4321,
        popen=popen,
    )
    argv = captured["argv"]
    stage = Path(argv[6])
    assert stage.is_dir()
    assert not stage.is_relative_to(home)

    shutil.rmtree(home)

    assert (stage / "romcloud" / "lifecycle" / "detached.py").is_file()
    assert Path(argv[0]).name.startswith("python")
    assert result == Path(argv[8])
    shutil.rmtree(stage)
