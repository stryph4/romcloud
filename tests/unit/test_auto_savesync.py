from __future__ import annotations

import os
import subprocess
import threading
import time
from contextlib import contextmanager
from pathlib import Path

import pytest
from click.testing import CliRunner

from romcloud.core.capabilities import CapabilityPolicy, OperatingMode
from romcloud.core.models.savesync import SaveGroupCondition
from romcloud.core.save_selection import DEFAULT_SAVE_SELECTION_POLICY
from romcloud.core.storage import StorageProvider
from romcloud.infrastructure import save_transaction
from romcloud.infrastructure import savesync_prompts
from romcloud.infrastructure.config import (
    AppConfig,
    CacheConfig,
    SavesConfig,
    SourceConfig,
    write_config,
)
from romcloud.integrations.batocera import auto_savesync as batocera_auto_savesync
from romcloud.integrations.batocera.auto_savesync import hook_content, install_hook
from romcloud.services.auto_savesync import (
    AutoSaveSyncCoordinator,
    layout_ids_for_session,
)
from romcloud.services.saves import SaveSyncService


class _Provider(StorageProvider):
    def __init__(self) -> None:
        self.reachable = True
        self.reachability_checks = 0

    @property
    def provider_id(self) -> str:
        return "test"

    @property
    def capabilities(self):
        from romcloud.core.storage import ProviderCapabilities

        # This fixture always backs "remote" with a real local directory
        # (see _service() below) — a local-like provider for exercising
        # SaveSync's own reconciliation logic, not provider-capability
        # gating (see test_savesync_capability_gating.py for that).
        return ProviderCapabilities(
            has_filesystem_semantics=True, supports_durable_transactions=True
        )

    def is_reachable(self, root: str) -> bool:
        self.reachability_checks += 1
        return self.reachable

    def list_systems(self, rom_root: str) -> list[str]:
        raise NotImplementedError

    def list_entries(self, rom_root: str, system: str):
        raise NotImplementedError

    def get_size(self, path: str):
        raise NotImplementedError

    def read_text(self, path: str) -> str:
        raise NotImplementedError

    def transfer_to(self, source_path: str, dest_path: str, on_progress=None) -> None:
        raise NotImplementedError


def _write(path: Path, value: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(value)


def _service(
    tmp_path: Path,
    provider: _Provider,
    *,
    capability_policy: CapabilityPolicy | None = None,
    xbox_enabled: bool = False,
) -> SaveSyncService:
    local = tmp_path / "local"
    local.mkdir()
    return SaveSyncService(
        provider=provider,
        connectivity_root=str(tmp_path / "remote-data"),
        local_root=str(local),
        remote_root=str(tmp_path / "remote"),
        state_path=tmp_path / "data" / "savesync-state.json",
        capability_policy=capability_policy,
        xbox_enabled=xbox_enabled,
    )


def _coordinator(tmp_path: Path, service: SaveSyncService) -> AutoSaveSyncCoordinator:
    return AutoSaveSyncCoordinator(
        service,
        data_root=tmp_path / "data",
        enabled=True,
        policy=DEFAULT_SAVE_SELECTION_POLICY,
        quiet_seconds=0,
    )


def test_disabled_coordinator_is_an_immediate_filesystem_and_service_noop(
    tmp_path: Path,
):
    class _UnexpectedService:
        def __getattr__(self, name):
            raise AssertionError(f"disabled Auto SaveSync accessed service.{name}")

    coordinator = AutoSaveSyncCoordinator(
        _UnexpectedService(),  # type: ignore[arg-type]
        data_root=tmp_path / "data",
        quiet_seconds=60,
        enabled=False,
    )

    started = time.monotonic()
    coordinator.game_start(
        system="psx", emulator="libretro", core="pcsx", rom="Game.chd"
    )
    coordinator.game_stop(
        system="psx", emulator="libretro", core="pcsx", rom="Game.chd"
    )
    coordinator.drain_pending()
    coordinator.menu_loop()

    assert time.monotonic() - started < 0.1
    assert not (tmp_path / "data").exists()


def test_disabled_lifecycle_cli_does_not_construct_coordinator(
    tmp_path: Path, monkeypatch
):
    from romcloud.cli.commands import autosync as autosync_commands
    from romcloud.cli.main import cli

    config_path = tmp_path / "romcloud.toml"
    write_config(
        AppConfig(
            source=SourceConfig("local", (tmp_path / "roms").as_posix()),
            cache=CacheConfig((tmp_path / "cache").as_posix()),
            local_roms_path=(tmp_path / "local-roms").as_posix(),
            data_path=(tmp_path / "data").as_posix(),
            saves=SavesConfig(
                local_path=(tmp_path / "saves").as_posix(),
                auto_sync_enabled=False,
            ),
        ),
        str(config_path),
    )
    monkeypatch.setattr(
        autosync_commands,
        "_coordinator",
        lambda _ctx: (_ for _ in ()).throw(
            AssertionError("disabled lifecycle entry constructed coordinator")
        ),
    )

    runner = CliRunner()
    for event in ("game-start", "game-stop"):
        result = runner.invoke(
            cli,
            [
                "--config",
                str(config_path),
                "_autosync",
                event,
                "psx",
                "libretro",
                "pcsx",
                "Game.chd",
            ],
        )
        assert result.exit_code == 0, result.output


def test_game_stop_worker_skips_popup_when_quick_sync_finds_no_new_conflict(
    tmp_path: Path, monkeypatch
):
    from romcloud.cli.commands import autosync as autosync_commands
    from romcloud.cli.main import cli

    config_path = tmp_path / "romcloud.toml"
    write_config(
        AppConfig(
            source=SourceConfig("local", (tmp_path / "roms").as_posix()),
            cache=CacheConfig((tmp_path / "cache").as_posix()),
            local_roms_path=(tmp_path / "local-roms").as_posix(),
            data_path=(tmp_path / "data").as_posix(),
            saves=SavesConfig(
                local_path=(tmp_path / "saves").as_posix(),
                auto_sync_enabled=True,
            ),
        ),
        str(config_path),
    )
    coordinator = type("Coordinator", (), {"game_stop": lambda self, **_kwargs: ()})()
    monkeypatch.setattr(autosync_commands, "_coordinator", lambda _ctx: coordinator)
    monkeypatch.setattr(
        autosync_commands,
        "_launch_pending_conflict_popup",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("no-conflict worker must not launch a popup")
        ),
    )

    result = CliRunner().invoke(
        cli,
        [
            "--config",
            str(config_path),
            "_autosync",
            "game-stop",
            "psx",
            "libretro",
            "pcsx",
            "Game.chd",
        ],
    )

    assert result.exit_code == 0, result.output


def test_game_stop_worker_passes_exact_caller_to_one_popup_launch(
    tmp_path: Path, monkeypatch
):
    from romcloud.cli.commands import autosync as autosync_commands
    from romcloud.cli.main import cli

    config_path = tmp_path / "romcloud.toml"
    write_config(
        AppConfig(
            source=SourceConfig("local", (tmp_path / "roms").as_posix()),
            cache=CacheConfig((tmp_path / "cache").as_posix()),
            local_roms_path=(tmp_path / "local-roms").as_posix(),
            data_path=(tmp_path / "data").as_posix(),
            saves=SavesConfig(
                local_path=(tmp_path / "saves").as_posix(),
                auto_sync_enabled=True,
            ),
        ),
        str(config_path),
    )
    coordinator = type(
        "Coordinator",
        (),
        {"game_stop": lambda self, **_kwargs: ("new-conflict-id",)},
    )()
    launches = []
    monkeypatch.setenv("ROMCLOUD_AUTOSYNC_CALLER_PID", "4242")
    monkeypatch.setattr(autosync_commands, "_coordinator", lambda _ctx: coordinator)
    monkeypatch.setattr(
        autosync_commands,
        "_launch_pending_conflict_popup",
        lambda root, **kwargs: launches.append((root, kwargs)),
    )

    result = CliRunner().invoke(
        cli,
        [
            "--config",
            str(config_path),
            "_autosync",
            "game-stop",
            "psx",
            "libretro",
            "pcsx",
            "Game.chd",
        ],
    )

    assert result.exit_code == 0, result.output
    assert launches == [
        (
            tmp_path / "data",
            {"lifecycle_caller_pid": 4242},
        )
    ]


def test_pending_conflict_launcher_is_short_lived_and_uses_focused_mode(
    tmp_path: Path, monkeypatch
):
    from romcloud.cli.commands import autosync as autosync_commands

    data_root = tmp_path / "romcloud" / "data"
    launcher = tmp_path / "romcloud" / "bin" / "romcloud-ports"
    launcher.parent.mkdir(parents=True)
    launcher.write_text("launcher", encoding="utf-8")
    savesync_prompts.enqueue(data_root, ("conflict-id",))
    calls = []
    monkeypatch.delenv("ROMCLOUD_BIN", raising=False)

    def fake_run(argv, **kwargs):
        calls.append((argv, kwargs))
        return type("Result", (), {"returncode": 0})()

    monkeypatch.setattr(
        batocera_auto_savesync,
        "wait_for_emulationstation_display",
        lambda _pid: batocera_auto_savesync.EmulationStationReadiness(
            ready=True,
            signal="x11-active-es",
            detail="x11-active-es",
            elapsed_seconds=0.1,
            attempts=2,
        ),
    )
    monkeypatch.setattr(autosync_commands.subprocess, "run", fake_run)

    autosync_commands._launch_pending_conflict_popup(
        data_root, lifecycle_caller_pid=4242
    )

    assert calls[0][0] == [str(launcher), "--savesync-conflicts"]
    assert calls[0][1]["check"] is False
    assert "timeout" not in calls[0][1]
    assert calls[0][1]["start_new_session"] is True
    assert calls[0][1]["close_fds"] is True
    assert calls[0][1]["cwd"] == str(data_root.parent)
    assert calls[0][1]["env"]["ROMCLOUD_BIN"] == str(
        launcher.with_name("romcloud")
    )


def test_popup_launch_failure_is_logged_and_queue_survives(
    tmp_path: Path, monkeypatch, caplog
):
    from romcloud.cli.commands import autosync as autosync_commands

    data_root = tmp_path / "romcloud" / "data"
    launcher = tmp_path / "romcloud" / "bin" / "romcloud-ports"
    launcher.parent.mkdir(parents=True)
    launcher.write_text("launcher", encoding="utf-8")
    savesync_prompts.enqueue(data_root, ("conflict-id",))
    monkeypatch.delenv("ROMCLOUD_BIN", raising=False)
    monkeypatch.setattr(
        batocera_auto_savesync,
        "wait_for_emulationstation_display",
        lambda _pid: batocera_auto_savesync.EmulationStationReadiness(
            ready=True,
            signal="wayland-active-es",
            detail="wayland-active-es",
            elapsed_seconds=0.1,
            attempts=2,
        ),
    )
    monkeypatch.setattr(
        autosync_commands.subprocess,
        "run",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("cannot exec")),
    )

    with caplog.at_level("WARNING"):
        autosync_commands._launch_pending_conflict_popup(data_root)

    assert "subprocess launch failed" in caplog.text
    assert savesync_prompts.pending_ids(data_root) == ("conflict-id",)


def test_popup_readiness_timeout_preserves_queue_and_does_not_launch(
    tmp_path: Path, monkeypatch, caplog
):
    from romcloud.cli.commands import autosync as autosync_commands

    data_root = tmp_path / "romcloud" / "data"
    launcher = tmp_path / "romcloud" / "bin" / "romcloud-ports"
    launcher.parent.mkdir(parents=True)
    launcher.write_text("launcher", encoding="utf-8")
    savesync_prompts.enqueue(data_root, ("conflict-id",))
    monkeypatch.setattr(
        batocera_auto_savesync,
        "wait_for_emulationstation_display",
        lambda _pid: batocera_auto_savesync.EmulationStationReadiness(
            ready=False,
            signal="display-not-ready",
            detail="xdotool found no active EmulationStation window",
            elapsed_seconds=5.0,
            attempts=14,
        ),
    )
    monkeypatch.setattr(
        autosync_commands.subprocess,
        "run",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("readiness timeout must not launch popup")
        ),
    )

    with caplog.at_level("WARNING"):
        autosync_commands._launch_pending_conflict_popup(
            data_root, lifecycle_caller_pid=4242
        )

    assert "readiness wait timed out" in caplog.text
    assert savesync_prompts.pending_ids(data_root) == ("conflict-id",)


def test_duplicate_workers_cannot_wait_or_launch_two_popups(
    tmp_path: Path, monkeypatch
):
    from romcloud.cli.commands import autosync as autosync_commands

    data_root = tmp_path / "romcloud" / "data"
    launcher = tmp_path / "romcloud" / "bin" / "romcloud-ports"
    launcher.parent.mkdir(parents=True)
    launcher.write_text("launcher", encoding="utf-8")
    savesync_prompts.enqueue(data_root, ("conflict-id",))
    readiness_entered = threading.Event()
    release_readiness = threading.Event()
    launches = []

    def wait_for_readiness(_pid):
        readiness_entered.set()
        assert release_readiness.wait(timeout=2)
        return batocera_auto_savesync.EmulationStationReadiness(
            ready=True,
            signal="x11-active-es",
            detail="x11-active-es",
            elapsed_seconds=0.1,
            attempts=2,
        )

    monkeypatch.setattr(
        batocera_auto_savesync,
        "wait_for_emulationstation_display",
        wait_for_readiness,
    )
    monkeypatch.setattr(
        autosync_commands.subprocess,
        "run",
        lambda argv, **kwargs: launches.append((argv, kwargs))
        or type("Result", (), {"returncode": 0})(),
    )

    first = threading.Thread(
        target=autosync_commands._launch_pending_conflict_popup,
        args=(data_root,),
        kwargs={"lifecycle_caller_pid": 111},
    )
    second = threading.Thread(
        target=autosync_commands._launch_pending_conflict_popup,
        args=(data_root,),
        kwargs={"lifecycle_caller_pid": 222},
    )
    first.start()
    assert readiness_entered.wait(timeout=2)
    second.start()
    second.join(timeout=2)
    assert not second.is_alive()
    release_readiness.set()
    first.join(timeout=2)

    assert not first.is_alive()
    assert len(launches) == 1


def test_manual_resolution_during_readiness_skips_stale_queued_popup(
    tmp_path: Path, monkeypatch
):
    from romcloud.cli.commands import autosync as autosync_commands

    data_root = tmp_path / "romcloud" / "data"
    launcher = tmp_path / "romcloud" / "bin" / "romcloud-ports"
    launcher.parent.mkdir(parents=True)
    launcher.write_text("launcher", encoding="utf-8")
    savesync_prompts.enqueue(data_root, ("conflict-id",))

    def resolve_while_waiting(_pid):
        savesync_prompts.complete(data_root, "conflict-id")
        return batocera_auto_savesync.EmulationStationReadiness(
            ready=True,
            signal="wayland-active-es",
            detail="wayland-active-es",
            elapsed_seconds=0.1,
            attempts=2,
        )

    monkeypatch.setattr(
        batocera_auto_savesync,
        "wait_for_emulationstation_display",
        resolve_while_waiting,
    )
    monkeypatch.setattr(
        autosync_commands.subprocess,
        "run",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("resolved queue must not launch popup")
        ),
    )

    autosync_commands._launch_pending_conflict_popup(
        data_root, lifecycle_caller_pid=4242
    )

    assert savesync_prompts.pending_ids(data_root) == ()


def test_batocera_hook_detaches_game_stop_and_records_handoff(tmp_path: Path):
    target = tmp_path / "scripts" / "romcloud-autosync"
    install_hook(tmp_path / "bin" / "romcloud", hook_path=target)
    content = target.read_text(encoding="utf-8")

    assert "gameStart" in content and "gameStop" in content
    assert "game-start" in content and "game-stop" in content
    assert 'nohup "$ROMCLOUD_BIN" _autosync game-stop' in content
    assert 'ROMCLOUD_AUTOSYNC_CALLER_PID="$PPID"' in content
    assert 'nohup "$ROMCLOUD_BIN" _autosync menu-loop' in content
    assert "auto-savesync-lifecycle.log" in content
    assert 'event="game_stop_hook_entered"' in content
    assert 'event="game_stop_handoff_started"' in content
    assert 'event="game_stop_handoff_failed"' in content
    assert 'event="game_stop_hook_returned"' in content
    assert "</dev/null &" in content
    assert '"$2" "$3" "$4" "$5"' in content
    if os.name != "nt":
        assert target.stat().st_mode & 0o111
    assert hook_content(tmp_path / "bin" / "romcloud") == content


def test_boot_menu_loop_launcher_is_detached_and_does_no_sync_work(
    tmp_path: Path, monkeypatch
):
    calls = []
    monkeypatch.setattr(
        batocera_auto_savesync,
        "running_menu_loop_pid",
        lambda data_root: None,
    )

    def fake_popen(argv, **kwargs):
        calls.append((argv, kwargs))
        return type("Process", (), {"pid": 3131})()

    pid = batocera_auto_savesync.spawn_menu_loop(
        tmp_path / "data",
        python_executable="/venv/bin/python",
        popen=fake_popen,
    )

    assert pid == 3131
    assert calls[0][0] == [
        "/venv/bin/python",
        "-m",
        "romcloud.cli.main",
        "_autosync",
        "menu-loop",
    ]
    assert calls[0][1] == {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "start_new_session": True,
    }


def test_remote_reconnect_launcher_is_detached_and_does_no_sync_work(
    monkeypatch,
):
    calls = []

    def fake_popen(argv, **kwargs):
        calls.append((argv, kwargs))
        return type("Process", (), {"pid": 5151})()

    pid = batocera_auto_savesync.spawn_remote_reconnect(
        python_executable="/venv/bin/python",
        popen=fake_popen,
    )

    assert pid == 5151
    assert calls == [
        (
            [
                "/venv/bin/python",
                "-m",
                "romcloud.cli.main",
                "_autosync",
                "remote-reconnect",
            ],
            {
                "stdin": subprocess.DEVNULL,
                "stdout": subprocess.DEVNULL,
                "stderr": subprocess.DEVNULL,
                "start_new_session": True,
            },
        )
    ]


def test_duplicate_boot_launcher_reuses_verified_resident_without_spawning(
    tmp_path: Path, monkeypatch
):
    monkeypatch.setattr(
        batocera_auto_savesync,
        "running_menu_loop_pid",
        lambda data_root: 4242,
    )

    pid = batocera_auto_savesync.spawn_menu_loop(
        tmp_path / "data",
        popen=lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("duplicate boot invocation spawned another loop")
        ),
    )

    assert pid == 4242


def test_menu_loop_pid_identity_refuses_unrelated_process(
    tmp_path: Path, monkeypatch
):
    data_root = tmp_path / "data"
    batocera_auto_savesync.record_menu_loop_pid(data_root, 4242)
    monkeypatch.setattr(batocera_auto_savesync, "_pid_alive", lambda pid: True)
    monkeypatch.setattr(
        batocera_auto_savesync,
        "_menu_loop_cmdline_matches",
        lambda pid, **kwargs: False,
    )
    monkeypatch.setattr(
        batocera_auto_savesync,
        "_signal_owned_process",
        lambda *a: (_ for _ in ()).throw(
            AssertionError("unrelated process was signalled")
        ),
    )

    assert batocera_auto_savesync.stop_menu_loop(data_root) is False
    assert not batocera_auto_savesync.menu_loop_pid_path(data_root).exists()


def test_owned_menu_loop_stop_is_bounded_and_clears_restart_record(
    tmp_path: Path, monkeypatch
):
    data_root = tmp_path / "data"
    batocera_auto_savesync.record_menu_loop_pid(data_root, 4242)
    signals = []
    monkeypatch.setattr(
        batocera_auto_savesync,
        "running_menu_loop_pid",
        lambda root: 4242,
    )
    monkeypatch.setattr(
        batocera_auto_savesync,
        "_signal_owned_process",
        lambda pid, sig: signals.append((pid, sig)),
    )
    monkeypatch.setattr(batocera_auto_savesync, "_pid_alive", lambda pid: False)

    assert batocera_auto_savesync.stop_menu_loop(data_root) is True
    assert signals == [(4242, batocera_auto_savesync.signal.SIGTERM)]
    assert not batocera_auto_savesync.menu_loop_pid_path(data_root).exists()


@pytest.mark.skipif(os.name == "nt", reason="Batocera hook is a POSIX shell script")
def test_game_stop_hook_returns_while_worker_continues(tmp_path: Path):
    binary = tmp_path / "romcloud"
    binary.write_text("#!/bin/bash\nsleep 0.4\n", encoding="utf-8")
    binary.chmod(0o755)
    hook = install_hook(binary, hook_path=tmp_path / "romcloud-autosync")

    started = time.monotonic()
    subprocess.run(
        [str(hook), "gameStop", "psx", "libretro", "pcsx", "Game.chd"],
        check=True,
        timeout=2,
    )

    assert time.monotonic() - started < 0.2


@pytest.mark.skipif(os.name == "nt", reason="Batocera hook is a POSIX shell script")
def test_game_stop_hook_logs_missing_worker_binary(tmp_path: Path):
    binary = tmp_path / "missing-romcloud"
    hook = install_hook(binary, hook_path=tmp_path / "romcloud-autosync")

    subprocess.run(
        [str(hook), "gameStop", "psx", "libretro", "pcsx", "Game.chd"],
        check=True,
        timeout=2,
    )

    lifecycle_log = tmp_path.parent / "logs" / "auto-savesync-lifecycle.log"
    assert 'event="game_stop_handoff_failed"' in lifecycle_log.read_text(
        encoding="utf-8"
    )
    assert 'reason="romcloud_bin_unavailable"' in lifecycle_log.read_text(
        encoding="utf-8"
    )


def test_lifecycle_caller_pid_accepts_only_a_positive_exact_value():
    assert (
        batocera_auto_savesync.lifecycle_caller_pid(
            {"ROMCLOUD_AUTOSYNC_CALLER_PID": "4242"}
        )
        == 4242
    )
    assert (
        batocera_auto_savesync.lifecycle_caller_pid(
            {"ROMCLOUD_AUTOSYNC_CALLER_PID": "not-a-pid"}
        )
        is None
    )
    assert batocera_auto_savesync.lifecycle_caller_pid({}) is None


def test_readiness_waits_for_caller_exit_and_two_active_es_observations(
    tmp_path: Path,
):
    proc_root = tmp_path / "proc"
    caller = proc_root / "4242"
    caller.mkdir(parents=True)
    (caller / "cmdline").write_bytes(
        b"/usr/bin/python3\0/usr/bin/emulatorlauncher\0"
    )
    now = 0.0
    probe_calls = 0
    sleep_calls = 0

    def clock():
        return now

    def sleep(seconds):
        nonlocal now, sleep_calls
        now += seconds
        sleep_calls += 1
        if sleep_calls == 1:
            (caller / "cmdline").unlink()

    def probe():
        nonlocal probe_calls
        probe_calls += 1
        return True, "x11-active-es (pid 99)"

    result = batocera_auto_savesync.wait_for_emulationstation_display(
        4242,
        timeout=1.0,
        proc_root=proc_root,
        clock=clock,
        sleep=sleep,
        probe=probe,
    )

    assert result.ready is True
    assert result.signal == "x11-active-es (pid 99)"
    assert probe_calls == 2
    assert sleep_calls == 2


def test_readiness_timeout_is_bounded_without_a_display_signal():
    now = 0.0
    sleeps: list[float] = []

    def clock():
        return now

    def sleep(seconds):
        nonlocal now
        sleeps.append(seconds)
        now += seconds

    result = batocera_auto_savesync.wait_for_emulationstation_display(
        None,
        timeout=0.3,
        poll_interval=0.05,
        clock=clock,
        sleep=sleep,
        probe=lambda: (False, "no active ES window"),
    )

    assert result.ready is False
    assert result.detail == "no active ES window"
    assert result.elapsed_seconds == pytest.approx(0.3)
    assert sum(sleeps) == pytest.approx(0.3)
    assert result.attempts < 10


def test_x11_readiness_probe_requires_active_window_to_belong_to_es(
    tmp_path: Path,
):
    proc_root = tmp_path / "proc"
    es = proc_root / "99"
    es.mkdir(parents=True)
    (es / "comm").write_text("emulationstation\n", encoding="utf-8")
    commands = []

    def run(argv, **kwargs):
        commands.append((argv, kwargs))
        return type("Result", (), {"returncode": 0, "stdout": "1234\n99\n"})()

    ready, detail = batocera_auto_savesync.probe_emulationstation_display(
        environment={"DISPLAY": ":0"},
        proc_root=proc_root,
        which=lambda name: "/usr/bin/xdotool" if name == "xdotool" else None,
        run=run,
    )

    assert ready is True
    assert detail == "x11-active-es (pid 99)"
    assert commands[0][0] == [
        "/usr/bin/xdotool",
        "getactivewindow",
        "getwindowpid",
    ]


def test_wayland_readiness_probe_requires_active_es_toplevel():
    commands = []

    def run(argv, **kwargs):
        commands.append((argv, kwargs))
        return type("Result", (), {"returncode": 0})()

    ready, detail = batocera_auto_savesync.probe_emulationstation_display(
        environment={"WAYLAND_DISPLAY": "wayland-0"},
        which=lambda name: "/usr/bin/wlrctl" if name == "wlrctl" else None,
        run=run,
    )

    assert ready is True
    assert detail == "wayland-active-es (app_id:emulationstation)"
    assert commands[0][0] == [
        "/usr/bin/wlrctl",
        "toplevel",
        "find",
        "app_id:emulationstation",
        "state:active",
    ]


def test_lifecycle_mapping_is_registry_bounded_and_xemu_is_never_automatic():
    policy = DEFAULT_SAVE_SELECTION_POLICY

    assert layout_ids_for_session(policy, "gamecube") == frozenset(
        {"dolphin-gc-memory-card-images", "dolphin-gc-gci-saves"}
    )
    assert layout_ids_for_session(policy, "wii") == frozenset(
        {"dolphin-wii-title-saves"}
    )
    assert layout_ids_for_session(policy, "psx", "duckstation") == frozenset(
        {"duckstation-memory-cards", "duckstation-root-sav"}
    )
    assert layout_ids_for_session(policy, "psx", "libretro", "pcsx-rearmed") == (
        frozenset({"retroarch-root-psx"})
    )
    assert layout_ids_for_session(policy, "xbox", "xemu") == frozenset()
    assert layout_ids_for_session(policy, "unknown-system") == frozenset()


def test_game_exit_detects_first_save_and_uploads_only_that_registry_group(tmp_path: Path):
    provider = _Provider()
    service = _service(tmp_path, provider)
    coordinator = _coordinator(tmp_path, service)
    service.full_sync()
    coordinator.game_start(system="psx", emulator="libretro", core="pcsx", rom="Game.chd")
    _write(tmp_path / "local" / "psx" / "Game.srm", b"new-save")

    coordinator.game_stop(
        system="psx", emulator="libretro", core="pcsx", rom="Game.chd"
    )

    assert (tmp_path / "remote" / "psx" / "Game.srm").read_bytes() == b"new-save"
    assert all(not group.dirty_path_hints for group in service.get_state().groups)


def test_gba_game_stop_uploads_and_periodic_quick_sync_repairs_materialization(
    tmp_path: Path,
):
    provider = _Provider()
    service = _service(tmp_path, provider)
    coordinator = _coordinator(tmp_path, service)
    local = tmp_path / "local" / "gba" / "Game.srm"
    remote = tmp_path / "remote" / "gba" / "Game.srm"
    service.full_sync()
    coordinator.game_start(
        system="gba", emulator="libretro", core="mgba", rom="Game.gba"
    )
    _write(local, b"gba-progress")

    coordinator.game_stop(
        system="gba", emulator="libretro", core="mgba", rom="Game.gba"
    )

    assert local.read_bytes() == b"gba-progress"
    assert remote.read_bytes() == b"gba-progress"

    local.unlink()
    coordinator.menu_tick(force=True)

    assert local.read_bytes() == b"gba-progress"
    assert remote.read_bytes() == b"gba-progress"
    assert service.quick_sync().status == "unchanged"


def test_game_exit_remote_dirty_downloads_through_quick_sync(tmp_path: Path):
    provider = _Provider()
    service = _service(tmp_path, provider)
    coordinator = _coordinator(tmp_path, service)
    local = tmp_path / "local" / "psx" / "Game.srm"
    remote = tmp_path / "remote" / "psx" / "Game.srm"
    _write(local, b"base")
    service.full_sync()
    coordinator.game_start(
        system="psx", emulator="libretro", core="pcsx", rom="Game.chd"
    )
    _write(remote, b"peer-progress")
    service._append_remote_journal(  # type: ignore[attr-defined]
        revision="peer-game-stop",
        timestamp="2026-01-01T00:00:00+00:00",
        mutations=[
            {
                "system": "psx",
                "layout_id": "retroarch-root-psx",
                "group_id": "retroarch-root-psx:psx/Game",
                "object_id": "psx/Game.srm",
                "operation": "update",
            }
        ],
    )

    coordinator.game_stop(
        system="psx", emulator="libretro", core="pcsx", rom="Game.chd"
    )

    assert local.read_bytes() == b"peer-progress"
    assert remote.read_bytes() == b"peer-progress"
    assert service.get_state().groups[0].condition is SaveGroupCondition.CLEAN


def test_game_exit_both_dirty_preserves_conflict(tmp_path: Path):
    provider = _Provider()
    service = _service(tmp_path, provider)
    coordinator = _coordinator(tmp_path, service)
    local = tmp_path / "local" / "psx" / "Game.srm"
    remote = tmp_path / "remote" / "psx" / "Game.srm"
    _write(local, b"base")
    service.full_sync()
    coordinator.game_start(
        system="psx", emulator="libretro", core="pcsx", rom="Game.chd"
    )
    _write(local, b"local-progress")
    _write(remote, b"peer-progress")
    service._append_remote_journal(  # type: ignore[attr-defined]
        revision="peer-conflict",
        timestamp="2026-01-01T00:00:00+00:00",
        mutations=[
            {
                "system": "psx",
                "layout_id": "retroarch-root-psx",
                "group_id": "retroarch-root-psx:psx/Game",
                "object_id": "psx/Game.srm",
                "operation": "update",
            }
        ],
    )

    new_conflicts = coordinator.game_stop(
        system="psx", emulator="libretro", core="pcsx", rom="Game.chd"
    )

    assert local.read_bytes() == b"local-progress"
    assert remote.read_bytes() == b"peer-progress"
    assert service.get_state().groups[0].condition is SaveGroupCondition.CONFLICT
    assert new_conflicts == (service.get_state().active_conflicts[0].conflict_id,)
    assert savesync_prompts.pending_ids(tmp_path / "data") == new_conflicts

    # Re-observing the same authoritative fingerprint is not a new prompt.
    assert coordinator.game_stop(
        system="psx", emulator="libretro", core="pcsx", rom="Game.chd"
    ) == ()
    assert savesync_prompts.pending_ids(tmp_path / "data") == new_conflicts


def test_only_game_stop_collects_new_conflict_ids(tmp_path: Path):
    provider = _Provider()
    service = _service(tmp_path, provider)
    coordinator = _coordinator(tmp_path, service)
    local = tmp_path / "local" / "psx" / "Game.srm"
    remote = tmp_path / "remote" / "psx" / "Game.srm"
    _write(local, b"base")
    service.full_sync()
    _write(local, b"local-progress")
    service.mark_local_dirty("psx/Game.srm")
    _write(remote, b"remote-progress")
    service._append_remote_journal(  # type: ignore[attr-defined]
        revision="peer-menu-conflict",
        timestamp="2026-01-01T00:00:00+00:00",
        mutations=[
            {
                "system": "psx",
                "layout_id": "retroarch-root-psx",
                "group_id": "retroarch-root-psx:psx/Game",
                "object_id": "psx/Game.srm",
                "operation": "update",
            }
        ],
    )

    coordinator.menu_tick(force=True)

    assert len(service.get_state().active_conflicts) == 1
    assert savesync_prompts.pending_ids(tmp_path / "data") == ()


def test_game_stop_queues_multiple_new_conflicts_by_exact_identity(tmp_path: Path):
    provider = _Provider()
    service = _service(tmp_path, provider)
    coordinator = _coordinator(tmp_path, service)
    for name in ("Alpha", "Beta"):
        _write(tmp_path / "local" / "psx" / f"{name}.srm", b"base")
    service.full_sync()
    coordinator.game_start(
        system="psx", emulator="libretro", core="pcsx", rom="Collection.chd"
    )
    mutations = []
    for name in ("Alpha", "Beta"):
        path = f"psx/{name}.srm"
        _write(tmp_path / "local" / path, f"local-{name}".encode())
        _write(tmp_path / "remote" / path, f"remote-{name}".encode())
        mutations.append(
            {
                "system": "psx",
                "layout_id": "retroarch-root-psx",
                "group_id": f"retroarch-root-psx:{name.lower()}",
                "object_id": path,
                "operation": "update",
            }
        )
    service._append_remote_journal(  # type: ignore[attr-defined]
        revision="peer-multiple-conflicts",
        timestamp="2026-01-01T00:00:00+00:00",
        mutations=mutations,
    )

    new_conflicts = coordinator.game_stop(
        system="psx", emulator="libretro", core="pcsx", rom="Collection.chd"
    )

    active_ids = tuple(
        sorted(item.conflict_id for item in service.get_state().active_conflicts)
    )
    assert len(active_ids) == 2
    assert new_conflicts == active_ids
    assert savesync_prompts.pending_ids(tmp_path / "data") == active_ids


def test_game_exit_unchanged_uses_no_transaction(tmp_path: Path, monkeypatch):
    provider = _Provider()
    service = _service(tmp_path, provider)
    coordinator = _coordinator(tmp_path, service)
    local = tmp_path / "local" / "psx" / "Game.srm"
    _write(local, b"base")
    service.full_sync()
    coordinator.game_start(
        system="psx", emulator="libretro", core="pcsx", rom="Game.chd"
    )
    monkeypatch.setattr(
        save_transaction,
        "prepare_transaction",
        lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("unchanged gameStop must not stage a transaction")
        ),
    )

    coordinator.game_stop(
        system="psx", emulator="libretro", core="pcsx", rom="Game.chd"
    )

    assert local.read_bytes() == b"base"


def test_game_exit_changed_save_backs_up_only_that_save(
    tmp_path: Path, monkeypatch
):
    provider = _Provider()
    service = _service(tmp_path, provider)
    coordinator = _coordinator(tmp_path, service)
    changed = tmp_path / "local" / "psx" / "Changed.srm"
    stable = tmp_path / "local" / "psx" / "Stable.srm"
    _write(changed, b"before")
    _write(stable, b"stable")
    for index in range(64):
        _write(tmp_path / "local" / "foreign" / f"User-{index:03d}.bin", b"user")
    service.full_sync()
    coordinator.game_start(
        system="psx", emulator="libretro", core="pcsx", rom="Changed.chd"
    )
    changed.write_bytes(b"after")
    captured: list[save_transaction.TransactionMetrics] = []
    real_prepare = save_transaction.prepare_transaction

    def track_prepare(*args, **kwargs):
        transaction = real_prepare(*args, **kwargs)
        captured.append(transaction.metrics)
        return transaction

    monkeypatch.setattr(save_transaction, "prepare_transaction", track_prepare)

    coordinator.game_stop(
        system="psx", emulator="libretro", core="pcsx", rom="Changed.chd"
    )

    assert (tmp_path / "remote" / "psx" / "Changed.srm").read_bytes() == b"after"
    assert (tmp_path / "remote" / "psx" / "Stable.srm").read_bytes() == b"stable"
    assert len(captured) == 1
    assert captured[0].backed_up_files == 1
    assert captured[0].backed_up_bytes == len(b"before")
    previous = tmp_path / "remote.savesync-previous"
    assert [
        path.relative_to(previous).as_posix()
        for path in previous.rglob("*")
        if path.is_file()
    ] == ["psx/Changed.srm"]
    assert (tmp_path / "local" / "foreign" / "User-063.bin").read_bytes() == b"user"


def test_game_exit_retries_after_duckstation_save_changes_during_staging(
    tmp_path: Path, monkeypatch, caplog
):
    provider = _Provider()
    service = _service(tmp_path, provider)
    coordinator = _coordinator(tmp_path, service)
    local = (
        tmp_path
        / "local"
        / "duckstation"
        / "memcards"
        / "_usr_share_duckstation_1.mcd"
    )
    remote = (
        tmp_path
        / "remote"
        / "duckstation"
        / "memcards"
        / "_usr_share_duckstation_1.mcd"
    )
    _write(local, b"baseline")
    service.full_sync()
    coordinator.game_start(
        system="psx", emulator="duckstation", core="duckstation", rom="Game.chd"
    )
    _write(local, b"first shutdown write")

    original_prepare = save_transaction.prepare_transaction
    prepare_calls = 0

    def change_once_after_staging(*args, **kwargs):
        nonlocal prepare_calls
        transaction = original_prepare(*args, **kwargs)
        prepare_calls += 1
        if prepare_calls == 1:
            _write(local, b"stable final bytes")
        return transaction

    monkeypatch.setattr(
        save_transaction, "prepare_transaction", change_once_after_staging
    )

    coordinator.game_stop(
        system="psx", emulator="duckstation", core="duckstation", rom="Game.chd"
    )

    assert prepare_calls == 2
    assert local.read_bytes() == b"stable final bytes"
    assert remote.read_bytes() == b"stable final bytes"
    assert all(not group.dirty_path_hints for group in service.get_state().groups)
    assert "save data changing during staging" in caplog.text


def test_unstable_local_group_is_bounded_and_keeps_dirty_state(
    tmp_path: Path, monkeypatch, caplog
):
    provider = _Provider()
    service = _service(tmp_path, provider)
    path = tmp_path / "local" / "psx" / "Game.srm"
    _write(path, b"base")
    service.full_sync()
    _write(path, b"changed")
    service.mark_local_dirty("psx/Game.srm")
    provider.reachability_checks = 0
    observations = 0

    def never_stable(group_ids):
        nonlocal observations
        observations += 1
        return {"observation": observations}

    monkeypatch.setattr(service, "observe_local_groups", never_stable)
    coordinator = AutoSaveSyncCoordinator(
        service,
        data_root=tmp_path / "data",
        enabled=True,
        policy=DEFAULT_SAVE_SELECTION_POLICY,
        quiet_seconds=0,
        stability_checks=3,
    )

    coordinator.drain_pending()

    assert observations == 4
    assert provider.reachability_checks == 0
    assert (tmp_path / "remote" / "psx" / "Game.srm").read_bytes() == b"base"
    assert service.get_state().groups[0].dirty_path_hints == ("psx/Game.srm",)
    assert "did not stabilize after 3 bounded checks" in caplog.text


def test_auto_stability_and_verification_do_not_scan_unrelated_layouts(
    tmp_path: Path, monkeypatch
):
    provider = _Provider()
    service = _service(tmp_path, provider)
    local = tmp_path / "local" / "psx" / "Game.srm"
    remote = tmp_path / "remote" / "psx" / "Game.srm"
    _write(local, b"base")
    service.full_sync()
    _write(local, b"changed")
    service.mark_local_dirty("psx/Game.srm")

    def unexpected_full_scan():
        raise AssertionError("targeted Auto SaveSync used an all-layout scan")

    monkeypatch.setattr(service, "_scan_local", unexpected_full_scan)
    monkeypatch.setattr(service, "_scan_remote", unexpected_full_scan)

    _coordinator(tmp_path, service).drain_pending()

    assert remote.read_bytes() == b"changed"


def test_active_game_group_is_deferred_then_processed_after_exit(tmp_path: Path):
    provider = _Provider()
    service = _service(tmp_path, provider)
    local = tmp_path / "local" / "psx" / "Game.srm"
    remote = tmp_path / "remote" / "psx" / "Game.srm"
    _write(local, b"base")
    service.full_sync()
    _write(local, b"changed")
    service.mark_local_dirty("psx/Game.srm")
    coordinator = _coordinator(tmp_path, service)
    coordinator.game_start(system="psx", emulator="libretro", core="pcsx", rom="Game.chd")

    coordinator.drain_pending()

    assert remote.read_bytes() == b"base"
    assert any(group.dirty_path_hints for group in service.get_state().groups)

    coordinator.game_stop(
        system="psx", emulator="libretro", core="pcsx", rom="Game.chd"
    )
    assert remote.read_bytes() == b"changed"


def test_unavailable_remote_preserves_durable_dirty_state(tmp_path: Path):
    provider = _Provider()
    service = _service(tmp_path, provider)
    local = tmp_path / "local" / "psx" / "Game.srm"
    _write(local, b"base")
    service.full_sync()
    _write(local, b"changed")
    service.mark_local_dirty("psx/Game.srm")
    provider.reachable = False

    _coordinator(tmp_path, service).drain_pending()

    group = service.get_state().groups[0]
    assert group.condition is SaveGroupCondition.LOCAL_DIRTY
    assert group.dirty_path_hints == ("psx/Game.srm",)
    assert (tmp_path / "remote" / "psx" / "Game.srm").read_bytes() == b"base"

    provider.reachable = True
    _coordinator(tmp_path, service).drain_pending()
    assert (tmp_path / "remote" / "psx" / "Game.srm").read_bytes() == b"changed"


def test_offline_game_mode_still_allows_independent_savesync(tmp_path: Path):
    provider = _Provider()
    offline = CapabilityPolicy("smart_cache", OperatingMode.OFFLINE)
    service = _service(tmp_path, provider, capability_policy=offline)
    path = tmp_path / "local" / "psx" / "Game.srm"
    _write(path, b"base")
    service.full_sync()
    _write(path, b"local")
    service.mark_local_dirty("psx/Game.srm")

    _coordinator(tmp_path, service).drain_pending()

    assert provider.reachability_checks > 0
    assert (tmp_path / "remote" / "psx" / "Game.srm").read_bytes() == b"local"


def test_verified_unchanged_hint_clears_without_transaction(
    tmp_path: Path, monkeypatch
):
    provider = _Provider()
    service = _service(tmp_path, provider)
    path = tmp_path / "local" / "psx" / "Game.srm"
    _write(path, b"base")
    service.full_sync()
    service.mark_local_dirty("psx/Game.srm")

    def unexpected_transaction(*args, **kwargs):
        raise AssertionError("unchanged group must not create a transaction")

    monkeypatch.setattr(save_transaction, "prepare_transaction", unexpected_transaction)
    _coordinator(tmp_path, service).drain_pending()

    group = service.get_state().groups[0]
    assert group.condition is SaveGroupCondition.CLEAN
    assert group.dirty_path_hints == ()


def test_repeated_dirty_hint_coalesces_to_one_group_and_one_pass(
    tmp_path: Path, monkeypatch
):
    provider = _Provider()
    service = _service(tmp_path, provider)
    path = tmp_path / "local" / "psx" / "Game.srm"
    _write(path, b"base")
    service.full_sync()
    _write(path, b"changed")
    service.mark_local_dirty("psx/Game.srm")
    service.mark_local_dirty("psx/Game.srm")
    calls = 0
    original = service.quick_sync

    def counted(*args, **kwargs):
        nonlocal calls
        calls += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(service, "quick_sync", counted)
    _coordinator(tmp_path, service).drain_pending()

    assert calls == 1
    assert len(service.get_state().groups) == 1
    assert (tmp_path / "remote" / "psx" / "Game.srm").read_bytes() == b"changed"


def test_background_transaction_contains_only_pending_group(
    tmp_path: Path, monkeypatch
):
    provider = _Provider()
    service = _service(tmp_path, provider)
    first = tmp_path / "local" / "psx" / "First.srm"
    second = tmp_path / "local" / "psx" / "Second.srm"
    _write(first, b"base-1")
    _write(second, b"base-2")
    service.full_sync()
    _write(first, b"changed-1")
    service.mark_local_dirty("psx/First.srm")
    captured: list[set[str]] = []
    original = save_transaction.prepare_transaction

    def capture(journal_path, views, **kwargs):
        views = tuple(views)
        captured.extend(set(view.current) | set(view.desired) for view in views)
        return original(journal_path, views, **kwargs)

    monkeypatch.setattr(save_transaction, "prepare_transaction", capture)
    _coordinator(tmp_path, service).drain_pending()

    assert captured == [{"psx/First.srm"}]
    assert (tmp_path / "remote" / "psx" / "Second.srm").read_bytes() == b"base-2"


def test_xemu_dirty_group_is_never_automatically_processed(tmp_path: Path):
    provider = _Provider()
    service = _service(tmp_path, provider, xbox_enabled=True)
    path = tmp_path / "local" / "xbox" / "xbox_hdd.qcow2"
    _write(path, b"disk")
    service.mark_local_dirty("xbox/xbox_hdd.qcow2")

    _coordinator(tmp_path, service).drain_pending()

    assert provider.reachability_checks == 0
    assert service.get_state().groups[0].dirty_path_hints
    assert not (tmp_path / "remote").exists()


def test_both_sides_changed_creates_conflict_without_overwrite(tmp_path: Path):
    provider = _Provider()
    service = _service(tmp_path, provider)
    local = tmp_path / "local" / "psx" / "Game.srm"
    remote = tmp_path / "remote" / "psx" / "Game.srm"
    _write(local, b"base")
    service.full_sync()
    _write(local, b"local")
    _write(remote, b"remote")
    service.mark_local_dirty("psx/Game.srm")

    _coordinator(tmp_path, service).drain_pending()

    assert local.read_bytes() == b"local"
    assert remote.read_bytes() == b"remote"
    state = service.get_state()
    assert state.groups[0].condition is SaveGroupCondition.CONFLICT
    assert len(tuple(conflict for conflict in state.conflicts if not conflict.resolved)) == 1


def test_remote_only_change_is_downloaded_by_game_exit(tmp_path: Path):
    provider = _Provider()
    service = _service(tmp_path, provider)
    local = tmp_path / "local" / "psx" / "Game.srm"
    remote = tmp_path / "remote" / "psx" / "Game.srm"
    _write(local, b"base")
    service.full_sync()
    _write(remote, b"remote")
    # Advisory false-positive: authoritative Quick Sync discovers that only
    # the remote changed and safely promotes it after gameplay has stopped.
    service.mark_local_dirty("psx/Game.srm")

    _coordinator(tmp_path, service).drain_pending()

    assert local.read_bytes() == b"remote"
    assert remote.read_bytes() == b"remote"
    assert service.get_state().groups[0].condition is SaveGroupCondition.CLEAN


def test_verified_local_deletion_removes_only_its_remote_group(tmp_path: Path):
    provider = _Provider()
    service = _service(tmp_path, provider)
    removed = tmp_path / "local" / "psx" / "Removed.srm"
    retained = tmp_path / "local" / "psx" / "Retained.srm"
    _write(removed, b"remove")
    _write(retained, b"retain")
    service.full_sync()
    removed.unlink()
    service.mark_local_dirty("psx/Removed.srm")

    _coordinator(tmp_path, service).drain_pending()

    assert not (tmp_path / "remote" / "psx" / "Removed.srm").exists()
    assert (tmp_path / "remote" / "psx" / "Retained.srm").read_bytes() == b"retain"


def test_dirty_group_arriving_during_pass_is_processed_afterward(
    tmp_path: Path, monkeypatch
):
    provider = _Provider()
    service = _service(tmp_path, provider)
    first = tmp_path / "local" / "psx" / "First.srm"
    second = tmp_path / "local" / "psx" / "Second.srm"
    _write(first, b"base-1")
    _write(second, b"base-2")
    service.full_sync()
    _write(first, b"next-1")
    service.mark_local_dirty("psx/First.srm")
    original = service.quick_sync
    injected = False

    def reconcile_then_dirty(*args, **kwargs):
        nonlocal injected
        report = original(*args, **kwargs)
        if not injected:
            injected = True
            _write(second, b"next-2")
            service.mark_local_dirty("psx/Second.srm")
        return report

    monkeypatch.setattr(service, "quick_sync", reconcile_then_dirty)

    _coordinator(tmp_path, service).drain_pending()

    assert (tmp_path / "remote" / "psx" / "First.srm").read_bytes() == b"next-1"
    assert (tmp_path / "remote" / "psx" / "Second.srm").read_bytes() == b"next-2"


def test_rapid_workers_never_reconcile_concurrently(tmp_path: Path, monkeypatch):
    provider = _Provider()
    service = _service(tmp_path, provider)
    path = tmp_path / "local" / "psx" / "Game.srm"
    _write(path, b"base")
    service.full_sync()
    _write(path, b"changed")
    service.mark_local_dirty("psx/Game.srm")
    original = service.quick_sync
    active = 0
    maximum = 0
    guard = threading.Lock()

    def slow_reconcile(*args, **kwargs):
        nonlocal active, maximum
        with guard:
            active += 1
            maximum = max(maximum, active)
        time.sleep(0.1)
        try:
            return original(*args, **kwargs)
        finally:
            with guard:
                active -= 1

    monkeypatch.setattr(service, "quick_sync", slow_reconcile)
    coordinators = [_coordinator(tmp_path, service), _coordinator(tmp_path, service)]
    threads = [threading.Thread(target=item.drain_pending) for item in coordinators]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)

    assert maximum == 1
    assert all(not thread.is_alive() for thread in threads)


def test_manual_and_background_reconcile_share_one_operation_boundary(
    tmp_path: Path, monkeypatch
):
    provider = _Provider()
    service = _service(tmp_path, provider)
    path = tmp_path / "local" / "psx" / "Game.srm"
    _write(path, b"base")
    service.full_sync()
    _write(path, b"changed")
    preview = service.preview_upload()
    service.mark_local_dirty("psx/Game.srm")
    operation_guard = threading.Lock()
    count_guard = threading.Lock()
    active = 0
    maximum = 0
    manual_entered = threading.Event()

    @contextmanager
    def instrumented_operation_lock():
        nonlocal active, maximum
        with operation_guard:
            with count_guard:
                active += 1
                maximum = max(maximum, active)
            if threading.current_thread().name == "manual-upload":
                manual_entered.set()
            try:
                time.sleep(0.05)
                yield
            finally:
                with count_guard:
                    active -= 1

    monkeypatch.setattr(service, "_operation_lock", instrumented_operation_lock)
    manual = threading.Thread(
        target=lambda: service.commit_upload(preview), name="manual-upload"
    )
    automatic = threading.Thread(target=_coordinator(tmp_path, service).drain_pending)
    manual.start()
    assert manual_entered.wait(timeout=2)
    automatic.start()
    manual.join(timeout=5)
    automatic.join(timeout=5)

    assert maximum == 1
    assert not manual.is_alive() and not automatic.is_alive()


def test_periodic_menu_tick_remote_only_change_auto_pulls(tmp_path: Path):
    provider = _Provider()
    service = _service(tmp_path, provider)
    coordinator = _coordinator(tmp_path, service)
    local = tmp_path / "local" / "psx" / "Game.srm"
    remote = tmp_path / "remote" / "psx" / "Game.srm"
    _write(local, b"base")
    service.full_sync()

    _write(remote, b"remote-new")
    service._append_remote_journal(  # type: ignore[attr-defined]
        revision="peer-r1",
        timestamp="2026-01-01T00:00:00+00:00",
        mutations=[
            {
                "system": "psx",
                "layout_id": "retroarch-root-psx",
                "group_id": "retroarch-root-psx:psx/Game",
                "object_id": "psx/Game.srm",
                "operation": "update",
            }
        ],
    )

    coordinator.menu_tick(force=True)

    assert local.read_bytes() == b"remote-new"


def test_menu_loop_survives_multiple_polling_intervals(tmp_path: Path, monkeypatch):
    from romcloud.services import auto_savesync as auto_savesync_service

    class StopLoop(Exception):
        pass

    coordinator = _coordinator(tmp_path, _service(tmp_path, _Provider()))
    ticks: list[bool] = []
    sleeps = 0

    monkeypatch.setattr(auto_savesync_service, "_MENU_PULL_INTERVAL_SECONDS", 2)
    monkeypatch.setattr(
        coordinator,
        "menu_tick",
        lambda *, force=False: ticks.append(force),
    )

    def bounded_sleep(_seconds):
        nonlocal sleeps
        sleeps += 1
        if sleeps == 5:
            raise StopLoop

    monkeypatch.setattr(auto_savesync_service.time, "sleep", bounded_sleep)

    with pytest.raises(StopLoop):
        coordinator.menu_loop()

    assert ticks == [True, False, False]


def test_only_one_menu_loop_holds_the_resident_lock(tmp_path: Path, monkeypatch):
    from romcloud.services import auto_savesync as auto_savesync_service

    class StopLoop(Exception):
        pass

    service = _service(tmp_path, _Provider())
    first = _coordinator(tmp_path, service)
    second = _coordinator(tmp_path, service)
    sleeping = threading.Event()
    release = threading.Event()
    errors: list[BaseException] = []
    first_ticks: list[bool] = []
    second_ticks: list[bool] = []
    monkeypatch.setattr(auto_savesync_service, "_MENU_PULL_INTERVAL_SECONDS", 1)
    monkeypatch.setattr(
        first, "menu_tick", lambda *, force=False: first_ticks.append(force)
    )
    monkeypatch.setattr(
        second, "menu_tick", lambda *, force=False: second_ticks.append(force)
    )

    def blocking_sleep(_seconds):
        sleeping.set()
        if not release.wait(timeout=2):
            raise AssertionError("resident loop test timed out")
        raise StopLoop

    monkeypatch.setattr(auto_savesync_service.time, "sleep", blocking_sleep)

    def run_first():
        try:
            first.menu_loop()
        except StopLoop:
            pass
        except BaseException as exc:  # pragma: no cover - assertion handoff
            errors.append(exc)

    thread = threading.Thread(target=run_first)
    thread.start()
    assert sleeping.wait(timeout=2)

    second.menu_loop()
    release.set()
    thread.join(timeout=2)

    assert not thread.is_alive()
    assert errors == []
    assert first_ticks == [True]
    assert second_ticks == []


def test_resident_menu_loop_exits_without_another_tick_when_disabled(
    tmp_path: Path, monkeypatch
):
    from romcloud.services import auto_savesync as auto_savesync_service

    service = _service(tmp_path, _Provider())
    enabled = True
    ticks: list[bool] = []
    coordinator = AutoSaveSyncCoordinator(
        service,
        data_root=tmp_path / "data",
        enabled=True,
        policy=DEFAULT_SAVE_SELECTION_POLICY,
        quiet_seconds=0,
        enabled_check=lambda: enabled,
    )
    sleeps = 0
    monkeypatch.setattr(auto_savesync_service, "_MENU_PULL_INTERVAL_SECONDS", 2)
    monkeypatch.setattr(
        coordinator,
        "menu_tick",
        lambda *, force=False: ticks.append(force),
    )

    def disable_during_interval(_seconds):
        nonlocal enabled, sleeps
        sleeps += 1
        if sleeps == 2:
            enabled = False

    monkeypatch.setattr(
        auto_savesync_service.time, "sleep", disable_during_interval
    )

    coordinator.menu_loop()

    assert sleeps == 2
    assert ticks == [True]


def test_menu_loop_suppresses_gameplay_then_resumes_after_game_stop(
    tmp_path: Path, monkeypatch
):
    from romcloud.services import auto_savesync as auto_savesync_service

    class StopLoop(Exception):
        pass

    service = _service(tmp_path, _Provider())
    coordinator = _coordinator(tmp_path, service)
    quick_calls = 0
    sleeps = 0
    monkeypatch.setattr(auto_savesync_service, "_MENU_PULL_INTERVAL_SECONDS", 2)
    monkeypatch.setattr(coordinator, "_menu_pull_due", lambda: True)

    def counted_quick_sync(**kwargs):
        nonlocal quick_calls
        quick_calls += 1
        return None

    monkeypatch.setattr(service, "quick_sync", counted_quick_sync)
    coordinator.game_start(
        system="unknown-system", emulator="unknown", core="unknown", rom="Game.rom"
    )

    def lifecycle_sleep(_seconds):
        nonlocal sleeps
        sleeps += 1
        if sleeps == 3:
            coordinator.game_stop(
                system="unknown-system",
                emulator="unknown",
                core="unknown",
                rom="Game.rom",
            )
        if sleeps == 5:
            raise StopLoop

    monkeypatch.setattr(auto_savesync_service.time, "sleep", lifecycle_sleep)

    with pytest.raises(StopLoop):
        coordinator.menu_loop()

    # The initial and first interval ticks were suppressed during gameplay;
    # gameStop reconciled immediately, then the forced-due test tick ran too.
    assert quick_calls == 2


def test_menu_tick_unchanged_journal_performs_no_save_layout_scan(
    tmp_path: Path, monkeypatch
):
    provider = _Provider()
    service = _service(tmp_path, provider)
    coordinator = _coordinator(tmp_path, service)
    _write(tmp_path / "local" / "psx" / "Game.srm", b"base")
    service.full_sync()
    calls = {"local": 0, "remote": 0}

    def fail_local(*args, **kwargs):
        calls["local"] += 1
        raise AssertionError("unchanged menu pull must not scan local layouts")

    def fail_remote(*args, **kwargs):
        calls["remote"] += 1
        raise AssertionError("unchanged menu pull must not scan remote layouts")

    monkeypatch.setattr(service, "_scan_automatic_local", fail_local)
    monkeypatch.setattr(service, "_scan_automatic_remote", fail_remote)

    coordinator.menu_tick(force=True)

    assert calls == {"local": 0, "remote": 0}


def test_remote_reconnect_runs_one_quick_sync_when_ready(tmp_path: Path, monkeypatch):
    provider = _Provider()
    service = _service(tmp_path, provider)
    coordinator = _coordinator(tmp_path, service)
    service.full_sync()
    calls = 0
    original = service.quick_sync

    def counted(**kwargs):
        nonlocal calls
        calls += 1
        return original(**kwargs)

    monkeypatch.setattr(service, "quick_sync", counted)

    coordinator.remote_reconnect()

    assert calls == 1


def test_remote_reconnect_quick_sync_failure_is_not_retried(
    tmp_path: Path, monkeypatch
):
    service = _service(tmp_path, _Provider())
    coordinator = _coordinator(tmp_path, service)
    service.full_sync()
    calls = 0

    def fail_once(**kwargs):
        nonlocal calls
        calls += 1
        raise RuntimeError("remote temporarily unavailable")

    monkeypatch.setattr(service, "quick_sync", fail_once)

    coordinator.remote_reconnect()

    assert calls == 1


def test_remote_reconnect_during_game_defers_until_game_stop(
    tmp_path: Path, monkeypatch
):
    provider = _Provider()
    service = _service(tmp_path, provider)
    coordinator = _coordinator(tmp_path, service)
    _write(tmp_path / "local" / "psx" / "Game.srm", b"base")
    service.full_sync()
    calls = 0
    original = service.quick_sync

    def counted(**kwargs):
        nonlocal calls
        calls += 1
        return original(**kwargs)

    monkeypatch.setattr(service, "quick_sync", counted)
    coordinator.game_start(
        system="psx", emulator="libretro", core="pcsx", rom="Game.chd"
    )

    coordinator.remote_reconnect()
    assert calls == 0

    coordinator.game_stop(
        system="psx", emulator="libretro", core="pcsx", rom="Game.chd"
    )
    assert calls == 1


def test_remote_reconnect_without_baseline_does_no_provider_or_scan_work(
    tmp_path: Path, monkeypatch
):
    provider = _Provider()
    service = _service(tmp_path, provider)
    coordinator = _coordinator(tmp_path, service)
    monkeypatch.setattr(
        service,
        "quick_sync",
        lambda **kwargs: (_ for _ in ()).throw(
            AssertionError("unready reconnect must not attempt Quick Sync")
        ),
    )

    coordinator.remote_reconnect()

    assert provider.reachability_checks == 0


def test_remote_reconnect_unchanged_journal_scans_no_layouts(
    tmp_path: Path, monkeypatch
):
    provider = _Provider()
    service = _service(tmp_path, provider)
    coordinator = _coordinator(tmp_path, service)
    service.full_sync()
    monkeypatch.setattr(
        service,
        "_scan_automatic_local",
        lambda: (_ for _ in ()).throw(
            AssertionError("unchanged reconnect scanned local layouts")
        ),
    )
    monkeypatch.setattr(
        service,
        "_scan_automatic_remote",
        lambda: (_ for _ in ()).throw(
            AssertionError("unchanged reconnect scanned remote layouts")
        ),
    )

    coordinator.remote_reconnect()


def test_menu_tick_unchanged_journal_drains_durable_local_dirty_group(
    tmp_path: Path,
):
    provider = _Provider()
    service = _service(tmp_path, provider)
    coordinator = _coordinator(tmp_path, service)
    local = (
        tmp_path
        / "local"
        / "duckstation"
        / "memcards"
        / "_usr_share_duckstation_1.mcd"
    )
    remote = (
        tmp_path
        / "remote"
        / "duckstation"
        / "memcards"
        / "_usr_share_duckstation_1.mcd"
    )
    service.full_sync()
    _write(local, b"durable-pending-card")
    service.mark_local_dirty(
        "duckstation/memcards/_usr_share_duckstation_1.mcd"
    )

    coordinator.menu_tick(force=True)

    assert remote.read_bytes() == b"durable-pending-card"
    group = service.get_state().groups[0]
    assert group.condition is SaveGroupCondition.CLEAN
    assert group.dirty_path_hints == ()


def test_menu_tick_unchanged_journal_keeps_xemu_dirty_group_manual(
    tmp_path: Path,
):
    provider = _Provider()
    service = _service(tmp_path, provider, xbox_enabled=True)
    coordinator = _coordinator(tmp_path, service)
    local = tmp_path / "local" / "xbox" / "xbox_hdd.qcow2"
    remote = tmp_path / "remote" / "xbox" / "xbox_hdd.qcow2"
    _write(local, b"base")
    service.full_sync()
    _write(local, b"local-change")
    service.mark_local_dirty("xbox/xbox_hdd.qcow2")

    coordinator.menu_tick(force=True)

    assert remote.read_bytes() == b"base"
    group = service.get_state().groups[0]
    assert group.condition is SaveGroupCondition.LOCAL_DIRTY
    assert group.dirty_path_hints == ("xbox/xbox_hdd.qcow2",)


def test_periodic_menu_tick_local_only_change_is_not_overwritten(tmp_path: Path):
    provider = _Provider()
    service = _service(tmp_path, provider)
    coordinator = _coordinator(tmp_path, service)
    local = tmp_path / "local" / "psx" / "Game.srm"
    _write(local, b"base")
    service.full_sync()
    _write(local, b"local-only")

    coordinator.menu_tick(force=True)

    assert local.read_bytes() == b"local-only"


def test_periodic_menu_tick_both_changed_becomes_conflict(tmp_path: Path):
    provider = _Provider()
    service = _service(tmp_path, provider)
    coordinator = _coordinator(tmp_path, service)
    local = tmp_path / "local" / "psx" / "Game.srm"
    remote = tmp_path / "remote" / "psx" / "Game.srm"
    _write(local, b"base")
    service.full_sync()
    _write(local, b"local")
    _write(remote, b"remote")
    service._append_remote_journal(  # type: ignore[attr-defined]
        revision="peer-r2",
        timestamp="2026-01-01T00:00:01+00:00",
        mutations=[
            {
                "system": "psx",
                "layout_id": "retroarch-root-psx",
                "group_id": "retroarch-root-psx:psx/Game",
                "object_id": "psx/Game.srm",
                "operation": "update",
            }
        ],
    )

    coordinator.menu_tick(force=True)

    assert service.get_state().groups[0].condition is SaveGroupCondition.CONFLICT
    assert local.read_bytes() == b"local"
    assert remote.read_bytes() == b"remote"


def test_gameplay_suppresses_periodic_pull_entirely(tmp_path: Path, monkeypatch):
    provider = _Provider()
    service = _service(tmp_path, provider)
    coordinator = _coordinator(tmp_path, service)
    calls = {"quick": 0}

    def counted_quick_sync(**kwargs):
        calls["quick"] += 1
        return None

    monkeypatch.setattr(service, "quick_sync", counted_quick_sync)
    coordinator.game_start(system="psx", emulator="libretro", core="pcsx", rom="Game.chd")

    coordinator.menu_tick(force=True)

    assert calls["quick"] == 0


def test_game_start_during_pull_marks_active_and_defers(tmp_path: Path, monkeypatch):
    provider = _Provider()
    service = _service(tmp_path, provider)
    coordinator = _coordinator(tmp_path, service)
    observed = {"active_seen": False}

    def quick_sync(**kwargs):
        coordinator.game_start(system="psx", emulator="libretro", core="pcsx", rom="Game.chd")
        observed["active_seen"] = kwargs["is_layout_active"]("retroarch-root-psx")
        return None

    monkeypatch.setattr(service, "quick_sync", quick_sync)

    coordinator.menu_tick(force=True)

    assert observed["active_seen"] is True


def test_periodic_pull_never_auto_pulls_xemu(tmp_path: Path):
    provider = _Provider()
    service = _service(tmp_path, provider, xbox_enabled=True)
    coordinator = _coordinator(tmp_path, service)
    local = tmp_path / "local" / "xbox" / "xbox_hdd.qcow2"
    remote = tmp_path / "remote" / "xbox" / "xbox_hdd.qcow2"
    _write(local, b"local")
    service.full_sync()
    _write(remote, b"remote")
    service._append_remote_journal(  # type: ignore[attr-defined]
        revision="peer-rx",
        timestamp="2026-01-01T00:00:03+00:00",
        mutations=[
            {
                "system": "xbox",
                "layout_id": "xemu-hdd",
                "group_id": "xemu-hdd:xbox/xbox_hdd",
                "object_id": "xbox/xbox_hdd.qcow2",
                "operation": "update",
            }
        ],
    )

    coordinator.menu_tick(force=True)

    assert local.read_bytes() == b"local"
