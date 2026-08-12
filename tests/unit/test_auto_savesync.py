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
from romcloud.infrastructure.config import (
    AppConfig,
    CacheConfig,
    SavesConfig,
    SourceConfig,
    write_config,
)
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


def test_batocera_hook_detaches_game_stop_but_keeps_start_marker_ordered(tmp_path: Path):
    target = tmp_path / "scripts" / "romcloud-autosync"
    install_hook(tmp_path / "bin" / "romcloud", hook_path=target)
    content = target.read_text(encoding="utf-8")

    assert "gameStart" in content and "gameStop" in content
    assert "game-start" in content and "game-stop" in content
    assert 'nohup "$ROMCLOUD_BIN" _autosync game-stop' in content
    assert "</dev/null &" in content
    assert '"$2" "$3" "$4" "$5"' in content
    if os.name != "nt":
        assert target.stat().st_mode & 0o111
    assert hook_content(tmp_path / "bin" / "romcloud") == content


@pytest.mark.skipif(os.name == "nt", reason="Batocera hook is a POSIX shell script")
def test_game_stop_hook_returns_without_waiting_for_background_worker(tmp_path: Path):
    binary = tmp_path / "romcloud"
    binary.write_text("#!/bin/bash\nsleep 2\n", encoding="utf-8")
    binary.chmod(0o755)
    hook = install_hook(binary, hook_path=tmp_path / "romcloud-autosync")

    started = time.monotonic()
    subprocess.run(
        [str(hook), "gameStop", "psx", "libretro", "pcsx", "Game.chd"],
        check=True,
        timeout=1,
    )

    assert time.monotonic() - started < 0.5


def test_lifecycle_mapping_is_registry_bounded_and_xemu_is_never_automatic():
    policy = DEFAULT_SAVE_SELECTION_POLICY

    assert layout_ids_for_session(policy, "gamecube") == frozenset(
        {"dolphin-gc-memory-card-images", "dolphin-gc-gci-saves"}
    )
    assert layout_ids_for_session(policy, "wii") == frozenset(
        {"dolphin-wii-title-saves"}
    )
    assert layout_ids_for_session(policy, "xbox", "xemu") == frozenset()
    assert layout_ids_for_session(policy, "unknown-system") == frozenset()


def test_game_exit_detects_first_save_and_uploads_only_that_registry_group(tmp_path: Path):
    provider = _Provider()
    service = _service(tmp_path, provider)
    coordinator = _coordinator(tmp_path, service)
    coordinator.game_start(system="psx", emulator="libretro", core="pcsx", rom="Game.chd")
    _write(tmp_path / "local" / "psx" / "Game.srm", b"new-save")

    coordinator.game_stop(
        system="psx", emulator="libretro", core="pcsx", rom="Game.chd"
    )

    assert (tmp_path / "remote" / "psx" / "Game.srm").read_bytes() == b"new-save"
    assert all(not group.dirty_path_hints for group in service.get_state().groups)


def test_active_game_group_is_deferred_then_processed_after_exit(tmp_path: Path):
    provider = _Provider()
    service = _service(tmp_path, provider)
    local = tmp_path / "local" / "psx" / "Game.srm"
    remote = tmp_path / "remote" / "psx" / "Game.srm"
    _write(local, b"base")
    service.reconcile()
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
    service.reconcile()
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


def test_offline_preserves_dirty_without_accessing_provider(tmp_path: Path):
    provider = _Provider()
    offline = CapabilityPolicy("smart_cache", OperatingMode.OFFLINE)
    service = _service(tmp_path, provider, capability_policy=offline)
    path = tmp_path / "local" / "psx" / "Game.srm"
    _write(path, b"local")
    service.mark_local_dirty("psx/Game.srm")

    _coordinator(tmp_path, service).drain_pending()

    assert provider.reachability_checks == 0
    assert service.get_state().groups[0].dirty_path_hints == ("psx/Game.srm",)
    assert not (tmp_path / "remote").exists()


def test_verified_unchanged_hint_clears_without_transaction(
    tmp_path: Path, monkeypatch
):
    provider = _Provider()
    service = _service(tmp_path, provider)
    path = tmp_path / "local" / "psx" / "Game.srm"
    _write(path, b"base")
    service.reconcile()
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
    service.reconcile()
    _write(path, b"changed")
    service.mark_local_dirty("psx/Game.srm")
    service.mark_local_dirty("psx/Game.srm")
    calls = 0
    original = service.reconcile_pending_groups

    def counted(*args, **kwargs):
        nonlocal calls
        calls += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(service, "reconcile_pending_groups", counted)
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
    service.reconcile()
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
    service.reconcile()
    _write(local, b"local")
    _write(remote, b"remote")
    service.mark_local_dirty("psx/Game.srm")

    _coordinator(tmp_path, service).drain_pending()

    assert local.read_bytes() == b"local"
    assert remote.read_bytes() == b"remote"
    state = service.get_state()
    assert state.groups[0].condition is SaveGroupCondition.CONFLICT
    assert len(tuple(conflict for conflict in state.conflicts if not conflict.resolved)) == 1


def test_remote_only_change_is_never_downloaded_by_game_exit(tmp_path: Path):
    provider = _Provider()
    service = _service(tmp_path, provider)
    local = tmp_path / "local" / "psx" / "Game.srm"
    remote = tmp_path / "remote" / "psx" / "Game.srm"
    _write(local, b"base")
    service.reconcile()
    _write(remote, b"remote")
    # Advisory false-positive: the authoritative pass must discover that only
    # the remote changed and must not promote it into the live save tree.
    service.mark_local_dirty("psx/Game.srm")

    _coordinator(tmp_path, service).drain_pending()

    assert local.read_bytes() == b"base"
    assert remote.read_bytes() == b"remote"
    assert service.get_state().groups[0].condition is SaveGroupCondition.REMOTE_DIRTY


def test_verified_local_deletion_removes_only_its_remote_group(tmp_path: Path):
    provider = _Provider()
    service = _service(tmp_path, provider)
    removed = tmp_path / "local" / "psx" / "Removed.srm"
    retained = tmp_path / "local" / "psx" / "Retained.srm"
    _write(removed, b"remove")
    _write(retained, b"retain")
    service.reconcile()
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
    service.reconcile()
    _write(first, b"next-1")
    service.mark_local_dirty("psx/First.srm")
    original = service.reconcile_pending_groups
    injected = False

    def reconcile_then_dirty(*args, **kwargs):
        nonlocal injected
        report = original(*args, **kwargs)
        if not injected:
            injected = True
            _write(second, b"next-2")
            service.mark_local_dirty("psx/Second.srm")
        return report

    monkeypatch.setattr(service, "reconcile_pending_groups", reconcile_then_dirty)

    _coordinator(tmp_path, service).drain_pending()

    assert (tmp_path / "remote" / "psx" / "First.srm").read_bytes() == b"next-1"
    assert (tmp_path / "remote" / "psx" / "Second.srm").read_bytes() == b"next-2"


def test_rapid_workers_never_reconcile_concurrently(tmp_path: Path, monkeypatch):
    provider = _Provider()
    service = _service(tmp_path, provider)
    path = tmp_path / "local" / "psx" / "Game.srm"
    _write(path, b"base")
    service.reconcile()
    _write(path, b"changed")
    service.mark_local_dirty("psx/Game.srm")
    original = service.reconcile_pending_groups
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

    monkeypatch.setattr(service, "reconcile_pending_groups", slow_reconcile)
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
    service.reconcile()
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
