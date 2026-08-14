from __future__ import annotations

from pathlib import Path

from romcloud.web.lifecycle import (
    clear_manager_state,
    manager_runtime_state,
    manager_state_path,
    manager_status,
    read_manager_state,
    start_manager,
    write_manager_state,
)


def test_state_round_trip_is_private_and_reports_running(tmp_path: Path) -> None:
    state = manager_runtime_state(
        host="0.0.0.0",
        port=8765,
        token="secret-token",
        scheme="https",
        pid=42,
    )
    path = write_manager_state(tmp_path, state)

    assert path == manager_state_path(tmp_path)
    assert read_manager_state(tmp_path)["token"] == "secret-token"
    status = manager_status(
        tmp_path,
        pid_alive=lambda pid: pid == 42,
        port_open=lambda host, port: host == "0.0.0.0" and port == 8765,
    )
    assert status["running"] is True
    assert status["url"].startswith("https://")
    assert status["url"].endswith(":8765/")


def test_clear_only_removes_state_owned_by_matching_process(tmp_path: Path) -> None:
    write_manager_state(
        tmp_path,
        manager_runtime_state(
            host="0.0.0.0", port=8765, token="token", scheme="https", pid=7
        ),
    )
    clear_manager_state(tmp_path, pid=8)
    assert manager_state_path(tmp_path).exists()
    clear_manager_state(tmp_path, pid=7)
    assert not manager_state_path(tmp_path).exists()


def test_start_surfaces_existing_service_without_spawning(tmp_path: Path) -> None:
    existing = {"running": True, "url": "https://batocera.local:8765/", "token": "t"}
    spawned = []
    result = start_manager(
        "/opt/romcloud/bin/romcloud",
        tmp_path,
        status_reader=lambda: existing,
        popen=lambda *args, **kwargs: spawned.append((args, kwargs)),
    )
    assert result == {**existing, "started": False}
    assert spawned == []


def test_start_spawns_existing_manager_command_and_returns_recorded_state(tmp_path: Path) -> None:
    statuses = iter(
        [
            {"running": False},
            {
                "running": True,
                "url": "https://batocera.local:8765/",
                "token": "generated",
                "pid": 123,
            },
        ]
    )
    captured = {}

    class Process:
        def poll(self):
            return None

    def popen(argv, **kwargs):
        captured["argv"] = argv
        captured["kwargs"] = kwargs
        return Process()

    result = start_manager(
        "/opt/romcloud/bin/romcloud",
        tmp_path,
        status_reader=lambda: next(statuses),
        port_open=lambda host, port: False,
        popen=popen,
        sleep=lambda seconds: None,
    )

    assert captured["argv"][:2] == [
        "/opt/romcloud/bin/romcloud",
        "manager",
    ]
    assert "--token" in captured["argv"]
    assert "--quiet" in captured["argv"]
    assert captured["kwargs"]["start_new_session"] is True
    assert result["running"] is True and result["started"] is True
