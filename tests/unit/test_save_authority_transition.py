from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from romcloud.core.capabilities import OperatingMode
from romcloud.core.exceptions import (
    ModeTransitionError,
    ProviderNotReachableError,
    SaveAuthorityConflictError,
)
from romcloud.core.models.savesync import SaveQuickSyncResult
from romcloud.core.save_selection import DEFAULT_SAVE_SELECTION_POLICY
from romcloud.infrastructure.config import AppConfig, CacheConfig, SourceConfig
from romcloud.infrastructure.providers.local import LocalFilesystemProvider
from romcloud.integrations.batocera import game_access
from romcloud.services.auto_savesync import ActiveSessionStore
from romcloud.services.saves import SaveSyncService


def _config(tmp_path: Path) -> AppConfig:
    return AppConfig(
        source=SourceConfig("local", str(tmp_path / "roms")),
        cache=CacheConfig(str(tmp_path / "cache")),
        local_roms_path=str(tmp_path / "local-roms"),
        data_path=str(tmp_path / "data"),
    )


def _real_saves(tmp_path: Path) -> SaveSyncService:
    local_root = tmp_path / "local-saves"
    remote_root = tmp_path / "remote-saves"
    local_root.mkdir()
    remote_root.mkdir()
    return SaveSyncService(
        provider=LocalFilesystemProvider(),
        connectivity_root=str(remote_root),
        local_root=str(local_root),
        remote_root=str(remote_root),
        state_path=tmp_path / "data/savesync-state.json",
    )


def _write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)


class _Saves:
    selection_policy = DEFAULT_SAVE_SELECTION_POLICY
    filesystem_remote_root = Path("/remote/saves")

    def __init__(self, conflicts=(), *, quick_error: Exception | None = None):
        self.conflicts = list(conflicts)
        self.calls = []
        self.quick_error = quick_error

    def quick_sync(self, **kwargs):
        self.calls.append(("quick", kwargs))
        if self.quick_error is not None:
            raise self.quick_error
        return SaveQuickSyncResult("reconciled", 1, 0, 1)

    def get_state(self):
        return SimpleNamespace(active_conflicts=tuple(self.conflicts))

    def resolve_conflict(self, conflict_id, resolution, **kwargs):
        self.calls.append(("resolve", conflict_id, resolution))
        self.conflicts = [item for item in self.conflicts if item.conflict_id != conflict_id]

    def with_local_root(self, root):
        self.calls.append(("shadow", root))
        return self


class _Routing:
    available = True
    active = False
    layout_ids = frozenset({"ppsspp-savedata"})
    shadow_root = Path("/shadow")

    def __init__(self):
        self.calls = []

    def activate(self):
        self.calls.append("activate")
        self.active = True

    def deactivate(self):
        self.calls.append("deactivate")
        self.active = False

    def recover_for_mode(self, *, direct):
        self.calls.append(f"recover-{direct}")


def _wire(monkeypatch, saves, routing):
    monkeypatch.setattr(game_access, "Container", lambda *args, **kwargs: SimpleNamespace(saves=saves))
    monkeypatch.setattr(game_access, "_direct_save_routing", lambda config, container: routing)


def test_cache_to_direct_forces_current_scan_before_routing(tmp_path, monkeypatch):
    saves, routing = _Saves(), _Routing()
    _wire(monkeypatch, saves, routing)

    game_access._prepare_save_authority_transition(
        _config(tmp_path), OperatingMode.CACHE, OperatingMode.CONNECTED, None,
        conflict_action="stop",
    )

    assert saves.calls[0][0] == "quick"
    assert saves.calls[0][1]["force_current_state"] is True
    assert saves.calls[0][1]["include_layout_ids"] == routing.layout_ids
    assert routing.calls == ["activate"]


@pytest.mark.parametrize(
    ("case", "expected_uploaded", "expected_downloaded", "expected_unchanged"),
    (
        ("local-only", 1, 0, 0),
        ("remote-only", 0, 1, 0),
        ("identical", 0, 0, 1),
    ),
)
def test_idle_cache_to_direct_reconciles_only_direct_layout_scope_and_routes(
    tmp_path,
    monkeypatch,
    case,
    expected_uploaded,
    expected_downloaded,
    expected_unchanged,
):
    saves = _real_saves(tmp_path)
    local = tmp_path / "local-saves/ppsspp/PSP/SAVEDATA/GAME/save.bin"
    remote = tmp_path / "remote-saves/ppsspp/PSP/SAVEDATA/GAME/save.bin"
    if case in {"local-only", "identical"}:
        _write(local, b"direct-save")
    if case in {"remote-only", "identical"}:
        _write(remote, b"direct-save")

    # These are valid SaveSync artifacts, but this layout retains local save
    # ownership in Direct Mode.  It must not enter the transition snapshots.
    unrelated_local = tmp_path / "local-saves/amiga500/Other.srm"
    unrelated_remote = tmp_path / "remote-saves/amiga500/Other.srm"
    _write(unrelated_local, b"unrelated-local")
    _write(unrelated_remote, b"unrelated-remote")

    routing = _Routing()
    _wire(monkeypatch, saves, routing)

    _, changed, result = game_access._prepare_save_authority_transition(
        _config(tmp_path),
        OperatingMode.CACHE,
        OperatingMode.CONNECTED,
        None,
        conflict_action="stop",
    )

    assert changed is True
    assert routing.calls == ["activate"]
    assert result.report is not None
    assert result.report.uploaded == expected_uploaded
    assert result.report.downloaded == expected_downloaded
    assert result.report.unchanged == expected_unchanged
    assert local.read_bytes() == b"direct-save"
    assert remote.read_bytes() == b"direct-save"
    assert unrelated_local.read_bytes() == b"unrelated-local"
    assert unrelated_remote.read_bytes() == b"unrelated-remote"


def test_idle_cache_to_direct_preserves_legitimate_conflict_before_routing(
    tmp_path, monkeypatch
):
    saves = _real_saves(tmp_path)
    local = tmp_path / "local-saves/ppsspp/PSP/SAVEDATA/GAME/save.bin"
    remote = tmp_path / "remote-saves/ppsspp/PSP/SAVEDATA/GAME/save.bin"
    _write(local, b"baseline")
    saves.full_sync()
    local.write_bytes(b"local-version")
    remote.write_bytes(b"remote-version")
    _write(tmp_path / "local-saves/amiga500/Other.srm", b"unrelated")

    routing = _Routing()
    _wire(monkeypatch, saves, routing)

    with pytest.raises(SaveAuthorityConflictError) as caught:
        game_access._prepare_save_authority_transition(
            _config(tmp_path),
            OperatingMode.CACHE,
            OperatingMode.CONNECTED,
            None,
            conflict_action="stop",
        )

    assert caught.value.conflict_ids
    assert routing.calls == []
    assert local.read_bytes() == b"local-version"
    assert remote.read_bytes() == b"remote-version"


def test_conflict_stops_before_direct_routing(tmp_path, monkeypatch):
    conflict = SimpleNamespace(conflict_id="conflict", layout_id="ppsspp-savedata")
    saves, routing = _Saves((conflict,)), _Routing()
    _wire(monkeypatch, saves, routing)

    with pytest.raises(SaveAuthorityConflictError) as caught:
        game_access._prepare_save_authority_transition(
            _config(tmp_path), OperatingMode.OFFLINE, OperatingMode.CONNECTED, None,
            conflict_action="stop",
        )

    assert caught.value.conflict_ids == ("conflict",)
    assert routing.calls == []


def test_explicit_remote_wins_resolves_before_routing(tmp_path, monkeypatch):
    conflict = SimpleNamespace(conflict_id="conflict", layout_id="ppsspp-savedata")
    saves, routing = _Saves((conflict,)), _Routing()
    _wire(monkeypatch, saves, routing)

    game_access._prepare_save_authority_transition(
        _config(tmp_path), OperatingMode.CACHE, OperatingMode.CONNECTED, None,
        conflict_action="remote-wins",
    )

    assert [call[0] for call in saves.calls] == ["quick", "resolve", "quick"]
    assert routing.calls == ["activate"]


def test_direct_exit_materializes_shadow_before_unrouting(tmp_path, monkeypatch):
    saves, routing = _Saves(), _Routing()
    routing.active = True
    _wire(monkeypatch, saves, routing)

    game_access._prepare_save_authority_transition(
        _config(tmp_path), OperatingMode.CONNECTED, OperatingMode.OFFLINE, None,
        conflict_action="stop",
    )

    assert saves.calls[0] == ("shadow", routing.shadow_root)
    assert saves.calls[1][0] == "quick"
    assert saves.calls[1][1]["authoritative_side"] == "remote"
    assert routing.calls == ["recover-True", "deactivate"]


def test_unsupported_layouts_do_not_create_an_authority_handoff(tmp_path, monkeypatch):
    unsupported = SimpleNamespace(
        conflict_id="retroarch-conflict", layout_id="retroarch-root-amiga500"
    )
    saves, routing = _Saves((unsupported,)), _Routing()
    routing.available = False
    routing.layout_ids = frozenset()
    _wire(monkeypatch, saves, routing)

    game_access._prepare_save_authority_transition(
        _config(tmp_path), OperatingMode.CACHE, OperatingMode.CONNECTED, None,
        conflict_action="remote-wins",
    )

    assert saves.calls == []
    assert saves.conflicts == [unsupported]
    assert routing.calls == []


def test_remote_wins_leaves_unsupported_conflicts_untouched(tmp_path, monkeypatch):
    unsupported = SimpleNamespace(
        conflict_id="retroarch-conflict", layout_id="retroarch-root-amiga500"
    )
    saves, routing = _Saves((unsupported,)), _Routing()
    _wire(monkeypatch, saves, routing)

    game_access._prepare_save_authority_transition(
        _config(tmp_path), OperatingMode.CACHE, OperatingMode.CONNECTED, None,
        conflict_action="remote-wins",
    )

    assert [call[0] for call in saves.calls] == ["quick"]
    assert saves.conflicts == [unsupported]
    assert routing.calls == ["activate"]


def test_direct_exit_sync_failure_preserves_remote_routing(tmp_path, monkeypatch):
    saves = _Saves(quick_error=RuntimeError("materialization failed"))
    routing = _Routing()
    routing.active = True
    _wire(monkeypatch, saves, routing)

    with pytest.raises(RuntimeError, match="materialization failed"):
        game_access._prepare_save_authority_transition(
            _config(tmp_path), OperatingMode.CONNECTED, OperatingMode.CACHE, None,
            conflict_action="stop",
        )

    assert routing.active is True
    assert routing.calls == ["recover-True"]


def test_authority_handoff_is_blocked_for_an_active_direct_layout(
    tmp_path, monkeypatch
):
    config = _config(tmp_path)
    ActiveSessionStore(Path(config.data_path)).start(
        system="psp", emulator="ppsspp", core="ppsspp", rom="Game.iso"
    )
    saves, routing = _Saves(), _Routing()
    _wire(monkeypatch, saves, routing)

    with pytest.raises(ModeTransitionError, match="while a game is using"):
        game_access._prepare_save_authority_transition(
            config, OperatingMode.CACHE, OperatingMode.CONNECTED, None,
            conflict_action="stop",
        )

    assert saves.calls == []
    assert routing.calls == []


def test_active_classic_libretro_layout_blocks_its_authority_handoff(
    tmp_path, monkeypatch
):
    config = _config(tmp_path)
    ActiveSessionStore(Path(config.data_path)).start(
        system="nes", emulator="libretro", core="fceumm", rom="Game.nes"
    )
    saves, routing = _Saves(), _Routing()
    routing.layout_ids = frozenset({"retroarch-root-nes"})
    _wire(monkeypatch, saves, routing)

    with pytest.raises(ModeTransitionError, match="retroarch-root-nes"):
        game_access._prepare_save_authority_transition(
            config, OperatingMode.CACHE, OperatingMode.CONNECTED, None,
            conflict_action="stop",
        )

    assert saves.calls == []
    assert routing.calls == []


def test_active_local_only_layout_does_not_block_direct_rom_transition(
    tmp_path, monkeypatch
):
    config = _config(tmp_path)
    ActiveSessionStore(Path(config.data_path)).start(
        system="amiga500", emulator="libretro", core="puae", rom="Game.adf"
    )
    saves, routing = _Saves(), _Routing()
    _wire(monkeypatch, saves, routing)

    game_access._prepare_save_authority_transition(
        config, OperatingMode.CACHE, OperatingMode.CONNECTED, None,
        conflict_action="stop",
    )

    assert [call[0] for call in saves.calls] == ["quick"]
    assert routing.calls == ["activate"]


def test_provider_failure_keeps_specific_type_message_and_emits_actionable_progress(
    tmp_path, monkeypatch
):
    config = _config(tmp_path)
    events = []
    monkeypatch.setattr(
        game_access,
        "_prepare_connected_source",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            ProviderNotReachableError("Configured ROM source is unavailable: NAS offline")
        ),
    )

    with pytest.raises(ProviderNotReachableError, match="NAS offline"):
        game_access.set_operating_mode(
            config, OperatingMode.CONNECTED, progress=events.append
        )

    assert events[-1].status == "error"
    assert "NAS offline" in events[-1].message


def test_routing_failure_is_not_replaced_by_generic_mode_failure(
    tmp_path, monkeypatch
):
    config = _config(tmp_path)
    events = []
    monkeypatch.setattr(game_access, "_prepare_connected_source", lambda *_args: None)
    monkeypatch.setattr(
        game_access,
        "_prepare_save_authority_transition",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            ModeTransitionError(
                "Direct Save Storage could not be prepared; routing recovery is required"
            )
        ),
    )

    with pytest.raises(ModeTransitionError, match="routing recovery is required"):
        game_access.set_operating_mode(
            config, OperatingMode.CONNECTED, progress=events.append
        )

    assert "Direct Save Storage could not be prepared" in events[-1].message
