from __future__ import annotations

import hashlib
import os
import subprocess
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from romcloud.bootstrap.container import Container
from romcloud.core.capabilities import CapabilityPolicy, OperatingMode
from romcloud.core.exceptions import SaveSyncConnectivityError, SaveSyncError
from romcloud.core.models.savesync import SaveGroupCondition, SaveQuickSyncResult
from romcloud.core.save_selection import DEFAULT_SAVE_SELECTION_POLICY
from romcloud.core.storage import StorageProvider
from romcloud.infrastructure import diagnostics, save_transaction
from romcloud.infrastructure import save_tree
from romcloud.infrastructure import savesync_commit
from romcloud.infrastructure import savesync_index
from romcloud.infrastructure import savesync_prompts
from romcloud.infrastructure.config import (
    AppConfig,
    CacheConfig,
    RemoteDataConfig,
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
from romcloud.infrastructure.diagnostics import DiagnosticQuery
from romcloud.services.saves import SaveSyncService

from tests.unit._savesync_protocol_helpers import seed_peer_commit


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


def _container_with_rom_selection(
    tmp_path: Path, *, selected_systems: tuple[str, ...]
) -> tuple[Container, Path]:
    source = tmp_path / "rom-source"
    source.mkdir()
    local_saves = tmp_path / "selected-local-saves"
    local_saves.mkdir()
    remote_data = tmp_path / "selected-remote-data"
    remote_data.mkdir()
    config = AppConfig(
        source=SourceConfig(
            "local", str(source), selected_systems=selected_systems
        ),
        cache=CacheConfig(str(tmp_path / "selected-cache")),
        local_roms_path=str(tmp_path / "selected-local-roms"),
        data_path=str(tmp_path / "selected-data"),
        remote_data=RemoteDataConfig("local", str(remote_data)),
        saves=SavesConfig(local_path=str(local_saves), auto_sync_enabled=True),
    )
    config_path = tmp_path / "selected-romcloud.toml"
    write_config(config, str(config_path))
    return Container(config), config_path


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


def test_automatic_savesync_is_enabled_in_cached_and_direct_modes(tmp_path: Path):
    from romcloud.cli.commands.autosync import _auto_sync_enabled
    from romcloud.infrastructure.library_view import write_operating_mode

    config = AppConfig(
        source=SourceConfig("local", str(tmp_path / "roms")),
        cache=CacheConfig(str(tmp_path / "cache")),
        local_roms_path=str(tmp_path / "local-roms"),
        data_path=str(tmp_path / "data"),
        saves=SavesConfig(local_path=str(tmp_path / "saves"), auto_sync_enabled=True),
    )
    for mode, expected in (
        (OperatingMode.CACHE, True),
        (OperatingMode.CONNECTED, True),
        (OperatingMode.OFFLINE, False),
    ):
        write_operating_mode(config, mode)
        assert _auto_sync_enabled(config) is expected


@pytest.mark.parametrize(
    ("auto_enabled", "mode"),
    (
        (False, OperatingMode.CACHE),
        (True, OperatingMode.OFFLINE),
    ),
)
def test_inactive_lifecycle_cli_does_not_construct_coordinator(
    tmp_path: Path, monkeypatch, auto_enabled: bool, mode: OperatingMode
):
    from romcloud.cli.commands import autosync as autosync_commands
    from romcloud.cli.main import cli
    from romcloud.infrastructure.library_view import write_operating_mode

    config_path = tmp_path / "romcloud.toml"
    config = AppConfig(
        source=SourceConfig("local", (tmp_path / "roms").as_posix()),
        cache=CacheConfig((tmp_path / "cache").as_posix()),
        local_roms_path=(tmp_path / "local-roms").as_posix(),
        data_path=(tmp_path / "data").as_posix(),
        saves=SavesConfig(
            local_path=(tmp_path / "saves").as_posix(),
            auto_sync_enabled=auto_enabled,
        ),
    )
    write_config(config, str(config_path))
    write_operating_mode(config, mode)
    coordinator_calls = []

    def unexpected_coordinator(_ctx):
        coordinator_calls.append(True)
        return object()

    monkeypatch.setattr(autosync_commands, "_coordinator", unexpected_coordinator)

    runner = CliRunner()
    commands = [
        ["game-start", "psx", "libretro", "pcsx", "Game.chd"],
        ["game-stop", "psx", "libretro", "pcsx", "Game.chd"],
        ["menu-tick"],
        ["remote-reconnect"],
        ["menu-loop"],
    ]
    for command in commands:
        result = runner.invoke(
            cli,
            [
                "--config",
                str(config_path),
                "_autosync",
                *command,
            ],
        )
        assert result.exit_code == 0, result.output
    assert coordinator_calls == []


@pytest.mark.parametrize("mode", [OperatingMode.CACHE, OperatingMode.CONNECTED])
def test_game_stop_worker_runs_quick_sync_in_cached_and_direct_modes(
    tmp_path: Path, monkeypatch, mode: OperatingMode
):
    from romcloud.cli.commands import autosync as autosync_commands
    from romcloud.cli.main import cli

    config_path = tmp_path / "romcloud.toml"
    config = AppConfig(
            source=SourceConfig("local", (tmp_path / "roms").as_posix()),
            cache=CacheConfig((tmp_path / "cache").as_posix()),
            local_roms_path=(tmp_path / "local-roms").as_posix(),
            data_path=(tmp_path / "data").as_posix(),
            saves=SavesConfig(
                local_path=(tmp_path / "saves").as_posix(),
                auto_sync_enabled=True,
            ),
    )
    write_config(config, str(config_path))
    from romcloud.infrastructure.library_view import write_operating_mode
    write_operating_mode(config, mode)
    calls = []
    coordinator = type(
        "Coordinator", (),
        {
            "game_stop_eligible": lambda self, **kwargs: True,
            "game_stop": lambda self, **kwargs: calls.append(kwargs) or (),
        },
    )()
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
    assert len(calls) == 1


def test_ineligible_lifecycle_cli_returns_before_starting_progress_popup(
    tmp_path: Path, monkeypatch
):
    from romcloud.cli.commands import autosync as autosync_commands
    from romcloud.cli.main import cli

    config_path = tmp_path / "romcloud.toml"
    config = AppConfig(
        source=SourceConfig(
            "local", (tmp_path / "roms").as_posix(), selected_systems=("snes",)
        ),
        cache=CacheConfig((tmp_path / "cache").as_posix()),
        local_roms_path=(tmp_path / "local-roms").as_posix(),
        data_path=(tmp_path / "data").as_posix(),
        saves=SavesConfig(
            local_path=(tmp_path / "saves").as_posix(), auto_sync_enabled=True
        ),
    )
    write_config(config, str(config_path))
    calls = []
    coordinator = type(
        "Coordinator",
        (),
        {
            "game_stop_eligible": lambda self, **_kwargs: False,
            "game_stop": lambda self, **kwargs: calls.append(kwargs) or (),
        },
    )()
    monkeypatch.setattr(autosync_commands, "_coordinator", lambda _ctx: coordinator)
    monkeypatch.setattr(
        autosync_commands,
        "start_savesync_progress",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("ineligible exit started progress UI")
        ),
    )

    result = CliRunner().invoke(
        cli,
        [
            "--config",
            str(config_path),
            "_autosync",
            "game-stop",
            "ports",
            "pygame",
            "pygame",
            "Application.sh",
        ],
    )

    assert result.exit_code == 0, result.output
    assert calls == [
        {
            "system": "ports",
            "emulator": "pygame",
            "core": "pygame",
            "rom": "Application.sh",
        }
    ]


def test_game_start_skips_progress_popup_when_no_safe_target(
    tmp_path: Path, monkeypatch
):
    from romcloud.cli.commands import autosync as autosync_commands
    from romcloud.cli.main import cli

    config_path = tmp_path / "romcloud.toml"
    config = AppConfig(
        source=SourceConfig("local", (tmp_path / "roms").as_posix()),
        cache=CacheConfig((tmp_path / "cache").as_posix()),
        local_roms_path=(tmp_path / "local-roms").as_posix(),
        data_path=(tmp_path / "data").as_posix(),
        saves=SavesConfig(
            local_path=(tmp_path / "saves").as_posix(), auto_sync_enabled=True
        ),
    )
    write_config(config, str(config_path))
    calls = []
    coordinator = type(
        "Coordinator",
        (),
        {
            "game_start_eligible": lambda self, **_kwargs: False,
            "game_start": lambda self, **kwargs: calls.append(kwargs) or (),
        },
    )()
    monkeypatch.setattr(autosync_commands, "_auto_sync_enabled", lambda _config: True)
    monkeypatch.setattr(autosync_commands, "_coordinator", lambda _ctx: coordinator)
    monkeypatch.setattr(
        autosync_commands,
        "_start_lifecycle_progress",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("no-target launch started progress UI")
        ),
    )

    result = CliRunner().invoke(
        cli,
        [
            "--config",
            str(config_path),
            "_autosync",
            "game-start",
            "ports",
            "pygame",
            "pygame",
            "Application.sh",
        ],
    )

    assert result.exit_code == 0, result.output
    assert calls and "progress" not in calls[0]


def test_game_start_progress_closes_before_existing_conflict_popup(
    tmp_path: Path, monkeypatch
):
    from romcloud.cli.commands import autosync as autosync_commands
    from romcloud.cli.main import cli

    config_path = tmp_path / "romcloud.toml"
    config = AppConfig(
        source=SourceConfig("local", (tmp_path / "roms").as_posix()),
        cache=CacheConfig((tmp_path / "cache").as_posix()),
        local_roms_path=(tmp_path / "local-roms").as_posix(),
        data_path=(tmp_path / "data").as_posix(),
        saves=SavesConfig(
            local_path=(tmp_path / "saves").as_posix(), auto_sync_enabled=True
        ),
    )
    write_config(config, str(config_path))
    events = []

    class Progress:
        def close(self, ok, message=None):
            events.append(("close", ok, message))

        def wait_until_closed(self):
            events.append(("progress-closed",))

    progress = Progress()

    class Coordinator:
        def game_start_eligible(self, **_kwargs):
            return True

        def game_start(self, **kwargs):
            assert kwargs["progress"] is progress
            kwargs["progress"].close(True, "Save conflict found.")
            events.append(("sync-result", "conflict-id"))
            return ("conflict-id",)

    monkeypatch.setattr(autosync_commands, "_auto_sync_enabled", lambda _config: True)
    monkeypatch.setattr(autosync_commands, "_coordinator", lambda _ctx: Coordinator())
    monkeypatch.setattr(
        autosync_commands,
        "_start_lifecycle_progress",
        lambda root, **kwargs: events.append(("progress-start", root, kwargs))
        or progress,
    )
    monkeypatch.setattr(
        autosync_commands,
        "_launch_pending_conflict_popup",
        lambda root, **kwargs: events.append(("conflict-popup", root, kwargs)),
    )

    result = CliRunner().invoke(
        cli,
        [
            "--config",
            str(config_path),
            "_autosync",
            "game-start",
            "snes",
            "libretro",
            "snes9x",
            "Super Metroid.sfc",
        ],
    )

    assert result.exit_code == 0, result.output
    assert [event[0] for event in events] == [
        "progress-start",
        "close",
        "sync-result",
        "progress-closed",
        "conflict-popup",
    ]
    assert events[-1][2]["wait_for_frontend"] is False


def test_conflict_popup_worker_passes_exact_lifecycle_caller(
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
    launches = []
    monkeypatch.setenv("ROMCLOUD_AUTOSYNC_CALLER_PID", "4242")
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
            "conflict-popup",
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


def test_batocera_hook_waits_for_game_stop_and_detaches_only_followup_work(
    tmp_path: Path,
):
    target = tmp_path / "scripts" / "romcloud-autosync"
    install_hook(tmp_path / "bin" / "romcloud", hook_path=target)
    content = target.read_text(encoding="utf-8")

    assert "gameStart" in content and "gameStop" in content
    assert "game-start" in content and "game-stop" in content
    assert 'nohup "$ROMCLOUD_BIN" _autosync game-stop' not in content
    assert '"$ROMCLOUD_BIN" _autosync game-stop' in content
    assert 'nohup "$ROMCLOUD_BIN" _autosync conflict-popup' in content
    assert 'ROMCLOUD_AUTOSYNC_CALLER_PID="$PPID"' in content
    assert 'nohup "$ROMCLOUD_BIN" _autosync menu-loop' in content
    assert "auto-savesync-lifecycle.log" in content
    assert 'event="game_stop_hook_entered"' in content
    assert 'event="game_stop_sync_started"' in content
    assert 'event="game_stop_sync_completed"' in content
    assert 'event="game_stop_sync_failed"' in content
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
def test_game_stop_hook_returns_only_after_durable_worker_completion(tmp_path: Path):
    binary = tmp_path / "romcloud"
    committed = tmp_path / "remote-commit-receipt"
    binary.write_text(
        "#!/bin/bash\n"
        "if [[ \"$2\" == \"game-stop\" ]]; then\n"
        "  sleep 0.3\n"
        f"  printf committed > \"{committed}\"\n"
        "fi\n",
        encoding="utf-8",
    )
    binary.chmod(0o755)
    hook = install_hook(binary, hook_path=tmp_path / "romcloud-autosync")

    started = time.monotonic()
    subprocess.run(
        [str(hook), "gameStop", "psx", "libretro", "pcsx", "Game.chd"],
        check=True,
        timeout=2,
    )

    assert time.monotonic() - started >= 0.25
    assert committed.read_text(encoding="utf-8") == "committed"


def test_game_stop_cli_reports_failure_when_required_sync_does_not_complete(
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
        {
            "game_stop_eligible": lambda self, **_kwargs: True,
            "game_stop": lambda self, **_kwargs: (_ for _ in ()).throw(
                RuntimeError("remote commit failed")
            )
        },
    )()
    monkeypatch.setattr(autosync_commands, "_coordinator", lambda _ctx: coordinator)

    result = CliRunner().invoke(
        cli,
        [
            "--config",
            str(config_path),
            "_autosync",
            "game-stop",
            "snes",
            "libretro",
            "snes9x",
            "Super Metroid.sfc",
        ],
    )

    assert result.exit_code != 0
    assert "did not complete" in result.output


@pytest.mark.skipif(os.name == "nt", reason="Batocera hook is a POSIX shell script")
def test_game_stop_hook_logs_missing_worker_binary(tmp_path: Path):
    binary = tmp_path / "missing-romcloud"
    hook = install_hook(binary, hook_path=tmp_path / "romcloud-autosync")

    result = subprocess.run(
        [str(hook), "gameStop", "psx", "libretro", "pcsx", "Game.chd"],
        check=False,
        timeout=2,
    )

    assert result.returncode != 0
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
        {
            "dolphin-gc-memory-card-images",
            "dolphin-gc-gci-saves",
            "dolphin-save-states",
        }
    )
    assert layout_ids_for_session(policy, "wii") == frozenset(
        {"dolphin-wii-title-saves", "dolphin-save-states"}
    )
    assert layout_ids_for_session(policy, "psx", "duckstation") == frozenset(
        {"duckstation-memory-cards", "duckstation-root-sav"}
    )
    assert layout_ids_for_session(policy, "psx", "libretro", "pcsx-rearmed") == (
        frozenset({"retroarch-root-psx"})
    )
    assert layout_ids_for_session(policy, "saturn", "ymir", "ymir") == frozenset(
        {
            "ymir-global-backup-memory",
            "ymir-per-game-backup-memory",
            "ymir-save-states",
        }
    )
    assert layout_ids_for_session(policy, "xbox", "xemu") == frozenset()
    assert layout_ids_for_session(policy, "unknown-system") == frozenset()


def test_every_automatic_layout_round_trips_through_lifecycle_registry():
    """Registry additions fail if lifecycle cannot recover canonical ownership."""
    policy = DEFAULT_SAVE_SELECTION_POLICY
    for layout in policy.layouts:
        if not layout.lifecycle_enabled:
            continue
        lifecycle_system = (layout.lifecycle_systems or (layout.system,))[0]
        emulator = layout.lifecycle_emulators[0] if layout.lifecycle_emulators else ""
        core = layout.lifecycle_cores[0] if layout.lifecycle_cores else ""
        resolved = layout_ids_for_session(
            policy, lifecycle_system, emulator, core
        )
        assert layout.layout_id in resolved, (
            layout.layout_id,
            lifecycle_system,
            emulator,
            core,
        )
        assert policy.layout(layout.layout_id).system == layout.system


@pytest.mark.parametrize(
    ("event_system", "layout_id", "canonical_system"),
    (
        ("gba", "retroarch-root-gba", "gba"),
        ("genesis", "retroarch-root-megadrive", "megadrive"),
        ("segacd", "retroarch-root-megacd", "megacd"),
        ("vita", "vita3k-title-saves", "psvita"),
    ),
)
def test_lifecycle_aliases_resolve_one_canonical_ownership_domain(
    event_system: str,
    layout_id: str,
    canonical_system: str,
):
    policy = DEFAULT_SAVE_SELECTION_POLICY
    layout = policy.layout(layout_id)
    emulator = layout.lifecycle_emulators[0] if layout.lifecycle_emulators else ""
    core = layout.lifecycle_cores[0] if layout.lifecycle_cores else ""

    resolved = layout_ids_for_session(policy, event_system, emulator, core)

    assert layout_id in resolved
    assert {policy.layout(value).system for value in resolved} == {canonical_system}


@pytest.mark.parametrize(
    ("system", "emulator", "core"),
    (
        ("ports", "pygame", "pygame"),
        ("switch", "ryujinx", "ryujinx"),
    ),
    ids=(
        "unsupported-application",
        "unsupported-layout",
    ),
)
def test_ineligible_game_stop_is_total_savesync_noop_before_popup_or_service_access(
    tmp_path: Path,
    monkeypatch,
    system: str,
    emulator: str,
    core: str,
):
    provider = _Provider()
    service = _service(tmp_path, provider)
    coordinator = AutoSaveSyncCoordinator(
        service,
        data_root=tmp_path / "data",
        enabled=True,
        policy=DEFAULT_SAVE_SELECTION_POLICY,
        quiet_seconds=0,
    )
    coordinator.game_start(
        system=system, emulator=emulator, core=core, rom="Application.rom"
    )
    progress = _FakeProgress()

    @contextmanager
    def unexpected_observation_scope():
        raise AssertionError("ineligible gameStop entered SaveSync observation")
        yield

    monkeypatch.setattr(service, "observation_scope", unexpected_observation_scope)
    monkeypatch.setattr(
        service,
        "quick_sync",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("ineligible gameStop ran Quick Sync")
        ),
    )

    assert coordinator.game_stop(
        system=system,
        emulator=emulator,
        core=core,
        rom="Application.rom",
        progress=progress,
    ) == ()
    assert progress.calls == []
    assert provider.reachability_checks == 0
    assert not (tmp_path / "data/savesync-state.json").exists()
    assert coordinator._sessions.has_active_session() is False


def test_supported_game_stop_remains_synchronous_and_eligible(
    tmp_path: Path,
):
    provider = _Provider()
    service = _service(tmp_path, provider)
    coordinator = AutoSaveSyncCoordinator(
        service,
        data_root=tmp_path / "data",
        enabled=True,
        policy=DEFAULT_SAVE_SELECTION_POLICY,
        quiet_seconds=0,
    )
    local = tmp_path / "local/snes/Super Metroid.srm"
    remote = tmp_path / "remote/snes/Super Metroid.srm"
    _write(local, b"baseline")
    service.full_sync()
    local.write_bytes(b"eligible-final-save")

    coordinator.game_stop(
        system="snes",
        emulator="libretro",
        core="snes9x",
        rom="Super Metroid.sfc",
    )

    assert remote.read_bytes() == b"eligible-final-save"


def test_local_gba_auto_sync_ignores_rom_import_selection(
    tmp_path: Path, monkeypatch
):
    """ROM source selection must never gate a supported local save layout."""
    from romcloud.cli.commands import autosync as autosync_commands

    container, config_path = _container_with_rom_selection(
        tmp_path, selected_systems=("psx",)
    )
    service = container.saves
    service.full_sync()
    local = Path(container.config.saves.local_path) / "gba/Pokemon Emerald.srm"
    _write(local, b"local-gba-save")

    monkeypatch.setattr(
        autosync_commands, "get_container", lambda _ctx: container
    )
    coordinator = autosync_commands._coordinator(
        SimpleNamespace(obj={"config_path": str(config_path)})
    )
    coordinator._stability_interval = 0

    coordinator.game_stop(
        system="gba",
        emulator="libretro",
        core="mgba",
        rom="/userdata/roms/gba/Pokemon Emerald.gba",
    )

    assert container.config.source.selected_systems == ("psx",)
    assert (
        Path(container.config.remote_data.root)
        / "saves/gba/Pokemon Emerald.srm"
    ).read_bytes() == b"local-gba-save"


def test_game_stop_correlation_id_is_not_reused_as_transaction_id(
    tmp_path: Path, monkeypatch
):
    lifecycle_id = "game-stop-3215-1788998520"
    store = diagnostics.configure_diagnostics(tmp_path / "diagnostics.db")
    assert store is not None
    provider = _Provider()
    service = _service(tmp_path, provider)
    coordinator = _coordinator(tmp_path, service)
    local = tmp_path / "local/gba/Pokemon Fire Red.srm"
    remote = tmp_path / "remote/gba/Pokemon Fire Red.srm"
    _write(local, b"baseline")
    service.full_sync()
    coordinator.game_start(
        system="gba",
        emulator="libretro",
        core="mgba",
        rom="/userdata/roms/gba/Pokemon Fire Red.gba",
    )
    local.write_bytes(b"hardware-final-save")

    transaction_ids: list[str] = []
    original_prepare = save_transaction.prepare_transaction

    def capture_prepare(*args, **kwargs):
        transaction_ids.append(kwargs["operation_id"])
        return original_prepare(*args, **kwargs)

    monkeypatch.setattr(save_transaction, "prepare_transaction", capture_prepare)
    monkeypatch.setenv("ROMCLOUD_DIAGNOSTIC_OPERATION_ID", lifecycle_id)

    coordinator.game_stop(
        system="gba",
        emulator="libretro",
        core="mgba",
        rom="/userdata/roms/gba/Pokemon Fire Red.gba",
    )

    assert remote.read_bytes() == b"hardware-final-save"
    assert len(transaction_ids) == 1
    transaction_id = transaction_ids[0]
    assert len(transaction_id) == 32
    assert int(transaction_id, 16) >= 0
    assert transaction_id != lifecycle_id

    lifecycle_chain = store.operation_chain(lifecycle_id)
    assert lifecycle_chain
    assert all(event["operation_id"] == lifecycle_id for event in lifecycle_chain)
    assert any(event["event_code"] == "group.classified" for event in lifecycle_chain)

    transaction_chain = store.operation_chain(transaction_id)
    assert transaction_chain
    assert {
        "transaction.prepared",
        "transaction.applying",
        "transaction.promoted",
        "transaction.finalized",
    }.issubset({event["event_code"] for event in transaction_chain})
    assert {
        event["parent_operation_id"] for event in transaction_chain
    } == {lifecycle_id}
    assert all(
        event["metadata"].get("transaction_id") == transaction_id
        for event in transaction_chain
    )

    # The ordinary manual path still creates its own diagnostic and
    # transaction identifiers and sees the Auto work as fully reconciled.
    monkeypatch.delenv("ROMCLOUD_DIAGNOSTIC_OPERATION_ID")
    assert service.quick_sync().status == "unchanged"


def test_full_and_quick_sync_share_code_defined_supported_layout_boundary(
    tmp_path: Path,
):
    container, _config_path = _container_with_rom_selection(
        tmp_path, selected_systems=("psx",)
    )
    service = container.saves
    local_root = Path(container.config.saves.local_path)
    remote_root = Path(container.config.remote_data.root) / "saves"
    gba = local_root / "gba/Pokemon Emerald.srm"
    unsupported = local_root / "unsupported/Pokemon Emerald.sav"
    _write(gba, b"gba-baseline")
    _write(unsupported, b"must-not-sync")

    full = service.full_sync()

    assert full.uploaded == 1
    assert (remote_root / "gba/Pokemon Emerald.srm").read_bytes() == b"gba-baseline"
    assert not (remote_root / "unsupported/Pokemon Emerald.sav").exists()

    gba.write_bytes(b"gba-dirty")
    service.mark_local_dirty("gba/Pokemon Emerald.srm")
    quick = service.quick_sync()

    assert quick.status == "reconciled"
    assert quick.processed_groups == ("retroarch-root-gba/pokemon emerald",)
    assert (remote_root / "gba/Pokemon Emerald.srm").read_bytes() == b"gba-dirty"
    assert not (remote_root / "unsupported/Pokemon Emerald.sav").exists()


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


def test_two_client_snes_existing_save_game_stop_quick_and_full_converge(
    tmp_path: Path, caplog
):
    provider = _Provider()
    remote = tmp_path / "remote"

    def device(name: str) -> tuple[SaveSyncService, AutoSaveSyncCoordinator, Path]:
        root = tmp_path / name
        local = root / "saves"
        local.mkdir(parents=True)
        service = SaveSyncService(
            provider=provider,
            connectivity_root=str(tmp_path / "remote-data"),
            local_root=str(local),
            remote_root=str(remote),
            state_path=root / "data/savesync-state.json",
        )
        coordinator = AutoSaveSyncCoordinator(
            service,
            data_root=root / "data",
            enabled=True,
            policy=DEFAULT_SAVE_SELECTION_POLICY,
            quiet_seconds=0,
        )
        return service, coordinator, local

    laptop, _laptop_auto, laptop_root = device("laptop")
    main_pc, main_auto, main_root = device("main-pc")
    laptop_save = laptop_root / "snes/Super Metroid.srm"
    main_save = main_root / "snes/Super Metroid.srm"
    remote_save = remote / "snes/Super Metroid.srm"

    _write(laptop_save, b"laptop-original")
    laptop.full_sync()
    main_pc.full_sync()
    assert main_save.read_bytes() == b"laptop-original"

    caplog.clear()
    with caplog.at_level("INFO"):
        main_auto.game_start(
            system="snes",
            emulator="libretro",
            core="snes9x",
            rom="Super Metroid.sfc",
        )
        main_save.write_bytes(b"main-pc-newer")
        main_auto.game_stop(
            system="snes",
            emulator="libretro",
            core="snes9x",
            rom="Super Metroid.sfc",
        )

    assert remote_save.read_bytes() == b"main-pc-newer"
    trace = caplog.text
    for evidence in (
        "layout_ids=retroarch-root-snes",
        "candidate_groups=1",
        "classification=changed reason=manifest-diff-from-baseline",
        "decision=upload reason=local-diverged-remote-matches-baseline",
        "SaveSync transaction start:",
        "SaveSync transaction materialization committed:",
        "SaveSync baseline committed:",
        "SaveSync remote journal committed:",
        "Quick SaveSync (index) cursor committed:",
        "Auto SaveSync final result: trigger=game stop status=reconciled",
    ):
        assert evidence in trace
    assert "main-pc-newer" not in trace
    assert laptop.quick_sync().status == "reconciled"
    assert laptop_save.read_bytes() == b"main-pc-newer"
    full_report = laptop.full_sync()
    assert full_report.uploaded == 0
    assert full_report.downloaded == 0
    assert laptop_save.read_bytes() == remote_save.read_bytes() == b"main-pc-newer"


def test_game_stop_normal_uncontended_path_traces_every_required_step(
    tmp_path: Path, caplog
):
    """End-to-end proof for the normal, uncontended gameStop path: every one
    of the required observable steps (hook received, local discovery,
    per-group hash observation, dirty marker creation, worker lock
    acquisition, quick sync start, reconciliation decision, transaction
    commit, remote journal commit, cursor advancement, final result) is
    present in the durable log, in the order a real hardware trace would
    need to diagnose this exact bug."""
    provider = _Provider()
    service = _service(tmp_path, provider)
    coordinator = _coordinator(tmp_path, service)
    local = tmp_path / "local/snes/Super Metroid.srm"
    remote = tmp_path / "remote/snes/Super Metroid.srm"
    _write(local, b"baseline")
    service.full_sync()
    cursor_before = service.get_state().quick_sync_cursor_generation

    caplog.clear()
    with caplog.at_level("INFO"):
        coordinator.game_start(
            system="snes", emulator="libretro", core="snes9x", rom="Super Metroid.sfc"
        )
        local.write_bytes(b"final-save-bytes")
        conflict_ids = coordinator.game_stop(
            system="snes", emulator="libretro", core="snes9x", rom="Super Metroid.sfc"
        )

    assert conflict_ids == ()
    assert remote.read_bytes() == b"final-save-bytes"
    trace = caplog.text
    for evidence in (
        "gameStop received: system=snes emulator=libretro core=snes9x rom=Super Metroid.sfc",
        "gameStop local discovery started: layout_ids=retroarch-root-snes",
        "SaveSync local hash observation: group_id=",
        "SaveSync local classification: layout_id=retroarch-root-snes",
        "classification=changed reason=manifest-diff-from-baseline",
        "SaveSync local discovery: dirty marker created: group_id=",
        "Auto SaveSync worker lock acquired: trigger=game stop",
        "Auto SaveSync quick sync started: trigger=game stop",
        "decision=upload reason=local-diverged-remote-matches-baseline",
        "SaveSync transaction materialization committed:",
        "SaveSync remote journal committed:",
        "Quick SaveSync (index) cursor committed:",
        "Auto SaveSync final result: trigger=game stop status=reconciled",
    ):
        assert evidence in trace, f"missing required trace evidence: {evidence!r}"
    # Hardware-log tracing must never require reading save contents.
    assert "final-save-bytes" not in trace
    assert service.get_state().quick_sync_cursor_generation != cursor_before

    # Manual Quick Sync immediately afterward must see no remaining mutation.
    manual = service.quick_sync()
    assert manual.status == "unchanged"


def test_game_stop_waits_through_inflight_save_write_and_uploads_final_bytes(
    tmp_path: Path,
):
    """Prove the exact hardware-suspected race: the emulator/core is still
    writing the save (observably changing content) at the instant gameStop
    fires. This must not be classified as unchanged; gameStop must wait for
    real settling and then publish the true final bytes, not a torn/stale
    intermediate value."""
    provider = _Provider()
    service = _service(tmp_path, provider)
    coordinator = AutoSaveSyncCoordinator(
        service,
        data_root=tmp_path / "data",
        enabled=True,
        policy=DEFAULT_SAVE_SELECTION_POLICY,
        quiet_seconds=0.05,
    )
    local = tmp_path / "local/snes/Super Metroid.srm"
    remote = tmp_path / "remote/snes/Super Metroid.srm"
    _write(local, b"baseline")
    service.full_sync()
    coordinator.game_start(
        system="snes", emulator="libretro", core="snes9x", rom="Super Metroid.sfc"
    )

    def emulator_flush() -> None:
        time.sleep(0.02)
        local.write_bytes(b"torn-in-flight-write")
        time.sleep(0.06)
        local.write_bytes(b"true-final-save-bytes")

    writer = threading.Thread(target=emulator_flush)
    writer.start()
    try:
        conflict_ids = coordinator.game_stop(
            system="snes", emulator="libretro", core="snes9x", rom="Super Metroid.sfc"
        )
    finally:
        writer.join(timeout=5)

    assert conflict_ids == ()
    assert not writer.is_alive()
    assert remote.read_bytes() == b"true-final-save-bytes"
    assert local.read_bytes() == b"true-final-save-bytes"


def test_game_stop_never_stabilizing_save_is_conservatively_deferred(
    tmp_path: Path, caplog
):
    """Prove the conservative side of the same race: if the save never
    settles within the bounded window, gameStop must defer/fail loudly
    rather than silently classify a moving target as unchanged (false
    success) or publish a torn intermediate value. Nothing durable is
    marked dirty from an unstable pass, and a later, genuinely stable
    gameStop is not permanently blocked."""
    provider = _Provider()
    service = _service(tmp_path, provider)
    coordinator = AutoSaveSyncCoordinator(
        service,
        data_root=tmp_path / "data",
        enabled=True,
        policy=DEFAULT_SAVE_SELECTION_POLICY,
        quiet_seconds=0.03,
        stability_checks=3,
    )
    local = tmp_path / "local/snes/Super Metroid.srm"
    remote = tmp_path / "remote/snes/Super Metroid.srm"
    _write(local, b"baseline")
    service.full_sync()
    coordinator.game_start(
        system="snes", emulator="libretro", core="snes9x", rom="Super Metroid.sfc"
    )

    stop_writing = threading.Event()

    def never_settles() -> None:
        counter = 0
        while not stop_writing.is_set():
            local.write_bytes(f"still-writing-{counter}".encode())
            counter += 1
            time.sleep(0.01)

    writer = threading.Thread(target=never_settles)
    writer.start()
    try:
        with caplog.at_level("WARNING"):
            with pytest.raises(SaveSyncError, match="did not stabilize"):
                coordinator.game_stop(
                    system="snes",
                    emulator="libretro",
                    core="snes9x",
                    rom="Super Metroid.sfc",
                )
    finally:
        stop_writing.set()
        writer.join(timeout=5)

    assert "gameStop save stability timeout" in caplog.text
    assert (
        "status=deferred reason=local-data-unstable-pre-discovery" in caplog.text
    )
    assert remote.read_bytes() == b"baseline"
    assert all(
        group.condition is SaveGroupCondition.CLEAN
        for group in service.get_state().groups
    )

    # Once the save genuinely settles, a later gameStop is not locked out.
    local.write_bytes(b"finally-settled")
    coordinator.game_stop(
        system="snes", emulator="libretro", core="snes9x", rom="Super Metroid.sfc"
    )
    assert remote.read_bytes() == b"finally-settled"


class TestGameStopObservationCost:
    """Latency-focused regression tests for the synchronous gameStop path.

    These assert *operation counts* (how many times a byte of a save is read,
    how many settle windows are slept through, which roots are entered) rather
    than wall-clock thresholds, so they stay meaningful on any hardware.
    """

    @staticmethod
    def _counting_hash(monkeypatch) -> list[Path]:
        reads: list[Path] = []
        original = save_tree.hash_file

        def counted(path: Path) -> str:
            reads.append(Path(path))
            return original(path)

        monkeypatch.setattr(save_tree, "hash_file", counted)
        return reads

    @staticmethod
    def _counting_sleep(monkeypatch) -> list[float]:
        """Count settle windows without shortening them — the window is real
        elapsed time, so shortcutting it here would make the reuse under test
        look free when it is not."""
        slept: list[float] = []
        original = time.sleep

        def counted(seconds: float) -> None:
            slept.append(seconds)
            original(seconds)

        monkeypatch.setattr(
            "romcloud.services.auto_savesync.time.sleep", counted
        )
        return slept

    def _prepared(self, tmp_path: Path):
        provider = _Provider()
        service = _service(tmp_path, provider)
        coordinator = AutoSaveSyncCoordinator(
            service,
            data_root=tmp_path / "data",
            enabled=True,
            policy=DEFAULT_SAVE_SELECTION_POLICY,
            quiet_seconds=0.2,
        )
        return service, coordinator

    def test_no_change_game_stop_reads_each_local_save_exactly_once(
        self, tmp_path: Path, monkeypatch
    ):
        """The overwhelmingly common case — nothing changed — must not read
        the same save tree over and over. One content observation is reused by
        the second (stat-confirmed) stability observation and by discovery."""
        service, coordinator = self._prepared(tmp_path)
        local = tmp_path / "local/snes/Super Metroid.srm"
        _write(local, b"baseline")
        service.full_sync()
        coordinator.game_start(
            system="snes", emulator="libretro", core="snes9x", rom="Super Metroid.sfc"
        )

        reads = self._counting_hash(monkeypatch)
        self._counting_sleep(monkeypatch)
        assert (
            coordinator.game_stop(
                system="snes",
                emulator="libretro",
                core="snes9x",
                rom="Super Metroid.sfc",
            )
            == ()
        )

        assert reads == [local]

    def test_no_change_game_stop_sleeps_through_one_settle_window(
        self, tmp_path: Path, monkeypatch
    ):
        service, coordinator = self._prepared(tmp_path)
        _write(tmp_path / "local/snes/Super Metroid.srm", b"baseline")
        service.full_sync()
        coordinator.game_start(
            system="snes", emulator="libretro", core="snes9x", rom="Super Metroid.sfc"
        )

        slept = self._counting_sleep(monkeypatch)
        coordinator.game_stop(
            system="snes", emulator="libretro", core="snes9x", rom="Super Metroid.sfc"
        )

        assert len(slept) == 1
        assert slept[0] <= 0.2

    def test_changed_save_game_stop_does_not_sleep_a_second_settle_window(
        self, tmp_path: Path, monkeypatch
    ):
        """The pre-discovery settle already proved stability for this exact
        content; Quick Sync's own preflight must extend that proven quiet run
        with one fresh observation instead of restarting the whole window."""
        service, coordinator = self._prepared(tmp_path)
        local = tmp_path / "local/snes/Super Metroid.srm"
        remote = tmp_path / "remote/snes/Super Metroid.srm"
        _write(local, b"baseline")
        service.full_sync()
        coordinator.game_start(
            system="snes", emulator="libretro", core="snes9x", rom="Super Metroid.sfc"
        )
        local.write_bytes(b"final-save-bytes")

        slept = self._counting_sleep(monkeypatch)
        coordinator.game_stop(
            system="snes", emulator="libretro", core="snes9x", rom="Super Metroid.sfc"
        )

        assert len(slept) == 1
        assert remote.read_bytes() == b"final-save-bytes"

    def test_changed_save_game_stop_re_reads_only_what_correctness_requires(
        self, tmp_path: Path, monkeypatch
    ):
        """One tiny changed save: the local file is read once for the settle
        proof and once more by the post-mutation verification that must see
        real bytes on disk — never once per scan phase."""
        service, coordinator = self._prepared(tmp_path)
        local = tmp_path / "local/snes/Super Metroid.srm"
        remote = tmp_path / "remote/snes/Super Metroid.srm"
        _write(local, b"baseline")
        service.full_sync()
        coordinator.game_start(
            system="snes", emulator="libretro", core="snes9x", rom="Super Metroid.sfc"
        )
        local.write_bytes(b"final-save-bytes")

        reads = self._counting_hash(monkeypatch)
        # Attribute reads to the commit protocol's index/payload cross-check
        # so the extra remote read is proven to be *that* narrow verification
        # rather than an accidental extra scan somewhere else.
        cross_check_reads: list[Path] = []
        original_cross_check = service._observe_remote_group_manifests

        def counting_cross_check(intent_groups):
            start = len(reads)
            try:
                return original_cross_check(intent_groups)
            finally:
                cross_check_reads.extend(reads[start:])

        monkeypatch.setattr(
            service, "_observe_remote_group_manifests", counting_cross_check
        )
        self._counting_sleep(monkeypatch)
        coordinator.game_stop(
            system="snes", emulator="libretro", core="snes9x", rom="Super Metroid.sfc"
        )

        assert remote.read_bytes() == b"final-save-bytes"
        assert reads.count(local) == 2
        # Local content may be reused within one operation once its content
        # is proven stable; remote content is never reused at all — the
        # remote dataset may be a network-backed CIFS/SMB mount whose
        # metadata cannot prove another client did not rewrite a file since
        # an earlier observation. Every remote-touching phase (plan scan,
        # staging verification, transaction pre/post check, final
        # verification) therefore re-reads real bytes: this is the safety
        # floor, not a regression to hashing the whole remote tree.
        assert reads.count(remote) == 6
        # Exactly one of those six is the commit protocol's cross-check, and
        # it read only this group's own remote path. The other five are the
        # pre-existing verification phases, unchanged.
        assert cross_check_reads == [remote]
        assert reads.count(remote) - len(cross_check_reads) == 5

    def test_multiple_changed_files_in_one_group_are_each_read_once_per_phase(
        self, tmp_path: Path, monkeypatch
    ):
        service, coordinator = self._prepared(tmp_path)
        first = tmp_path / "local/psx/duckstation/memcards/shared_card_1.mcd"
        second = tmp_path / "local/psx/duckstation/memcards/shared_card_2.mcd"
        _write(first, b"card-one-baseline")
        _write(second, b"card-two-baseline")
        service.full_sync()
        coordinator.game_start(
            system="psx", emulator="libretro", core="swanstation", rom="FF7.chd"
        )
        first.write_bytes(b"card-one-final")
        second.write_bytes(b"card-two-final")

        reads = self._counting_hash(monkeypatch)
        self._counting_sleep(monkeypatch)
        coordinator.game_stop(
            system="psx", emulator="libretro", core="swanstation", rom="FF7.chd"
        )

        assert (
            tmp_path / "remote/psx/duckstation/memcards/shared_card_1.mcd"
        ).read_bytes() == b"card-one-final"
        assert (
            tmp_path / "remote/psx/duckstation/memcards/shared_card_2.mcd"
        ).read_bytes() == b"card-two-final"
        assert reads.count(first) == 2
        assert reads.count(second) == 2

    def test_narrow_game_stop_never_enters_unrelated_save_roots(
        self, tmp_path: Path, monkeypatch
    ):
        """One SNES game closing must not read a single byte of any other
        system's saves, however large that data is."""
        service, coordinator = self._prepared(tmp_path)
        snes = tmp_path / "local/snes/Super Metroid.srm"
        unrelated = tmp_path / "local/psx/duckstation/memcards/shared_card_1.mcd"
        _write(snes, b"baseline")
        _write(unrelated, b"unrelated-psx-memory-card")
        service.full_sync()
        coordinator.game_start(
            system="snes", emulator="libretro", core="snes9x", rom="Super Metroid.sfc"
        )
        snes.write_bytes(b"final-save-bytes")

        reads = self._counting_hash(monkeypatch)
        self._counting_sleep(monkeypatch)
        coordinator.game_stop(
            system="snes", emulator="libretro", core="snes9x", rom="Super Metroid.sfc"
        )

        assert unrelated not in reads
        assert not any("duckstation" in read.parts for read in reads), reads
        assert unrelated.read_bytes() == b"unrelated-psx-memory-card"

    def test_a_save_still_being_written_is_re_read_until_it_settles(
        self, tmp_path: Path, monkeypatch
    ):
        """Observation reuse must never short-circuit the settle proof: an
        actively changing file has to be re-read on every observation and the
        published bytes must be the final ones."""
        service, coordinator = self._prepared(tmp_path)
        local = tmp_path / "local/snes/Super Metroid.srm"
        remote = tmp_path / "remote/snes/Super Metroid.srm"
        _write(local, b"baseline")
        service.full_sync()
        coordinator.game_start(
            system="snes", emulator="libretro", core="snes9x", rom="Super Metroid.sfc"
        )

        reads = self._counting_hash(monkeypatch)
        observations = {"count": 0}
        original_observe = service.observe_local_layouts

        def flushing_observe(layout_ids):
            observations["count"] += 1
            if observations["count"] <= 2:
                local.write_bytes(
                    f"torn-in-flight-{observations['count']}".encode()
                )
            elif observations["count"] == 3:
                local.write_bytes(b"true-final-save-bytes")
            return original_observe(layout_ids)

        monkeypatch.setattr(service, "observe_local_layouts", flushing_observe)
        self._counting_sleep(monkeypatch)
        coordinator.game_stop(
            system="snes", emulator="libretro", core="snes9x", rom="Super Metroid.sfc"
        )

        assert observations["count"] >= 4
        assert reads.count(local) >= observations["count"]
        assert remote.read_bytes() == b"true-final-save-bytes"
        assert local.read_bytes() == b"true-final-save-bytes"

    def test_post_mutation_verification_always_re_reads_real_bytes(
        self, tmp_path: Path, monkeypatch
    ):
        """The final proof must come from disk, never from an earlier
        observation: corrupting the committed remote file behind ROMCloud's
        back has to be detected and the transaction rolled back."""
        service, coordinator = self._prepared(tmp_path)
        local = tmp_path / "local/snes/Super Metroid.srm"
        remote = tmp_path / "remote/snes/Super Metroid.srm"
        _write(local, b"baseline")
        service.full_sync()
        coordinator.game_start(
            system="snes", emulator="libretro", core="snes9x", rom="Super Metroid.sfc"
        )
        local.write_bytes(b"final-save-bytes")

        original_apply = save_transaction.apply_transaction

        def corrupting_apply(*args, **kwargs):
            result = original_apply(*args, **kwargs)
            remote.write_bytes(b"corrupted-by-something-else")
            return result

        monkeypatch.setattr(
            "romcloud.services.saves.save_transaction.apply_transaction",
            corrupting_apply,
        )
        self._counting_sleep(monkeypatch)

        with pytest.raises(SaveSyncError):
            coordinator.game_stop(
                system="snes",
                emulator="libretro",
                core="snes9x",
                rom="Super Metroid.sfc",
            )

        assert local.read_bytes() == b"final-save-bytes"

    def test_remote_change_mid_operation_is_never_hidden_by_reused_metadata(
        self, tmp_path: Path, monkeypatch
    ):
        """The critical reconciliation boundary: if the remote content
        actually changes partway through one gameStop operation (another
        client on the network wrote a same-size save between this
        operation's plan scan and its staging check), that must be detected
        rather than silently confirmed as unchanged from an earlier, reused
        observation and must never be silently overwritten by either side.
        Remote content is never cached, so every remote-touching phase
        re-reads real bytes and a genuine divergence is always seen — here,
        surfaced as a conflict between two independent changes."""
        service, coordinator = self._prepared(tmp_path)
        local = tmp_path / "local/snes/Super Metroid.srm"
        remote = tmp_path / "remote/snes/Super Metroid.srm"
        _write(local, b"baseline")
        service.full_sync()
        coordinator.game_start(
            system="snes", emulator="libretro", core="snes9x", rom="Super Metroid.sfc"
        )
        local.write_bytes(b"our-final-save-bytes")

        remote_scans = {"count": 0}
        original_scan_remote_layouts = service._scan_remote_layouts

        def injecting_scan_remote_layouts(layout_ids, **kwargs):
            remote_scans["count"] += 1
            if remote_scans["count"] == 2:
                # Simulate another client's write landing mid-operation, after
                # this operation's own plan scan already observed the old
                # remote bytes.
                remote.write_bytes(b"same-size-bytes-from-elsewhere")
            return original_scan_remote_layouts(layout_ids, **kwargs)

        monkeypatch.setattr(
            service, "_scan_remote_layouts", injecting_scan_remote_layouts
        )
        self._counting_sleep(monkeypatch)

        conflict_ids = coordinator.game_stop(
            system="snes", emulator="libretro", core="snes9x", rom="Super Metroid.sfc"
        )

        assert remote_scans["count"] >= 2
        assert conflict_ids != ()
        assert service.get_state().active_conflicts
        # Detecting the mid-operation change must never let either side's
        # write be silently overwritten by the other.
        assert local.read_bytes() == b"our-final-save-bytes"
        assert remote.read_bytes() == b"same-size-bytes-from-elsewhere"

    def test_narrowly_scoped_remote_observation_never_hashes_other_systems(
        self, tmp_path: Path, monkeypatch
    ):
        """Freshness (never caching remote content) must not regress into
        scanning the whole remote dataset: a narrow gameStop scope still
        never opens an unrelated system's remote save, however large."""
        service, coordinator = self._prepared(tmp_path)
        snes_local = tmp_path / "local/snes/Super Metroid.srm"
        psx_local = tmp_path / "local/psx/duckstation/memcards/shared_card_1.mcd"
        _write(snes_local, b"snes-baseline")
        _write(psx_local, b"unrelated-psx-memory-card")
        service.full_sync()
        coordinator.game_start(
            system="snes", emulator="libretro", core="snes9x", rom="Super Metroid.sfc"
        )
        snes_local.write_bytes(b"snes-final-save-bytes")

        reads = self._counting_hash(monkeypatch)
        self._counting_sleep(monkeypatch)
        coordinator.game_stop(
            system="snes", emulator="libretro", core="snes9x", rom="Super Metroid.sfc"
        )

        remote_psx = tmp_path / "remote/psx/duckstation/memcards/shared_card_1.mcd"
        assert remote_psx not in reads
        assert not any("duckstation" in read.parts for read in reads), reads
        assert remote_psx.read_bytes() == b"unrelated-psx-memory-card"


def test_snes_game_stop_never_invokes_container_reconciliation(tmp_path: Path):
    class _UnexpectedContainerRegistry:
        def get(self, _adapter_id):
            raise AssertionError("ordinary SNES saves must not use a container adapter")

    provider = _Provider()
    local = tmp_path / "local"
    local.mkdir()
    service = SaveSyncService(
        provider=provider,
        connectivity_root=str(tmp_path / "remote-data"),
        local_root=str(local),
        remote_root=str(tmp_path / "remote"),
        state_path=tmp_path / "data/savesync-state.json",
        container_registry=_UnexpectedContainerRegistry(),  # type: ignore[arg-type]
    )
    coordinator = _coordinator(tmp_path, service)
    save = local / "snes/Super Metroid.srm"
    _write(save, b"baseline")
    service.full_sync()
    coordinator.game_start(
        system="snes",
        emulator="libretro",
        core="snes9x",
        rom="Super Metroid.sfc",
    )
    save.write_bytes(b"ordinary-update")

    coordinator.game_stop(
        system="snes",
        emulator="libretro",
        core="snes9x",
        rom="Super Metroid.sfc",
    )

    assert (tmp_path / "remote/snes/Super Metroid.srm").read_bytes() == (
        b"ordinary-update"
    )


def test_gba_game_stop_uploads_and_periodic_quick_sync_repairs_materialization(
    tmp_path: Path,
):
    provider = _Provider()
    service = _service(tmp_path, provider)
    coordinator = AutoSaveSyncCoordinator(
        service,
        data_root=tmp_path / "data",
        enabled=True,
        policy=DEFAULT_SAVE_SELECTION_POLICY,
        quiet_seconds=0,
    )
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


def test_local_gba_game_stop_without_session_scans_only_canonical_gba_scope(
    tmp_path: Path,
    monkeypatch,
):
    """A scoped gameStop scan must not discard an authoritative addition
    merely because its filesystem mtime predates the lifecycle session."""
    provider = _Provider()
    service = _service(tmp_path, provider)
    coordinator = AutoSaveSyncCoordinator(
        service,
        data_root=tmp_path / "data",
        enabled=True,
        policy=DEFAULT_SAVE_SELECTION_POLICY,
        quiet_seconds=0,
    )
    service.full_sync()
    local = tmp_path / "local/gba/Pokemon Emerald.srm"
    remote = tmp_path / "remote/gba/Pokemon Emerald.srm"
    _write(local, b"new-save-with-preserved-mtime")
    old_timestamp = time.time() - 3600
    os.utime(local, (old_timestamp, old_timestamp))

    scanned_layouts: list[frozenset[str]] = []
    original_scan = service._scan_local_layouts

    def record_scoped_scan(layout_ids):
        scanned_layouts.append(layout_ids)
        return original_scan(layout_ids)

    monkeypatch.setattr(service, "_scan_local_layouts", record_scoped_scan)
    monkeypatch.setattr(
        service,
        "_scan_local",
        lambda: (_ for _ in ()).throw(
            AssertionError("gameStop performed an all-layout local scan")
        ),
    )
    dirty_at_quick = []
    quick_results = []
    original_quick = service.quick_sync

    def capture_dirty_then_quick(**kwargs):
        dirty_at_quick.extend(
            group
            for group in service.get_state().groups
            if group.dirty_path_hints
        )
        result = original_quick(**kwargs)
        quick_results.append(result)
        return result

    monkeypatch.setattr(service, "quick_sync", capture_dirty_then_quick)

    coordinator.game_stop(
        system="gba",
        emulator="libretro",
        core="mgba",
        rom="/userdata/roms/gba/Pokemon Emerald.gba",
    )

    assert scanned_layouts
    assert all(
        layouts == frozenset({"retroarch-root-gba"})
        for layouts in scanned_layouts
    )
    assert [group.group_id for group in dirty_at_quick] == [
        "retroarch-root-gba/pokemon emerald"
    ]
    assert quick_results[0].processed_groups == (
        "retroarch-root-gba/pokemon emerald",
    )
    assert remote.read_bytes() == b"new-save-with-preserved-mtime"


@pytest.mark.parametrize(
    ("system", "emulator", "core"),
    (
        ("", "libretro", "mgba"),
        ("unknown", "", ""),
    ),
)
def test_ambiguous_game_stop_identity_fails_closed_before_observation(
    tmp_path: Path,
    monkeypatch,
    system: str,
    emulator: str,
    core: str,
):
    provider = _Provider()
    service = _service(tmp_path, provider)
    coordinator = AutoSaveSyncCoordinator(
        service,
        data_root=tmp_path / "data",
        enabled=True,
        policy=DEFAULT_SAVE_SELECTION_POLICY,
        quiet_seconds=0,
    )
    monkeypatch.setattr(
        service,
        "observe_local_layouts",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("ambiguous gameStop entered local observation")
        ),
    )

    assert coordinator.game_stop(
        system=system,
        emulator=emulator,
        core=core,
        rom="/userdata/roms/gba/Pokemon Emerald.gba",
    ) == ()
    assert provider.reachability_checks == 0


def test_gba_game_stop_observes_late_sram_flush_within_bounded_settle_window(
    tmp_path: Path,
    caplog,
):
    provider = _Provider()
    service = _service(tmp_path, provider)
    coordinator = AutoSaveSyncCoordinator(
        service,
        data_root=tmp_path / "data",
        enabled=True,
        policy=DEFAULT_SAVE_SELECTION_POLICY,
        quiet_seconds=0.05,
        stability_checks=4,
    )
    local = tmp_path / "local/gba/Pokemon Emerald.srm"
    remote = tmp_path / "remote/gba/Pokemon Emerald.srm"
    _write(local, b"baseline")
    service.full_sync()
    coordinator.game_start(
        system="gba", emulator="libretro", core="mgba", rom="Pokemon Emerald.gba"
    )

    def delayed_sram_flush() -> None:
        time.sleep(0.02)
        local.write_bytes(b"post-hook-final-save")

    writer = threading.Thread(target=delayed_sram_flush)
    writer.start()
    try:
        with caplog.at_level("INFO"):
            coordinator.game_stop(
                system="gba",
                emulator="libretro",
                core="mgba",
                rom="Pokemon Emerald.gba",
            )
    finally:
        writer.join(timeout=5)

    assert not writer.is_alive()
    assert remote.read_bytes() == b"post-hook-final-save"
    assert "Scoped local save observation: attempt=1" in caplog.text
    assert "matches_previous=False" in caplog.text
    assert "matches_previous=True" in caplog.text
    assert '"canonical_path":"gba/Pokemon Emerald.srm"' in caplog.text
    assert '"mtime_ns":' in caplog.text
    assert "post-hook-final-save" not in caplog.text


def test_hook_logs_raw_gba_argv_with_shared_diagnostic_operation_id():
    content = hook_content(Path("/userdata/system/romcloud/bin/romcloud"))

    assert 'ROMCLOUD_DIAGNOSTIC_OPERATION_ID="game-stop-$$-$(date +%s)"' in content
    assert 'event="game_stop_argv"' in content
    assert 'system=%q emulator=%q core=%q rom=%q' in content


def test_manual_quick_sync_is_hint_driven_for_unmarked_gba_change(tmp_path: Path):
    provider = _Provider()
    service = _service(tmp_path, provider)
    local = tmp_path / "local/gba/Pokemon Emerald.srm"
    remote = tmp_path / "remote/gba/Pokemon Emerald.srm"
    _write(local, b"baseline")
    service.full_sync()
    local.write_bytes(b"changed-without-game-stop-marker")

    result = service.quick_sync()

    assert result.status == "unchanged"
    assert result.reason == "index-no-eligible-changes"
    assert remote.read_bytes() == b"baseline"

    full = service.full_sync()
    assert full.uploaded == 1
    assert remote.read_bytes() == b"changed-without-game-stop-marker"


def test_gba_game_stop_persists_canonical_dirty_domain_for_restart_quick_sync(
    tmp_path: Path,
    monkeypatch,
):
    provider = _Provider()
    service = _service(tmp_path, provider)
    coordinator = AutoSaveSyncCoordinator(
        service,
        data_root=tmp_path / "data",
        enabled=True,
        policy=DEFAULT_SAVE_SELECTION_POLICY,
        quiet_seconds=0,
    )
    local = tmp_path / "local/gba/Pokemon Emerald.srm"
    remote = tmp_path / "remote/gba/Pokemon Emerald.srm"
    _write(local, b"baseline")
    service.full_sync()
    coordinator.game_start(
        system="gba", emulator="libretro", core="mgba", rom="Pokemon Emerald.gba"
    )
    local.write_bytes(b"changed")

    def stop_after_mark(*_args, **_kwargs):
        raise SaveSyncConnectivityError("simulate process exit after durable mark")

    monkeypatch.setattr(service, "quick_sync", stop_after_mark)
    with pytest.raises(SaveSyncConnectivityError):
        coordinator.game_stop(
            system="gba",
            emulator="libretro",
            core="mgba",
            rom="Pokemon Emerald.gba",
        )

    persisted = service.get_state()
    dirty = [group for group in persisted.groups if group.dirty_path_hints]
    assert [(group.group_id, group.layout_id, group.dirty_path_hints) for group in dirty] == [
        (
            "retroarch-root-gba/pokemon emerald",
            "retroarch-root-gba",
            ("gba/Pokemon Emerald.srm",),
        )
    ]

    restarted = SaveSyncService(
        provider=provider,
        connectivity_root=str(tmp_path / "remote-data"),
        local_root=str(tmp_path / "local"),
        remote_root=str(tmp_path / "remote"),
        state_path=tmp_path / "data/savesync-state.json",
    )
    result = restarted.quick_sync()

    assert result.status == "reconciled"
    assert "retroarch-root-gba/pokemon emerald" in result.processed_groups
    assert remote.read_bytes() == b"changed"


def test_eden_metroid_game_stop_and_peer_quick_sync_use_physical_nand_roots(
    tmp_path: Path,
):
    provider = _Provider()
    remote = tmp_path / "remote"
    account = "0123456789ABCDEF0123456789ABCDEF"
    title = "010093801237C000"
    relative = Path("0000000000000000") / account / title / "slot_00" / "save.dat"

    def device(name: str) -> tuple[SaveSyncService, AutoSaveSyncCoordinator, Path]:
        root = tmp_path / name
        local = root / "saves"
        eden_save_root = root / "system/configs/eden/nand/user/save"
        local.mkdir(parents=True)
        service = SaveSyncService(
            provider=provider,
            connectivity_root=str(tmp_path / "remote-data"),
            local_root=str(local),
            remote_root=str(remote),
            state_path=root / "data/savesync-state.json",
            mapped_local_roots=(
                ("eden-switch-user-saves", str(eden_save_root), "yuzu"),
            ),
        )
        coordinator = AutoSaveSyncCoordinator(
            service,
            data_root=root / "data",
            enabled=True,
            policy=DEFAULT_SAVE_SELECTION_POLICY,
            quiet_seconds=0,
        )
        return service, coordinator, eden_save_root / relative

    service_a, coordinator_a, physical_a = device("device-a")
    service_b, _coordinator_b, physical_b = device("device-b")
    service_a.full_sync()
    service_b.full_sync()

    coordinator_a.game_start(
        system="switch", emulator="eden", core="eden", rom="Metroid Dread.xci"
    )
    _write(physical_a, b"metroid-progress")
    coordinator_a.game_stop(
        system="switch", emulator="eden", core="eden", rom="Metroid Dread.xci"
    )

    canonical = remote / "yuzu" / relative
    assert canonical.read_bytes() == b"metroid-progress"
    assert service_b.quick_sync().status == "reconciled"
    assert physical_b.read_bytes() == b"metroid-progress"
    assert not (tmp_path / "device-b/saves/yuzu").exists()

    coordinator_a.game_start(
        system="switch", emulator="eden", core="eden", rom="Metroid Dread.xci"
    )
    physical_a.write_bytes(b"metroid-progress-updated")
    coordinator_a.game_stop(
        system="switch", emulator="eden", core="eden", rom="Metroid Dread.xci"
    )
    assert service_b.quick_sync().status == "reconciled"
    assert physical_b.read_bytes() == b"metroid-progress-updated"

    coordinator_a.game_start(
        system="switch", emulator="eden", core="eden", rom="Metroid Dread.xci"
    )
    physical_a.unlink()
    coordinator_a.game_stop(
        system="switch", emulator="eden", core="eden", rom="Metroid Dread.xci"
    )
    assert service_b.quick_sync().status == "reconciled"
    assert not physical_b.exists()
    assert not canonical.exists()
    assert service_b.quick_sync().status == "unchanged"


def test_eden_switch_titles_merge_independently_but_same_title_conflicts(
    tmp_path: Path,
):
    provider = _Provider()
    remote = tmp_path / "remote"
    account = "0123456789ABCDEF0123456789ABCDEF"
    metroid = "010093801237C000"
    other_title = "01007EF00011E000"

    def device(name: str) -> tuple[SaveSyncService, AutoSaveSyncCoordinator, Path]:
        root = tmp_path / name
        local = root / "saves"
        eden_save_root = root / "system/configs/eden/nand/user/save"
        local.mkdir(parents=True)
        service = SaveSyncService(
            provider=provider,
            connectivity_root=str(tmp_path / "remote-data"),
            local_root=str(local),
            remote_root=str(remote),
            state_path=root / "data/savesync-state.json",
            mapped_local_roots=(
                ("eden-switch-user-saves", str(eden_save_root), "yuzu"),
            ),
        )
        return (
            service,
            AutoSaveSyncCoordinator(
                service,
                data_root=root / "data",
                enabled=True,
                policy=DEFAULT_SAVE_SELECTION_POLICY,
                quiet_seconds=0,
            ),
            eden_save_root / "0000000000000000" / account,
        )

    service_a, coordinator_a, account_a = device("device-a")
    service_b, coordinator_b, account_b = device("device-b")
    _write(account_a / metroid / "save.dat", b"metroid-base")
    _write(account_a / other_title / "save.dat", b"other-base")
    service_a.full_sync()
    service_b.full_sync()

    coordinator_a.game_start(
        system="switch", emulator="eden", core="eden", rom="Metroid Dread.xci"
    )
    coordinator_b.game_start(
        system="switch", emulator="eden", core="eden", rom="Other Game.xci"
    )
    (account_a / metroid / "save.dat").write_bytes(b"metroid-device-a")
    (account_b / other_title / "save.dat").write_bytes(b"other-device-b")
    coordinator_a.game_stop(
        system="switch", emulator="eden", core="eden", rom="Metroid Dread.xci"
    )
    assert coordinator_b.game_stop(
        system="switch", emulator="eden", core="eden", rom="Other Game.xci"
    ) == ()

    assert (account_b / metroid / "save.dat").read_bytes() == b"metroid-device-a"
    assert (account_a / other_title / "save.dat").read_bytes() == b"other-base"
    assert (
        remote / "yuzu/0000000000000000" / account / other_title / "save.dat"
    ).read_bytes() == b"other-device-b"
    assert service_a.quick_sync().status == "reconciled"
    assert (account_a / other_title / "save.dat").read_bytes() == b"other-device-b"

    coordinator_a.game_start(
        system="switch", emulator="eden", core="eden", rom="Metroid Dread.xci"
    )
    coordinator_b.game_start(
        system="switch", emulator="eden", core="eden", rom="Metroid Dread.xci"
    )
    (account_a / metroid / "save.dat").write_bytes(b"metroid-a-conflict")
    (account_b / metroid / "save.dat").write_bytes(b"metroid-b-conflict")
    coordinator_a.game_stop(
        system="switch", emulator="eden", core="eden", rom="Metroid Dread.xci"
    )
    conflicts = coordinator_b.game_stop(
        system="switch", emulator="eden", core="eden", rom="Metroid Dread.xci"
    )

    assert len(conflicts) == 1
    assert (account_b / metroid / "save.dat").read_bytes() == b"metroid-b-conflict"
    assert (
        remote / "yuzu/0000000000000000" / account / metroid / "save.dat"
    ).read_bytes() == b"metroid-a-conflict"
    assert service_b.get_state().active_conflicts[0].layout_id == (
        "yuzu-account-title-save"
    )


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
    # A peer device committed through the protocol while this session ran.
    seed_peer_commit(
        service,
        remote_root=tmp_path / "remote",
        relative_path="psx/Game.srm",
        content=b"peer-progress",
        device_id="peer",
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
    seed_peer_commit(
        service,
        remote_root=tmp_path / "remote",
        relative_path="psx/Game.srm",
        content=b"peer-progress",
        device_id="peer",
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
    seed_peer_commit(
        service,
        remote_root=tmp_path / "remote",
        relative_path="psx/Game.srm",
        content=b"remote-progress",
        device_id="peer",
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
    for name in ("Alpha", "Beta"):
        path = f"psx/{name}.srm"
        _write(tmp_path / "local" / path, f"local-{name}".encode())
        seed_peer_commit(
            service,
            remote_root=tmp_path / "remote",
            relative_path=path,
            content=f"remote-{name}".encode(),
            device_id="peer",
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
    # A pending edit for a *different* game than the one being launched.
    # gameStart's own targeted pre-launch sync only ever covers its own
    # resolved group ("retroarch-root-psx/game", from rom="Game.chd"), which
    # has no local file here and is therefore a no-op — it must never widen
    # to touch this unrelated group's pending edit.
    local = tmp_path / "local" / "psx" / "OtherGame.srm"
    remote = tmp_path / "remote" / "psx" / "OtherGame.srm"
    _write(local, b"base")
    service.full_sync()
    _write(local, b"changed")
    service.mark_local_dirty("psx/OtherGame.srm")
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


def test_game_stop_disconnect_fails_without_consuming_change_then_reconnects(
    tmp_path: Path,
):
    provider = _Provider()
    service = _service(tmp_path, provider)
    coordinator = _coordinator(tmp_path, service)
    local = tmp_path / "local/snes/Super Metroid.srm"
    remote = tmp_path / "remote/snes/Super Metroid.srm"
    _write(local, b"baseline")
    service.full_sync()
    cursor = service.get_state().quick_sync_cursor_generation
    coordinator.game_start(
        system="snes",
        emulator="libretro",
        core="snes9x",
        rom="Super Metroid.sfc",
    )
    local.write_bytes(b"unsynchronized-change")
    provider.reachable = False

    with pytest.raises(
        SaveSyncConnectivityError, match="remote-data storage is not readable"
    ):
        coordinator.game_stop(
            system="snes",
            emulator="libretro",
            core="snes9x",
            rom="Super Metroid.sfc",
        )

    state = service.get_state()
    assert state.quick_sync_cursor_generation == cursor
    assert state.groups[0].condition is SaveGroupCondition.LOCAL_DIRTY
    assert state.groups[0].dirty_path_hints == ("snes/Super Metroid.srm",)
    assert remote.read_bytes() == b"baseline"

    provider.reachable = True
    coordinator.remote_reconnect()

    assert remote.read_bytes() == b"unsynchronized-change"
    assert service.get_state().groups[0].condition is SaveGroupCondition.CLEAN


def test_failed_game_stop_transaction_retains_baseline_cursor_and_dirty_hint(
    tmp_path: Path, monkeypatch
):
    monkeypatch.setenv(
        "ROMCLOUD_DIAGNOSTIC_OPERATION_ID", "game-stop-failed-transaction"
    )
    provider = _Provider()
    service = _service(tmp_path, provider)
    coordinator = _coordinator(tmp_path, service)
    local = tmp_path / "local/snes/Super Metroid.srm"
    remote = tmp_path / "remote/snes/Super Metroid.srm"
    _write(local, b"baseline")
    service.full_sync()
    before = service.get_state()
    coordinator.game_start(
        system="snes",
        emulator="libretro",
        core="snes9x",
        rom="Super Metroid.sfc",
    )
    local.write_bytes(b"new-revision")
    original_apply = service._apply_selected_transaction
    monkeypatch.setattr(
        service,
        "_apply_selected_transaction",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("simulated remote commit failure")
        ),
    )

    with pytest.raises(RuntimeError, match="simulated remote commit failure"):
        coordinator.game_stop(
            system="snes",
            emulator="libretro",
            core="snes9x",
            rom="Super Metroid.sfc",
        )

    failed = service.get_state()
    assert failed.shared_manifest == before.shared_manifest
    assert failed.quick_sync_cursor_generation == before.quick_sync_cursor_generation
    assert failed.groups[0].condition is SaveGroupCondition.LOCAL_DIRTY
    assert failed.groups[0].dirty_path_hints == ("snes/Super Metroid.srm",)
    assert remote.read_bytes() == b"baseline"

    monkeypatch.setattr(service, "_apply_selected_transaction", original_apply)
    coordinator.remote_reconnect()

    assert remote.read_bytes() == b"new-revision"
    assert service.get_state().groups[0].condition is SaveGroupCondition.CLEAN


def test_game_stop_worker_busy_retains_dirty_state_and_drain_pending_completes_it(
    tmp_path: Path, monkeypatch
):
    """A gameStop that loses the worker lock to a live Quick Sync must not
    silently discard the change: durable dirty state is captured before the
    lock is even attempted, and the guaranteed drain-pending follow-up (what
    the CLI spawns detached on this exact failure) completes it once the
    busy operation releases the lock."""
    from romcloud.core.exceptions import SaveSyncWorkerBusyError

    provider = _Provider()
    service = _service(tmp_path, provider)
    coordinator = _coordinator(tmp_path, service)
    other_worker = _coordinator(tmp_path, service)
    psx_local = tmp_path / "local/psx/Game.srm"
    snes_local = tmp_path / "local/snes/Super Metroid.srm"
    snes_remote = tmp_path / "remote/snes/Super Metroid.srm"
    _write(psx_local, b"psx-base")
    _write(snes_local, b"baseline")
    service.full_sync()

    busy_entered = threading.Event()
    release_busy = threading.Event()
    original_quick_sync = service.quick_sync

    def slow_quick_sync(*args, **kwargs):
        busy_entered.set()
        assert release_busy.wait(timeout=2)
        return original_quick_sync(*args, **kwargs)

    monkeypatch.setattr(service, "quick_sync", slow_quick_sync)
    psx_local.write_bytes(b"psx-changed")
    service.mark_local_dirty("psx/Game.srm")
    busy_thread = threading.Thread(target=other_worker.drain_pending)
    busy_thread.start()
    assert busy_entered.wait(timeout=2)

    coordinator.game_start(
        system="snes", emulator="libretro", core="snes9x", rom="Super Metroid.sfc"
    )
    snes_local.write_bytes(b"main-pc-newer")
    with pytest.raises(SaveSyncWorkerBusyError):
        coordinator.game_stop(
            system="snes", emulator="libretro", core="snes9x", rom="Super Metroid.sfc"
        )

    # Not silently discarded: durable dirty state was captured before the
    # lock was even attempted, and the remote was never touched by this pass.
    snes_group = next(
        group
        for group in service.get_state().groups
        if group.layout_id == "retroarch-root-snes"
    )
    assert snes_group.condition is SaveGroupCondition.LOCAL_DIRTY
    assert snes_remote.read_bytes() == b"baseline"

    release_busy.set()
    busy_thread.join(timeout=5)
    assert not busy_thread.is_alive()
    assert (tmp_path / "remote/psx/Game.srm").read_bytes() == b"psx-changed"

    # The guaranteed follow-up (spawned detached by the CLI on this exact
    # failure) drains the retained work once the busy operation has released
    # the lock.
    coordinator.drain_pending()

    assert snes_remote.read_bytes() == b"main-pc-newer"
    snes_group = next(
        group
        for group in service.get_state().groups
        if group.layout_id == "retroarch-root-snes"
    )
    assert snes_group.condition is SaveGroupCondition.CLEAN


def test_game_stop_cli_schedules_drain_pending_follow_up_on_worker_busy(
    tmp_path: Path, monkeypatch
):
    from romcloud.cli.commands import autosync as autosync_commands
    from romcloud.cli.main import cli
    from romcloud.core.exceptions import SaveSyncWorkerBusyError

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
        {
            "game_stop_eligible": lambda self, **_kwargs: True,
            "game_stop": lambda self, **_kwargs: (_ for _ in ()).throw(
                SaveSyncWorkerBusyError("worker lock busy")
            )
        },
    )()
    monkeypatch.setattr(autosync_commands, "_coordinator", lambda _ctx: coordinator)
    spawn_calls = []
    monkeypatch.setattr(
        batocera_auto_savesync,
        "spawn_drain_pending",
        lambda **kwargs: spawn_calls.append(kwargs) or 4242,
    )

    result = CliRunner().invoke(
        cli,
        [
            "--config",
            str(config_path),
            "_autosync",
            "game-stop",
            "snes",
            "libretro",
            "snes9x",
            "Super Metroid.sfc",
        ],
    )

    assert result.exit_code != 0
    assert "did not complete" in result.output
    assert len(spawn_calls) == 1


def test_failed_compatibility_journal_after_commit_recovers_forward(
    tmp_path: Path, monkeypatch, caplog
):
    """After the ownership cutover the legacy journal is history only.

    The remote index is the commit point, so once it has published a
    verified payload the save is durably shared with every peer on the
    current protocol. A failing journal append must therefore degrade
    loudly and recover forward — rolling the committed bytes back to
    satisfy an old reader would destroy a save that other devices are
    already entitled to read. (The pre-cutover behavior, where the journal
    is the only discovery mechanism and a failed append *does* roll back,
    is covered by
    ``test_save_sync_service.py::TestLegacyDatasetCommitBehavior``.)
    """
    provider = _Provider()
    service = _service(tmp_path, provider)
    coordinator = _coordinator(tmp_path, service)
    local = tmp_path / "local/snes/Super Metroid.srm"
    remote = tmp_path / "remote/snes/Super Metroid.srm"
    _write(local, b"baseline")
    service.full_sync()
    coordinator.game_start(
        system="snes",
        emulator="libretro",
        core="snes9x",
        rom="Super Metroid.sfc",
    )
    local.write_bytes(b"new-revision")
    monkeypatch.setattr(
        service,
        "_append_remote_journal",
        lambda **_kwargs: (_ for _ in ()).throw(
            OSError("simulated journal commit failure")
        ),
    )

    with caplog.at_level("ERROR"):
        coordinator.game_stop(
            system="snes",
            emulator="libretro",
            core="snes9x",
            rom="Super Metroid.sfc",
        )

    # The verified payload stayed committed and was published to the index.
    assert remote.read_bytes() == b"new-revision"
    assert "SaveSync compatibility journal is degraded" in caplog.text
    assert "rollback=refused" in caplog.text
    index_root = savesync_index.default_index_root(tmp_path / "remote")
    head = savesync_index.load_head(index_root)
    assert head is not None
    shard = savesync_index.load_shard(
        index_root, "retroarch-root-snes", head.layouts["retroarch-root-snes"]
    )
    group = next(iter(shard.groups))
    assert group.artifacts[0].sha256 == hashlib.sha256(b"new-revision").hexdigest()
    # No dirty work is left pending: the change really did commit.
    assert service.get_state().groups[0].condition is SaveGroupCondition.CLEAN
    # The shared intent was retired rather than left blocking future syncs.
    assert savesync_commit.load_intent(index_root) is None


def test_nonfinal_game_stop_persists_dirty_work_until_last_session_stops(
    tmp_path: Path, monkeypatch
):
    provider = _Provider()
    service = _service(tmp_path, provider)
    coordinator = _coordinator(tmp_path, service)
    first = tmp_path / "local/snes/First.srm"
    second = tmp_path / "local/snes/Second.srm"
    _write(first, b"first-baseline")
    _write(second, b"second-baseline")
    service.full_sync()
    coordinator.game_start(
        system="snes", emulator="libretro", core="snes9x", rom="First.sfc"
    )
    coordinator.game_start(
        system="snes", emulator="libretro", core="snes9x", rom="Second.sfc"
    )
    first.write_bytes(b"first-new")
    quick_calls = 0
    original_quick_sync = service.quick_sync

    def counted_quick_sync(**kwargs):
        nonlocal quick_calls
        quick_calls += 1
        return original_quick_sync(**kwargs)

    monkeypatch.setattr(service, "quick_sync", counted_quick_sync)

    assert coordinator.game_stop(
        system="snes", emulator="libretro", core="snes9x", rom="First.sfc"
    ) == ()
    assert quick_calls == 0
    assert service.get_state().groups[0].condition is SaveGroupCondition.LOCAL_DIRTY
    assert (tmp_path / "remote/snes/First.srm").read_bytes() == b"first-baseline"

    coordinator.game_stop(
        system="snes", emulator="libretro", core="snes9x", rom="Second.sfc"
    )

    assert quick_calls == 1
    assert (tmp_path / "remote/snes/First.srm").read_bytes() == b"first-new"


def test_final_game_stop_cannot_succeed_with_unchanged_dirty_work(
    tmp_path: Path, monkeypatch
):
    provider = _Provider()
    service = _service(tmp_path, provider)
    coordinator = _coordinator(tmp_path, service)
    local = tmp_path / "local/snes/Super Metroid.srm"
    _write(local, b"baseline")
    service.full_sync()
    coordinator.game_start(
        system="snes",
        emulator="libretro",
        core="snes9x",
        rom="Super Metroid.sfc",
    )
    local.write_bytes(b"new-revision")
    cursor = service.get_state().quick_sync_cursor_generation
    monkeypatch.setattr(
        service,
        "quick_sync",
        lambda **_kwargs: SaveQuickSyncResult(
            status="unchanged",
            remote_generation=cursor or 0,
            cursor_before=cursor,
            cursor_after=cursor,
            reason="simulated-no-progress",
        ),
    )

    with pytest.raises(SaveSyncError, match="made no progress"):
        coordinator.game_stop(
            system="snes",
            emulator="libretro",
            core="snes9x",
            rom="Super Metroid.sfc",
        )

    state = service.get_state()
    assert state.groups[0].condition is SaveGroupCondition.LOCAL_DIRTY
    assert state.quick_sync_cursor_generation == cursor
    assert (tmp_path / "remote/snes/Super Metroid.srm").read_bytes() == b"baseline"


def test_offline_game_mode_retains_local_dirty_work_without_network(tmp_path: Path):
    provider = _Provider()
    service = _service(tmp_path, provider)
    path = tmp_path / "local" / "psx" / "Game.srm"
    _write(path, b"base")
    service.full_sync()
    _write(path, b"local")
    service.mark_local_dirty("psx/Game.srm")
    checks_before = provider.reachability_checks

    coordinator = AutoSaveSyncCoordinator(
        service,
        data_root=tmp_path / "data",
        enabled=False,
        policy=DEFAULT_SAVE_SELECTION_POLICY,
        quiet_seconds=0,
    )
    coordinator.drain_pending()

    assert provider.reachability_checks == checks_before
    assert (tmp_path / "remote" / "psx" / "Game.srm").read_bytes() == b"base"
    assert service.get_state().groups[0].condition is SaveGroupCondition.LOCAL_DIRTY


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
    _write(local, b"base")
    service.full_sync()

    seed_peer_commit(
        service,
        remote_root=tmp_path / "remote",
        relative_path="psx/Game.srm",
        content=b"remote-new",
        device_id="peer",
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
        return SaveQuickSyncResult(
            status="unchanged",
            remote_generation=0,
            cursor_before=0,
            cursor_after=0,
            reason="test-noop",
        )

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

    # The initial and first interval ticks were suppressed during gameplay.
    # This unsupported application's gameStop only retires its session marker;
    # the next forced-due menu tick is the sole Quick Sync.
    assert quick_calls == 1


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
    seed_peer_commit(
        service,
        remote_root=tmp_path / "remote",
        relative_path="psx/Game.srm",
        content=b"remote",
        device_id="peer",
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


class _FakeProgress:
    """Records every stage()/close() call in order, for asserting the exact
    sequence gameStop's Auto SaveSync progress popup would have shown."""

    def __init__(self, *, raise_on_call: bool = False) -> None:
        self.calls: list[tuple] = []
        self._raise_on_call = raise_on_call

    def stage(self, text: str) -> None:
        self.calls.append(("stage", text))
        if self._raise_on_call:
            raise RuntimeError("progress popup UI crashed")

    def close(self, ok: bool, message=None) -> None:
        self.calls.append(("close", ok, message))
        if self._raise_on_call:
            raise RuntimeError("progress popup UI crashed")

    @property
    def stages(self) -> list[str]:
        return [call[1] for call in self.calls if call[0] == "stage"]


class TestGameStopProgressPopup:
    """Requirement coverage for the gameStop Auto SaveSync progress popup:
    the popup is requested immediately, synchronous SaveSync behavior is
    unchanged, real stage/result text is surfaced, and any UI failure never
    affects the real SaveSync outcome."""

    def test_progress_is_requested_immediately_as_the_very_first_call(
        self, tmp_path: Path
    ):
        provider = _Provider()
        service = _service(tmp_path, provider)
        coordinator = _coordinator(tmp_path, service)
        local = tmp_path / "local/snes/Super Metroid.srm"
        _write(local, b"baseline")
        service.full_sync()
        local.write_bytes(b"final-save-bytes")

        progress = _FakeProgress()
        coordinator.game_stop(
            system="snes",
            emulator="libretro",
            core="snes9x",
            rom="Super Metroid.sfc",
            progress=progress,
        )

        assert progress.calls[0] == ("stage", "Checking save changes…")

    def test_synchronous_savesync_result_is_unchanged_by_the_popup(
        self, tmp_path: Path
    ):
        provider = _Provider()
        service = _service(tmp_path, provider)
        coordinator = _coordinator(tmp_path, service)
        local = tmp_path / "local/snes/Super Metroid.srm"
        remote = tmp_path / "remote/snes/Super Metroid.srm"
        _write(local, b"baseline")
        service.full_sync()
        local.write_bytes(b"final-save-bytes")

        progress = _FakeProgress()
        conflict_ids = coordinator.game_stop(
            system="snes",
            emulator="libretro",
            core="snes9x",
            rom="Super Metroid.sfc",
            progress=progress,
        )

        assert conflict_ids == ()
        assert remote.read_bytes() == b"final-save-bytes"

    def test_real_stage_progression_is_surfaced_for_an_uploaded_save(
        self, tmp_path: Path
    ):
        provider = _Provider()
        service = _service(tmp_path, provider)
        coordinator = _coordinator(tmp_path, service)
        local = tmp_path / "local/snes/Super Metroid.srm"
        _write(local, b"baseline")
        service.full_sync()
        local.write_bytes(b"final-save-bytes")

        progress = _FakeProgress()
        coordinator.game_stop(
            system="snes",
            emulator="libretro",
            core="snes9x",
            rom="Super Metroid.sfc",
            progress=progress,
        )

        assert progress.stages == [
            "Checking save changes…",
            "Waiting for save data to settle…",
            "Preparing save sync…",
            "Comparing save versions…",
            "Uploading save…",
            "Verifying save…",
            "Save sync complete.",
        ]
        assert progress.calls[-1] == ("close", True, None)

    def test_fast_no_change_path_still_shows_immediate_feedback_and_closes(
        self, tmp_path: Path
    ):
        provider = _Provider()
        service = _service(tmp_path, provider)
        coordinator = _coordinator(tmp_path, service)
        local = tmp_path / "local/snes/Super Metroid.srm"
        _write(local, b"baseline")
        service.full_sync()
        # No change at all since the last full sync.

        progress = _FakeProgress()
        conflict_ids = coordinator.game_stop(
            system="snes",
            emulator="libretro",
            core="snes9x",
            rom="Super Metroid.sfc",
            progress=progress,
        )

        assert conflict_ids == ()
        assert progress.stages[0] == "Checking save changes…"
        assert "No save changes detected." in progress.stages
        assert progress.calls[-1] == ("close", True, None)

    def test_failure_produces_a_visible_failure_state_without_a_traceback(
        self, tmp_path: Path, caplog
    ):
        provider = _Provider()
        service = _service(tmp_path, provider)
        coordinator = AutoSaveSyncCoordinator(
            service,
            data_root=tmp_path / "data",
            enabled=True,
            policy=DEFAULT_SAVE_SELECTION_POLICY,
            quiet_seconds=0.03,
            stability_checks=3,
        )
        local = tmp_path / "local/snes/Super Metroid.srm"
        _write(local, b"baseline")
        service.full_sync()
        coordinator.game_start(
            system="snes", emulator="libretro", core="snes9x", rom="Super Metroid.sfc"
        )

        stop_writing = threading.Event()

        def never_settles() -> None:
            counter = 0
            while not stop_writing.is_set():
                local.write_bytes(f"still-writing-{counter}".encode())
                counter += 1
                time.sleep(0.01)

        writer = threading.Thread(target=never_settles)
        writer.start()
        progress = _FakeProgress()
        try:
            with pytest.raises(SaveSyncError):
                coordinator.game_stop(
                    system="snes",
                    emulator="libretro",
                    core="snes9x",
                    rom="Super Metroid.sfc",
                    progress=progress,
                )
        finally:
            stop_writing.set()
            writer.join(timeout=5)

        assert progress.calls[-1] == (
            "close",
            False,
            "Save sync failed.\nYour local save has been preserved.",
        )
        # No internal traceback text ever reaches the popup.
        assert "Traceback" not in progress.calls[-1][2]

    def test_ui_failure_never_fails_savesync(self, tmp_path: Path):
        """A progress reporter whose stage()/close() raise must never abort
        or corrupt the real synchronous SaveSync operation."""
        provider = _Provider()
        service = _service(tmp_path, provider)
        coordinator = _coordinator(tmp_path, service)
        local = tmp_path / "local/snes/Super Metroid.srm"
        remote = tmp_path / "remote/snes/Super Metroid.srm"
        _write(local, b"baseline")
        service.full_sync()
        local.write_bytes(b"final-save-bytes")

        progress = _FakeProgress(raise_on_call=True)
        conflict_ids = coordinator.game_stop(
            system="snes",
            emulator="libretro",
            core="snes9x",
            rom="Super Metroid.sfc",
            progress=progress,
        )

        assert conflict_ids == ()
        assert remote.read_bytes() == b"final-save-bytes"

    def test_default_progress_is_a_safe_noop_when_none_is_provided(
        self, tmp_path: Path
    ):
        provider = _Provider()
        service = _service(tmp_path, provider)
        coordinator = _coordinator(tmp_path, service)
        local = tmp_path / "local/snes/Super Metroid.srm"
        remote = tmp_path / "remote/snes/Super Metroid.srm"
        _write(local, b"baseline")
        service.full_sync()
        local.write_bytes(b"final-save-bytes")

        conflict_ids = coordinator.game_stop(
            system="snes", emulator="libretro", core="snes9x", rom="Super Metroid.sfc"
        )

        assert conflict_ids == ()
        assert remote.read_bytes() == b"final-save-bytes"

    def test_conflict_handling_is_unaffected_by_the_progress_popup(
        self, tmp_path: Path
    ):
        """New conflicts discovered during gameStop are still returned for
        the existing conflict-popup handoff even with a progress reporter
        attached — no new conflict-resolution UI is invented here."""
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
        seed_peer_commit(
            service,
            remote_root=tmp_path / "remote",
            relative_path="psx/Game.srm",
            content=b"peer-progress",
            device_id="peer",
        )

        progress = _FakeProgress()
        conflict_ids = coordinator.game_stop(
            system="psx",
            emulator="libretro",
            core="pcsx",
            rom="Game.chd",
            progress=progress,
        )

        assert conflict_ids != ()
        assert service.get_state().groups[0].condition is SaveGroupCondition.CONFLICT
        # The shared progress result announces the transition, while the
        # existing focused popup still owns all resolution controls.
        assert "Save conflict found." in progress.stages

    def test_no_duplicate_quick_sync_operation_is_introduced_by_the_popup(
        self, tmp_path: Path, caplog
    ):
        provider = _Provider()
        service = _service(tmp_path, provider)
        coordinator = _coordinator(tmp_path, service)
        local = tmp_path / "local/snes/Super Metroid.srm"
        _write(local, b"baseline")
        service.full_sync()
        local.write_bytes(b"final-save-bytes")

        progress = _FakeProgress()
        with caplog.at_level("INFO"):
            coordinator.game_stop(
                system="snes",
                emulator="libretro",
                core="snes9x",
                rom="Super Metroid.sfc",
                progress=progress,
            )

        assert caplog.text.count("Auto SaveSync quick sync started: trigger=game stop") == 1
