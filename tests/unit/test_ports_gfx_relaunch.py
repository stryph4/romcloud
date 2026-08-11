"""Regression tests for the GUI update-to-relaunch handoff."""

from __future__ import annotations

from pathlib import Path

from ports_gfx.relaunch import (
    GuiRelaunchCoordinator,
    canonical_graphical_launcher,
    relaunch_failure_message,
)


def test_canonical_launcher_is_installer_managed_ports_wrapper() -> None:
    launcher = canonical_graphical_launcher(
        "/userdata/system/romcloud/bin/romcloud"
    )

    assert launcher == Path("/userdata/system/romcloud/bin/romcloud-ports")


def test_success_requests_exactly_one_detached_replacement_process(tmp_path) -> None:
    coordinator = GuiRelaunchCoordinator(
        "/userdata/system/romcloud/bin/romcloud"
    )
    calls: list[tuple[list[str], dict]] = []

    def fake_popen(argv, **kwargs):
        calls.append((argv, kwargs))
        return object()

    assert coordinator.mark_update_succeeded(progress_complete=True) is True
    assert coordinator.terminal is True

    first = coordinator.launch_once(
        popen=fake_popen,
        failure_log_path=tmp_path / "relaunch.log",
    )
    second = coordinator.launch_once(
        popen=fake_popen,
        failure_log_path=tmp_path / "relaunch.log",
    )

    assert first.attempted is True
    assert first.launched is True
    assert second.attempted is False
    assert calls == [
        (
            ["/userdata/system/romcloud/bin/romcloud-ports"],
            {"close_fds": True, "start_new_session": True},
        )
    ]


def test_relaunch_cannot_be_requested_before_update_completion(tmp_path) -> None:
    coordinator = GuiRelaunchCoordinator("/opt/romcloud/bin/romcloud")
    calls: list[list[str]] = []

    assert coordinator.mark_update_succeeded(progress_complete=False) is False
    result = coordinator.launch_once(
        popen=lambda argv, **_kwargs: calls.append(argv),
        failure_log_path=tmp_path / "relaunch.log",
    )

    assert coordinator.terminal is False
    assert result.attempted is False
    assert calls == []


def test_failed_update_never_requests_relaunch(tmp_path) -> None:
    coordinator = GuiRelaunchCoordinator("/opt/romcloud/bin/romcloud")
    calls: list[list[str]] = []

    coordinator.mark_update_failed()
    result = coordinator.launch_once(
        popen=lambda argv, **_kwargs: calls.append(argv),
        failure_log_path=tmp_path / "relaunch.log",
    )

    assert coordinator.terminal is False
    assert result.attempted is False
    assert calls == []


def test_launch_failure_is_persisted_and_remains_terminal(tmp_path) -> None:
    coordinator = GuiRelaunchCoordinator("/opt/romcloud/bin/romcloud")
    log_path = tmp_path / "logs" / "gui-relaunch.log"

    def fail_popen(_argv, **_kwargs):
        raise OSError("permission denied\nwhile starting wrapper")

    coordinator.mark_update_succeeded(progress_complete=True)
    result = coordinator.launch_once(
        popen=fail_popen,
        failure_log_path=log_path,
    )

    assert result.attempted is True
    assert result.launched is False
    assert coordinator.terminal is True
    assert coordinator.relaunch_pending is False
    assert "permission denied while starting wrapper" in result.error
    assert "update succeeded; GUI relaunch failed" in log_path.read_text()
    assert "Reopen ROMCloud from the Batocera Ports menu" in relaunch_failure_message(result)
