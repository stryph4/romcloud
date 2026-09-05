from __future__ import annotations

import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from romcloud.core.exceptions import ModeTransitionError
from romcloud.core.save_selection import DEFAULT_SAVE_SELECTION_POLICY
from romcloud.core.storage import ProviderCapabilities
from romcloud.infrastructure.config import AppConfig, CacheConfig, SavesConfig, SourceConfig
from romcloud.integrations.batocera.direct_saves import (
    MANIFEST_FILENAME,
    BindMountOperations,
    DirectSaveRouting,
)
from romcloud.services.saves import SaveSyncService


class _FakeMounts:
    def __init__(
        self, *, fail_on: int | None = None, fail_unbind: bool = False
    ) -> None:
        self.bindings: dict[Path, Path] = {}
        self.calls = 0
        self.fail_on = fail_on
        self.fail_unbind = fail_unbind

    def bind(self, source: Path, target: Path) -> None:
        self.calls += 1
        if self.fail_on == self.calls:
            raise OSError("bind failed")
        self.bindings[target] = source

    def unbind(self, target: Path) -> None:
        if self.fail_unbind:
            raise OSError("unbind failed")
        if target not in self.bindings:
            raise OSError("not mounted")
        del self.bindings[target]

    def is_mount(self, target: Path) -> bool:
        return target in self.bindings

    def is_owned(self, source: Path, target: Path) -> bool:
        return self.bindings.get(target) == source


def _config(tmp_path: Path) -> AppConfig:
    saves = tmp_path / "userdata" / "saves"
    data = tmp_path / "userdata" / "romcloud" / "data"
    saves.mkdir(parents=True)
    data.mkdir(parents=True)
    return AppConfig(
        source=SourceConfig("local", str(tmp_path / "roms")),
        cache=CacheConfig(str(tmp_path / "cache")),
        local_roms_path=str(tmp_path / "local-roms"),
        data_path=str(data),
        saves=SavesConfig(local_path=str(saves)),
    )


def test_direct_capability_is_distinct_and_conservative() -> None:
    policy = DEFAULT_SAVE_SELECTION_POLICY
    assert policy.direct_save_layout_ids() == {
        "mame-nvram",
        "mame-state",
        "pcsx2-legacy-states",
        "pcsx2-states",
        "ppsspp-savedata",
        "ppsspp-states",
    }
    for unsafe in (
        "retroarch-root-snes",
        "duckstation-memory-cards",
        "rpcs3-savedata",
        "xemu-hdd",
        "xenia-content",
        "yuzu-account-title-save",
    ):
        assert not policy.layout(unsafe).direct_save_capable


def test_non_filesystem_provider_has_no_direct_routes(tmp_path: Path) -> None:
    routing = DirectSaveRouting(_config(tmp_path), DEFAULT_SAVE_SELECTION_POLICY, None)
    assert routing.available is False
    assert routing.planned_routes() == ()


def test_filesystem_provider_without_durable_handoff_stays_local(tmp_path: Path) -> None:
    remote = tmp_path / "remote" / "saves"
    remote.mkdir(parents=True)
    provider = SimpleNamespace(
        provider_id="filesystem-without-transactions",
        capabilities=ProviderCapabilities(has_filesystem_semantics=True),
    )
    service = SaveSyncService(
        provider=provider,
        connectivity_root=str(remote.parent),
        local_root=str(tmp_path / "local"),
        remote_root=str(remote),
        state_path=tmp_path / "state.json",
    )

    assert service.filesystem_remote_root is None


def test_routes_only_exact_audited_directories_and_restores_local_data(tmp_path: Path) -> None:
    config = _config(tmp_path)
    remote = tmp_path / "remote" / "saves"
    remote.mkdir(parents=True)
    local_file = Path(config.saves.local_path) / "ppsspp/PSP/SAVEDATA/GAME/save.bin"
    local_file.parent.mkdir(parents=True)
    local_file.write_bytes(b"local")
    untouched_empty = Path(config.saves.local_path) / "ppsspp/PPSSPP_STATE"
    untouched_empty.mkdir(parents=True)
    mounts = _FakeMounts()
    routing = DirectSaveRouting(
        config, DEFAULT_SAVE_SELECTION_POLICY, remote, mount_operations=mounts
    )

    routes = routing.activate()

    assert routes
    assert all(route.local_path != Path(config.saves.local_path) for route in routes)
    assert all(route.remote_path != remote for route in routes)
    assert not local_file.exists()
    shadow_file = routing.shadow_root / "ppsspp/PSP/SAVEDATA/GAME/save.bin"
    assert shadow_file.read_bytes() == b"local"
    assert len(mounts.bindings) == len(routes)

    routing.deactivate()
    assert local_file.read_bytes() == b"local"
    assert untouched_empty.is_dir()
    assert mounts.bindings == {}
    assert not routing.active


def test_partial_bind_failure_rolls_back_every_local_directory(tmp_path: Path) -> None:
    config = _config(tmp_path)
    remote = tmp_path / "remote" / "saves"
    remote.mkdir(parents=True)
    local_file = Path(config.saves.local_path) / "mame/nvram/game/data"
    local_file.parent.mkdir(parents=True)
    local_file.write_bytes(b"local")
    untouched_empty = Path(config.saves.local_path) / "ppsspp/PPSSPP_STATE"
    untouched_empty.mkdir(parents=True)
    mounts = _FakeMounts(fail_on=2)
    routing = DirectSaveRouting(
        config, DEFAULT_SAVE_SELECTION_POLICY, remote, mount_operations=mounts
    )

    with pytest.raises(ModeTransitionError, match="local save ownership was restored"):
        routing.activate()

    assert local_file.read_bytes() == b"local"
    assert untouched_empty.is_dir()
    assert mounts.bindings == {}
    assert not routing.active


def test_linux_bind_route_supports_new_files_and_atomic_replace(tmp_path: Path) -> None:
    if os.name != "posix" or not hasattr(os, "geteuid") or os.geteuid() != 0:
        pytest.skip("requires a privileged Linux mount namespace")
    source = tmp_path / "remote"
    target = tmp_path / "emulator-save"
    source.mkdir()
    target.mkdir()
    mounts = BindMountOperations()
    try:
        mounts.bind(source, target)
    except (OSError, subprocess.CalledProcessError):
        pytest.skip("bind mounts are unavailable in this test environment")
    try:
        (target / "new.tmp").write_bytes(b"new save")
        os.replace(target / "new.tmp", target / "game.sav")
        assert (source / "game.sav").read_bytes() == b"new save"
        assert mounts.is_owned(source, target)
    finally:
        if mounts.is_owned(source, target):
            mounts.unbind(target)


def test_failed_activation_rollback_preserves_recovery_manifest(tmp_path: Path) -> None:
    config = _config(tmp_path)
    remote = tmp_path / "remote" / "saves"
    remote.mkdir(parents=True)
    mounts = _FakeMounts(fail_on=2, fail_unbind=True)
    routing = DirectSaveRouting(
        config, DEFAULT_SAVE_SELECTION_POLICY, remote, mount_operations=mounts
    )

    with pytest.raises(ModeTransitionError, match="manifest was preserved"):
        routing.activate()

    assert routing.active
    assert mounts.bindings

    mounts.fail_unbind = False
    routing.deactivate()
    assert not routing.active
    assert mounts.bindings == {}


def test_recovery_never_recreates_a_missing_remote_directory(tmp_path: Path) -> None:
    config = _config(tmp_path)
    remote = tmp_path / "remote" / "saves"
    remote.mkdir(parents=True)
    mounts = _FakeMounts()
    routing = DirectSaveRouting(
        config, DEFAULT_SAVE_SELECTION_POLICY, remote, mount_operations=mounts
    )
    routes = routing.activate()
    missing = routes[0]
    mounts.bindings.clear()
    missing.remote_path.rmdir()

    with pytest.raises(ModeTransitionError, match="remote directory is unavailable"):
        routing.recover_for_mode(direct=True)

    assert not missing.remote_path.exists()


def test_recovery_rejects_a_symlinked_owned_manifest(tmp_path: Path) -> None:
    config = _config(tmp_path)
    manifest = Path(config.data_path) / MANIFEST_FILENAME
    manifest.parent.mkdir(parents=True, exist_ok=True)
    target = tmp_path / "unowned-manifest.json"
    target.write_text("{}", encoding="utf-8")
    try:
        manifest.symlink_to(target)
    except OSError as exc:
        pytest.skip(f"symlinks unavailable: {exc}")

    routing = DirectSaveRouting(
        config, DEFAULT_SAVE_SELECTION_POLICY, tmp_path / "remote"
    )

    with pytest.raises(ModeTransitionError, match="manifest is invalid"):
        routing.recover_for_mode(direct=True)


def test_unmanifested_existing_mount_is_never_adopted(tmp_path: Path) -> None:
    config = _config(tmp_path)
    remote = tmp_path / "remote" / "saves"
    remote.mkdir(parents=True)
    mounts = _FakeMounts()
    routing = DirectSaveRouting(
        config, DEFAULT_SAVE_SELECTION_POLICY, remote, mount_operations=mounts
    )
    route = routing.planned_routes()[0]
    mounts.bindings[route.local_path] = route.remote_path

    with pytest.raises(ModeTransitionError, match="without its owned manifest"):
        routing.activate()

    assert mounts.bindings[route.local_path] == route.remote_path
