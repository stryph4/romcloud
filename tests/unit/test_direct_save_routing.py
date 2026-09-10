from __future__ import annotations

import json
from pathlib import Path

import pytest

from romcloud.core.exceptions import ModeTransitionError
from romcloud.core.save_selection import DEFAULT_SAVE_SELECTION_POLICY
from romcloud.infrastructure.config import AppConfig, CacheConfig, SavesConfig, SourceConfig
from romcloud.infrastructure.providers.local import LocalFilesystemProvider
from romcloud.infrastructure import diagnostics
from romcloud.infrastructure.diagnostics import DiagnosticQuery
from romcloud.integrations.batocera.direct_saves import MANIFEST_FILENAME, LegacyDirectSaveMigration
from romcloud.services.saves import SaveSyncService


class _FakeMounts:
    def __init__(self, *, fail_unbind: bool = False) -> None:
        self.bindings: dict[Path, Path] = {}
        self.fail_unbind = fail_unbind

    def unbind(self, target: Path) -> None:
        if self.fail_unbind:
            raise OSError("unbind failed")
        del self.bindings[target]

    def is_mount(self, target: Path) -> bool:
        return target in self.bindings

    def is_owned(self, source: Path, target: Path) -> bool:
        return self.bindings.get(target) == source


def _config(tmp_path: Path) -> AppConfig:
    saves = tmp_path / "userdata/saves"
    data = tmp_path / "userdata/romcloud/data"
    saves.mkdir(parents=True)
    data.mkdir(parents=True)
    return AppConfig(
        source=SourceConfig("local", str(tmp_path / "roms")),
        cache=CacheConfig(str(tmp_path / "cache")),
        local_roms_path=str(tmp_path / "local-roms"),
        data_path=str(data),
        saves=SavesConfig(local_path=str(saves)),
    )


def _service(config: AppConfig, remote: Path) -> SaveSyncService:
    remote.mkdir(parents=True, exist_ok=True)
    return SaveSyncService(
        provider=LocalFilesystemProvider(),
        connectivity_root=str(remote),
        local_root=config.saves.local_path,
        remote_root=str(remote),
        state_path=Path(config.data_path) / "savesync-state.json",
    )


def _paths(config: AppConfig, remote: Path) -> tuple[Path, Path, Path]:
    relative = Path("ppsspp/PSP/SAVEDATA")
    return (
        Path(config.saves.local_path) / relative,
        remote / relative,
        Path(config.data_path) / "direct-save-local" / relative,
    )


def _manifest(config: AppConfig, remote: Path, *, state: str = "active") -> Path:
    local, remote_path, shadow = _paths(config, remote)
    path = Path(config.data_path) / MANIFEST_FILENAME
    path.write_text(json.dumps({
        "version": 2,
        "state": state,
        "routes": [{
            "layout_id": "ppsspp-savedata",
            "canonical_root": "ppsspp/PSP/SAVEDATA",
            "local_path": str(local),
            "remote_path": str(remote_path),
            "shadow_path": str(shadow),
        }],
    }), encoding="utf-8")
    return path


def _write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)


def _migration(config: AppConfig, remote: Path, mounts=None) -> LegacyDirectSaveMigration:
    return LegacyDirectSaveMigration(
        config, DEFAULT_SAVE_SELECTION_POLICY, remote,
        mount_operations=mounts or _FakeMounts(),
    )


def test_fresh_install_has_no_direct_save_runtime_or_manifest(tmp_path: Path) -> None:
    config = _config(tmp_path)
    migration = _migration(config, tmp_path / "remote")
    assert migration.migrate(_service(config, tmp_path / "remote")).status == "not-needed"
    assert not (Path(config.data_path) / MANIFEST_FILENAME).exists()
    assert not hasattr(DEFAULT_SAVE_SELECTION_POLICY, "direct_save_layout_ids")


def test_active_legacy_mount_reconciles_shadow_and_restores_local(tmp_path: Path) -> None:
    config = _config(tmp_path)
    remote = tmp_path / "remote"
    service = _service(config, remote)
    local, remote_path, shadow = _paths(config, remote)
    _write(shadow / "GAME/save.bin", b"save")
    _write(remote_path / "GAME/save.bin", b"save")
    local.mkdir(parents=True)
    manifest = _manifest(config, remote)
    mounts = _FakeMounts()
    mounts.bindings[local] = remote_path

    report = _migration(config, remote, mounts).migrate(service)

    assert report.status == "completed"
    assert (local / "GAME/save.bin").read_bytes() == b"save"
    assert local not in mounts.bindings
    assert not manifest.exists()
    assert not shadow.exists()


def test_missing_mount_with_valid_shadow_is_recovered(tmp_path: Path) -> None:
    config = _config(tmp_path)
    remote = tmp_path / "remote"
    service = _service(config, remote)
    local, remote_path, shadow = _paths(config, remote)
    _write(shadow / "GAME/save.bin", b"shadow")
    _write(remote_path / "GAME/save.bin", b"shadow")
    local.mkdir(parents=True)
    _manifest(config, remote, state="recovery-required")

    report = _migration(config, remote).migrate(service)

    assert report.status == "completed"
    assert (local / "GAME/save.bin").read_bytes() == b"shadow"


def test_remote_only_partial_route_materializes_without_mutating_remote(tmp_path: Path) -> None:
    config = _config(tmp_path)
    remote = tmp_path / "remote"
    service = _service(config, remote)
    local, remote_path, _shadow = _paths(config, remote)
    _write(remote_path / "GAME/save.bin", b"remote-only")
    local.mkdir(parents=True)
    _manifest(config, remote, state="preparing")
    mounts = _FakeMounts()
    mounts.bindings[local] = remote_path

    report = _migration(config, remote, mounts).migrate(service)

    assert report.status == "completed"
    assert (local / "GAME/save.bin").read_bytes() == b"remote-only"
    assert (remote_path / "GAME/save.bin").read_bytes() == b"remote-only"


def test_three_way_local_only_change_is_published_then_localized(tmp_path: Path) -> None:
    config = _config(tmp_path)
    remote = tmp_path / "remote"
    service = _service(config, remote)
    local, remote_path, shadow = _paths(config, remote)
    _write(local / "GAME/save.bin", b"base")
    _write(remote_path / "GAME/save.bin", b"base")
    service.full_sync()
    shadow.parent.mkdir(parents=True, exist_ok=True)
    local.rename(shadow)
    local.mkdir(parents=True)
    (shadow / "GAME/save.bin").write_bytes(b"local-new")
    _manifest(config, remote)
    mounts = _FakeMounts()
    mounts.bindings[local] = remote_path

    report = _migration(config, remote, mounts).migrate(service)

    assert report.status == "completed"
    assert (local / "GAME/save.bin").read_bytes() == b"local-new"
    assert (remote_path / "GAME/save.bin").read_bytes() == b"local-new"


def test_three_way_remote_only_change_is_received_then_localized(tmp_path: Path) -> None:
    config = _config(tmp_path)
    remote = tmp_path / "remote"
    service = _service(config, remote)
    local, remote_path, shadow = _paths(config, remote)
    _write(local / "GAME/save.bin", b"base")
    _write(remote_path / "GAME/save.bin", b"base")
    service.full_sync()
    shadow.parent.mkdir(parents=True, exist_ok=True)
    local.rename(shadow)
    local.mkdir(parents=True)
    (remote_path / "GAME/save.bin").write_bytes(b"remote-new")
    _manifest(config, remote)
    mounts = _FakeMounts()
    mounts.bindings[local] = remote_path

    report = _migration(config, remote, mounts).migrate(service)

    assert report.status == "completed"
    assert (local / "GAME/save.bin").read_bytes() == b"remote-new"
    assert (remote_path / "GAME/save.bin").read_bytes() == b"remote-new"


def test_three_way_divergence_records_conflict_and_never_overwrites(tmp_path: Path) -> None:
    config = _config(tmp_path)
    remote = tmp_path / "remote"
    service = _service(config, remote)
    local, remote_path, shadow = _paths(config, remote)
    _write(local / "GAME/save.bin", b"base")
    _write(remote_path / "GAME/save.bin", b"base")
    service.full_sync()
    shadow.parent.mkdir(parents=True, exist_ok=True)
    local.rename(shadow)
    local.mkdir(parents=True)
    (shadow / "GAME/save.bin").write_bytes(b"local-new")
    (remote_path / "GAME/save.bin").write_bytes(b"remote-new")
    manifest = _manifest(config, remote)
    mounts = _FakeMounts()
    mounts.bindings[local] = remote_path

    report = _migration(config, remote, mounts).migrate(service)

    assert report.status == "conflict"
    assert report.conflict_ids
    assert (local / "GAME/save.bin").read_bytes() == b"local-new"
    assert (remote_path / "GAME/save.bin").read_bytes() == b"remote-new"
    assert manifest.exists()
    assert local not in mounts.bindings


def test_provider_unavailable_localizes_shadow_but_keeps_recovery_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path)
    remote = tmp_path / "remote"
    service = _service(config, remote)
    local, remote_path, shadow = _paths(config, remote)
    _write(shadow / "GAME/save.bin", b"local-safe")
    local.mkdir(parents=True)
    manifest = _manifest(config, remote)
    mounts = _FakeMounts()
    mounts.bindings[local] = remote_path
    monkeypatch.setattr(service, "is_remote_reachable", lambda: False)

    report = _migration(config, remote, mounts).migrate(service)

    assert report.status == "provider-unavailable"
    assert (local / "GAME/save.bin").read_bytes() == b"local-safe"
    assert manifest.exists()
    assert json.loads(manifest.read_text())["state"] == "migration-localized"


def test_provider_unavailable_without_shadow_preserves_active_route(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path)
    remote = tmp_path / "remote"
    service = _service(config, remote)
    local, remote_path, _shadow = _paths(config, remote)
    local.mkdir(parents=True)
    manifest = _manifest(config, remote)
    mounts = _FakeMounts()
    mounts.bindings[local] = remote_path
    monkeypatch.setattr(service, "is_remote_reachable", lambda: False)

    with pytest.raises(ModeTransitionError, match="no local shadow"):
        _migration(config, remote, mounts).migrate(service)

    assert manifest.exists()
    assert mounts.bindings[local] == remote_path


def test_unowned_mount_is_never_touched(tmp_path: Path) -> None:
    config = _config(tmp_path)
    remote = tmp_path / "remote"
    service = _service(config, remote)
    local, remote_path, shadow = _paths(config, remote)
    _write(shadow / "GAME/save.bin", b"safe")
    local.mkdir(parents=True)
    manifest = _manifest(config, remote)
    mounts = _FakeMounts()
    mounts.bindings[local] = tmp_path / "foreign-source"

    with pytest.raises(ModeTransitionError, match="unowned mount"):
        _migration(config, remote, mounts).migrate(service)

    assert manifest.exists()
    assert mounts.bindings[local] != remote_path
    assert (shadow / "GAME/save.bin").read_bytes() == b"safe"


@pytest.mark.parametrize("mutation", ["malformed", "escaping", "symlink"])
def test_malformed_or_unowned_manifest_fails_conservatively(
    tmp_path: Path, mutation: str
) -> None:
    config = _config(tmp_path)
    remote = tmp_path / "remote"
    service = _service(config, remote)
    manifest = _manifest(config, remote)
    if mutation == "malformed":
        manifest.write_text("{", encoding="utf-8")
    elif mutation == "escaping":
        payload = json.loads(manifest.read_text())
        payload["routes"][0]["local_path"] = str(tmp_path / "outside")
        manifest.write_text(json.dumps(payload), encoding="utf-8")
    else:
        manifest.unlink()
        try:
            manifest.symlink_to(tmp_path / "outside.json")
        except OSError:
            pytest.skip("symlinks require elevated privileges on this Windows host")

    with pytest.raises(ModeTransitionError, match="manifest"):
        _migration(config, remote).migrate(service)
    assert manifest.is_symlink() or manifest.exists()


def test_localized_interrupted_migration_resumes_and_is_idempotent(tmp_path: Path) -> None:
    config = _config(tmp_path)
    remote = tmp_path / "remote"
    service = _service(config, remote)
    local, remote_path, _shadow = _paths(config, remote)
    _write(local / "GAME/save.bin", b"safe")
    _write(remote_path / "GAME/save.bin", b"safe")
    manifest = _manifest(config, remote, state="migration-localized")
    migration = _migration(config, remote)

    assert migration.migrate(service).status == "completed"
    assert not manifest.exists()
    assert migration.migrate(service).status == "not-needed"
    assert (local / "GAME/save.bin").read_bytes() == b"safe"


def test_unbind_failure_never_retires_manifest_or_shadow(tmp_path: Path) -> None:
    config = _config(tmp_path)
    remote = tmp_path / "remote"
    service = _service(config, remote)
    local, remote_path, shadow = _paths(config, remote)
    _write(shadow / "GAME/save.bin", b"safe")
    _write(remote_path / "GAME/save.bin", b"safe")
    local.mkdir(parents=True)
    manifest = _manifest(config, remote)
    mounts = _FakeMounts(fail_unbind=True)
    mounts.bindings[local] = remote_path

    with pytest.raises(OSError, match="unbind failed"):
        _migration(config, remote, mounts).migrate(service)

    assert manifest.exists()
    assert (shadow / "GAME/save.bin").read_bytes() == b"safe"


def test_migration_diagnostics_share_one_operation_id(tmp_path: Path) -> None:
    config = _config(tmp_path)
    remote = tmp_path / "remote"
    service = _service(config, remote)
    local, remote_path, shadow = _paths(config, remote)
    _write(shadow / "GAME/save.bin", b"safe")
    _write(remote_path / "GAME/save.bin", b"safe")
    local.mkdir(parents=True)
    _manifest(config, remote)
    mounts = _FakeMounts()
    mounts.bindings[local] = remote_path
    store = diagnostics.configure_diagnostics(tmp_path / "diagnostics.db")
    assert store is not None

    _migration(config, remote, mounts).migrate(service)

    events = store.query(DiagnosticQuery(subsystem="savesync", page_size=200))
    codes = {item["event_code"] for item in events}
    assert {
        "direct_save_migration_started",
        "direct_save_manifest_found",
        "direct_save_route_inspected",
        "direct_save_mount_removed",
        "direct_save_local_materialized",
        "direct_save_manifest_retired",
        "direct_save_migration_completed",
    }.issubset(codes)
    operation_ids = {
        item["operation_id"]
        for item in events
        if item["event_code"].startswith("direct_save_")
    }
    assert len(operation_ids) == 1
    assert None not in operation_ids
