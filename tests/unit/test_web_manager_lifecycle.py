from __future__ import annotations

from pathlib import Path

from romcloud.web.lifecycle import (
    clear_manager_state,
    manager_runtime_state,
    manager_state_path,
    manager_status,
    manager_instance_lock,
    read_manager_state,
    start_manager,
    stop_manager,
    write_manager_state,
)
import pytest


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
    assert "token" not in status
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
    assert "--token" not in captured["argv"]
    assert captured["kwargs"]["env"]["ROMCLOUD_MANAGER_TOKEN"]
    assert captured["kwargs"]["env"]["ROMCLOUD_MANAGER_INSTANCE"]
    assert "--quiet" in captured["argv"]
    assert captured["kwargs"]["start_new_session"] is True
    assert result["running"] is True and result["started"] is True


def test_singleton_lock_rejects_a_second_owner(tmp_path: Path) -> None:
    with manager_instance_lock(tmp_path):
        with pytest.raises(RuntimeError, match="already running"):
            with manager_instance_lock(tmp_path):
                pass


def test_stop_is_bounded_and_only_signals_verified_owner(tmp_path: Path) -> None:
    write_manager_state(
        tmp_path,
        manager_runtime_state(
            host="0.0.0.0",
            port=8765,
            token="token",
            scheme="https",
            pid=77,
            instance_id="owned-instance",
        ),
    )
    signals = []
    alive = [True, False]
    stopped = stop_manager(
        tmp_path,
        kill=lambda pid, sig: signals.append((pid, sig)),
        pid_alive=lambda pid: alive.pop(0) if alive else False,
        owned_pid=lambda pid, instance: pid == 77 and instance == "owned-instance",
        sleep=lambda _: None,
    )
    assert stopped is True
    assert signals and signals[0][0] == 77
    assert not manager_state_path(tmp_path).exists()


def test_stop_never_signals_reused_unowned_pid(tmp_path: Path) -> None:
    write_manager_state(
        tmp_path,
        manager_runtime_state(
            host="0.0.0.0", port=8765, token="token", scheme="https", pid=88
        ),
    )
    signals = []
    assert not stop_manager(
        tmp_path,
        kill=lambda pid, sig: signals.append((pid, sig)),
        pid_alive=lambda pid: True,
        owned_pid=lambda pid, instance: False,
    )
    assert signals == []
