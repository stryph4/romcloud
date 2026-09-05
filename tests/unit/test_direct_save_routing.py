from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from romcloud.core.exceptions import ModeTransitionError
from romcloud.core.save_selection import (
    DEFAULT_SAVE_SELECTION_POLICY,
    SaveSelectionPolicy,
)
from romcloud.core.storage import ProviderCapabilities
from romcloud.infrastructure.config import AppConfig, CacheConfig, SavesConfig, SourceConfig
from romcloud.infrastructure.providers.local import LocalFilesystemProvider
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


def _config(
    tmp_path: Path, *, selected_systems: tuple[str, ...] | None = None
) -> AppConfig:
    saves = tmp_path / "userdata" / "saves"
    data = tmp_path / "userdata" / "romcloud" / "data"
    saves.mkdir(parents=True)
    data.mkdir(parents=True)
    return AppConfig(
        source=SourceConfig(
            "local", str(tmp_path / "roms"), selected_systems=selected_systems
        ),
        cache=CacheConfig(str(tmp_path / "cache")),
        local_roms_path=str(tmp_path / "local-roms"),
        data_path=str(data),
        saves=SavesConfig(local_path=str(saves)),
    )


def test_direct_capability_is_distinct_and_conservative() -> None:
    policy = DEFAULT_SAVE_SELECTION_POLICY
    original_direct = {
        "mame-nvram",
        "mame-state",
        "pcsx2-legacy-states",
        "pcsx2-states",
        "ppsspp-savedata",
        "ppsspp-states",
    }
    classic_direct = {
        f"retroarch-root-{system}"
        for system in (
            "nes",
            "snes",
            "megadrive",
            "mastersystem",
            "gamegear",
            "gb",
            "gbc",
            "gba",
            "pcengine",
            "neogeo",
            "atari2600",
            "atari5200",
            "atari7800",
            "psx",
        )
    }
    assert original_direct.union(classic_direct).issubset(
        policy.direct_save_layout_ids()
    )
    for unsafe in (
        "retroarch-root-amiga500",
        "retroarch-root-dos",
        "n64-root",
        "nds-root",
        "duckstation-memory-cards",
        "rpcs3-savedata",
        "xemu-hdd",
        "xenia-content",
        "yuzu-account-title-save",
    ):
        assert not policy.layout(unsafe).direct_save_capable


@pytest.mark.parametrize(
    "system",
    ("nes", "snes", "megadrive", "gb", "gbc", "gba", "psx"),
)
def test_classic_layout_owns_one_complete_isolated_system_directory(
    system: str,
) -> None:
    layout = DEFAULT_SAVE_SELECTION_POLICY.layout(f"retroarch-root-{system}")

    assert layout.direct_save_capable
    assert layout.direct_route_root == system
    assert layout.recursive
    assert layout.eligible_files == ("*",)
    assert layout.direct_save_emulators == ("libretro",)
    assert layout.direct_save_requires_override is False
    assert layout.direct_save_categories == ("game-save", "save-state")


@pytest.mark.parametrize("core", ("pcsx_rearmed", "swanstation", "mednafen_psx"))
def test_ps1_direct_save_is_limited_to_current_batocera_libretro_cores(
    tmp_path: Path, core: str
) -> None:
    policy = DEFAULT_SAVE_SELECTION_POLICY

    assert policy.supports_direct_save_runtime(
        "retroarch-root-psx", emulator="libretro", core=core
    )
    route = DirectSaveRouting(
        _config(tmp_path, selected_systems=("psx",)),
        policy,
        tmp_path / "remote/saves",
        mount_operations=_FakeMounts(),
    ).planned_routes()
    assert len(route) == 1
    assert route[0].layout_id == "retroarch-root-psx"
    assert route[0].canonical_root == "psx"
    assert not policy.supports_direct_save_runtime(
        "retroarch-root-psx", emulator="duckstation", core="duckstation"
    )


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


def test_selected_classic_system_does_not_redirect_another_system(tmp_path: Path) -> None:
    config = _config(tmp_path, selected_systems=("nes",))
    remote = tmp_path / "remote" / "saves"
    remote.mkdir(parents=True)
    snes = Path(config.saves.local_path) / "snes/Game.srm"
    snes.parent.mkdir(parents=True)
    snes.write_bytes(b"snes-local")
    mounts = _FakeMounts()
    routing = DirectSaveRouting(
        config, DEFAULT_SAVE_SELECTION_POLICY, remote, mount_operations=mounts
    )

    routes = routing.activate()

    assert {route.layout_id for route in routes} == {"retroarch-root-nes"}
    assert set(mounts.bindings) == {Path(config.saves.local_path) / "nes"}
    assert snes.read_bytes() == b"snes-local"
    routing.deactivate()


def test_existing_manifest_remains_a_safe_subset_after_capability_expansion(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    remote = tmp_path / "remote" / "saves"
    remote.mkdir(parents=True)
    mounts = _FakeMounts()
    legacy_ids = {
        "mame-nvram",
        "mame-state",
        "pcsx2-legacy-states",
        "pcsx2-states",
        "ppsspp-savedata",
        "ppsspp-states",
    }
    legacy_policy = SaveSelectionPolicy(
        layouts=tuple(
            DEFAULT_SAVE_SELECTION_POLICY.layout(layout_id)
            for layout_id in sorted(legacy_ids)
        )
    )
    legacy = DirectSaveRouting(
        config, legacy_policy, remote, mount_operations=mounts
    )
    legacy.activate()
    manifest = Path(config.data_path) / MANIFEST_FILENAME
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    payload["version"] = 1
    manifest.write_text(json.dumps(payload), encoding="utf-8")

    expanded = DirectSaveRouting(
        config,
        DEFAULT_SAVE_SELECTION_POLICY,
        remote,
        mount_operations=mounts,
    )

    assert expanded.active
    assert expanded.layout_ids == legacy_ids
    assert Path(config.saves.local_path) / "nes" not in mounts.bindings
    expanded.recover_for_mode(direct=True)
    expanded.deactivate()
    assert mounts.bindings == {}


def test_current_manifest_cannot_silently_drop_an_owned_route(tmp_path: Path) -> None:
    config = _config(tmp_path, selected_systems=("nes", "snes"))
    remote = tmp_path / "remote" / "saves"
    remote.mkdir(parents=True)
    routing = DirectSaveRouting(
        config,
        DEFAULT_SAVE_SELECTION_POLICY,
        remote,
        mount_operations=_FakeMounts(),
    )
    routing.activate()
    manifest = Path(config.data_path) / MANIFEST_FILENAME
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    payload["routes"] = payload["routes"][:1]
    manifest.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ModeTransitionError, match="configuration changed"):
        _ = routing.active


def test_classic_round_trip_preserves_existing_and_direct_created_save(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path, selected_systems=("nes",))
    local = Path(config.saves.local_path)
    remote = tmp_path / "remote" / "saves"
    remote.mkdir(parents=True)
    save = local / "nes/Game.srm"
    save.parent.mkdir(parents=True)
    save.write_bytes(b"existing-local")
    service = SaveSyncService(
        provider=LocalFilesystemProvider(),
        connectivity_root=str(remote.parent),
        local_root=str(local),
        remote_root=str(remote),
        state_path=Path(config.data_path) / "savesync-state.json",
    )
    service.full_sync()
    mounts = _FakeMounts()
    routing = DirectSaveRouting(
        config, DEFAULT_SAVE_SELECTION_POLICY, remote, mount_operations=mounts
    )

    routing.activate()
    assert (remote / "nes/Game.srm").read_bytes() == b"existing-local"
    (remote / "nes/Game.srm").write_bytes(b"direct-created")
    shadow = service.with_local_root(routing.shadow_root)
    shadow.quick_sync(
        force_current_state=True,
        include_layout_ids=routing.layout_ids,
        authoritative_side="remote",
    )
    routing.deactivate()

    assert save.read_bytes() == b"direct-created"
    assert DEFAULT_SAVE_SELECTION_POLICY.is_included("nes", "Game.srm")

    # A second complete authority cycle must retain the same paths and data.
    service.quick_sync(
        force_current_state=True, include_layout_ids=routing.layout_ids
    )
    routing.activate()
    shadow = service.with_local_root(routing.shadow_root)
    shadow.quick_sync(
        force_current_state=True,
        include_layout_ids=routing.layout_ids,
        authoritative_side="remote",
    )
    routing.deactivate()
    assert save.read_bytes() == b"direct-created"


def test_direct_routing_never_mutates_user_retroarch_configuration(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path, selected_systems=("snes",))
    remote = tmp_path / "remote" / "saves"
    remote.mkdir(parents=True)
    user_config = (
        Path(config.saves.local_path).parent
        / "system/configs/retroarch/config/Snes9x/Snes9x.cfg"
    )
    user_config.parent.mkdir(parents=True)
    original = b'savefile_directory = "/custom/user/path"\n'
    user_config.write_bytes(original)
    routing = DirectSaveRouting(
        config,
        DEFAULT_SAVE_SELECTION_POLICY,
        remote,
        mount_operations=_FakeMounts(),
    )

    routing.activate()
    routing.deactivate()

    assert user_config.read_bytes() == original


def test_new_classic_layout_uses_existing_conflict_detection(tmp_path: Path) -> None:
    config = _config(tmp_path, selected_systems=("gba",))
    local = Path(config.saves.local_path)
    remote = tmp_path / "remote" / "saves"
    remote.mkdir(parents=True)
    local_save = local / "gba/Game.srm"
    local_save.parent.mkdir(parents=True)
    local_save.write_bytes(b"baseline")
    service = SaveSyncService(
        provider=LocalFilesystemProvider(),
        connectivity_root=str(remote.parent),
        local_root=str(local),
        remote_root=str(remote),
        state_path=Path(config.data_path) / "savesync-state.json",
    )
    service.full_sync()
    local_save.write_bytes(b"local-change")
    (remote / "gba/Game.srm").write_bytes(b"direct-change")

    result = service.quick_sync(
        force_current_state=True,
        include_layout_ids=frozenset({"retroarch-root-gba"}),
    )

    assert result.report is not None and result.report.conflicts == 1
    assert {
        conflict.layout_id for conflict in service.get_state().active_conflicts
    } == {"retroarch-root-gba"}


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


def test_direct_route_rejects_descendant_symlinks_without_following_them(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path, selected_systems=("nes",))
    remote = tmp_path / "remote" / "saves"
    remote.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    linked = Path(config.saves.local_path) / "nes/linked"
    linked.parent.mkdir(parents=True)
    try:
        linked.symlink_to(outside, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"symlinks unavailable: {exc}")
    routing = DirectSaveRouting(
        config,
        DEFAULT_SAVE_SELECTION_POLICY,
        remote,
        mount_operations=_FakeMounts(),
    )

    with pytest.raises(ModeTransitionError, match="refuses symlinked local save"):
        routing.activate()

    assert linked.is_symlink()
    assert not routing.active


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
