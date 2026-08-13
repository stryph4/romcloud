from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from romcloud.services import connections


def _config(tmp_path):
    return SimpleNamespace(
        source=SimpleNamespace(rom_root=str(tmp_path / "source")),
        smb=SimpleNamespace(
            server="nas.local",
            share="ROMs",
            username="player",
            port=445,
            remote_path="Libraries/Roms",
        ),
        remote_data=None,
        data_path=str(tmp_path / "data"),
        credentials_path=tmp_path / "credentials.toml",
    )


def _shutdown_config(tmp_path):
    config = _config(tmp_path)
    config.source.rom_root = "/userdata/romcloud/source"
    config.remote_data = SimpleNamespace(
        provider="smb",
        root="/userdata/romcloud/remote",
        smb=SimpleNamespace(
            server="omnivault",
            share="Emulation",
            username="player",
            port=445,
            remote_path="",
        ),
    )
    return config


def _forbid_mount_metadata(monkeypatch, *mount_points):
    forbidden = {str(Path(path)) for path in mount_points}
    for method_name in ("resolve", "stat", "exists", "is_dir", "iterdir"):
        original = getattr(Path, method_name)

        def guarded(self, *args, _original=original, _name=method_name, **kwargs):
            if str(self) in forbidden:
                raise AssertionError(
                    f"shutdown called Path.{_name}() on CIFS mount {self}"
                )
            return _original(self, *args, **kwargs)

        monkeypatch.setattr(Path, method_name, guarded)


def test_mount_emits_connecting_then_connected_without_live_server(tmp_path, monkeypatch):
    config = _config(tmp_path)
    monkeypatch.setattr(
        connections.mount_worker,
        "credentials_for_mount",
        lambda config, target: "secret",
    )
    monkeypatch.setattr(
        connections.mount,
        "mount_cifs_source",
        lambda **kwargs: SimpleNamespace(already_mounted=False),
    )
    monkeypatch.setattr(
        connections,
        "connection_status",
        lambda config: {"state": "connected"},
    )
    events = []

    result = connections.mount_connections(config, events.append)

    assert result == {"state": "connected", "changed": True}
    assert [(event.status, event.stage) for event in events] == [
        ("running", "connect"),
        ("success", "mounted"),
    ]


def test_unmount_reverses_targets_and_reports_transition(tmp_path, monkeypatch):
    config = _config(tmp_path)
    calls = []
    monkeypatch.setattr(connections.mount_worker, "stop_worker", lambda home: None)
    monkeypatch.setattr(
        connections.mount,
        "unmount_cifs_source",
        lambda path: calls.append(path) or True,
    )
    monkeypatch.setattr(
        connections,
        "connection_status",
        lambda config: {"state": "disconnected"},
    )

    result = connections.unmount_connections(config)

    assert calls == [str(tmp_path / "source")]
    assert result == {"state": "disconnected", "changed": True}


def test_shutdown_stop_uses_short_worker_grace_and_lazy_unmount(tmp_path, monkeypatch):
    config = _config(tmp_path)
    calls = []
    monkeypatch.setattr(
        connections.mount_worker,
        "stop_worker",
        lambda home, **kwargs: calls.append(("stop", kwargs)),
    )
    monkeypatch.setattr(
        connections.mount,
        "unmount_cifs_source",
        lambda path, **kwargs: calls.append(("unmount", kwargs)) or True,
    )
    monkeypatch.setattr(
        connections,
        "connection_status",
        lambda config: {"state": "disconnected"},
    )

    result = connections.unmount_connections(config, shutdown=True)

    assert result["changed"] is True
    assert calls == [
        ("stop", {"grace_period": 1.0}),
        (
            "unmount",
            {
                "lazy": True,
                "command_timeout": 5.0,
                "expected_server": None,
                "expected_share": "ROMs",
                "expected_read_only": True,
                "expected_remote_path": "Libraries/Roms",
            },
        ),
    ]


def test_shutdown_uses_lexical_targets_and_skips_post_unmount_diagnostics(
    tmp_path, monkeypatch
):
    config = _shutdown_config(tmp_path)
    _forbid_mount_metadata(
        monkeypatch,
        config.source.rom_root,
        config.remote_data.root,
    )
    calls = []
    monkeypatch.setattr(connections.mount_worker, "stop_worker", lambda *a, **k: None)
    monkeypatch.setattr(
        connections.mount,
        "unmount_cifs_source",
        lambda path, **kwargs: calls.append((path, kwargs)) or True,
    )
    monkeypatch.setattr(
        connections,
        "connection_status",
        lambda config: (_ for _ in ()).throw(
            AssertionError("shutdown must not run connection diagnostics")
        ),
    )

    result = connections.unmount_connections(config, shutdown=True)

    assert result == {
        "state": "disconnected",
        "configured": True,
        "changed": True,
    }
    assert calls == [
        (
            "/userdata/romcloud/remote",
            {
                "lazy": True,
                "command_timeout": 5.0,
                "expected_server": "omnivault",
                "expected_share": "Emulation",
                "expected_read_only": False,
                "expected_remote_path": "",
            },
        ),
        (
            "/userdata/romcloud/source",
            {
                "lazy": True,
                "command_timeout": 5.0,
                "expected_server": None,
                "expected_share": "ROMs",
                "expected_read_only": True,
                "expected_remote_path": "Libraries/Roms",
            },
        ),
    ]


def test_normal_stop_retains_resolved_path_validation(tmp_path, monkeypatch):
    config = _shutdown_config(tmp_path)
    resolved = []
    expected_paths = {
        str(Path(config.source.rom_root)),
        str(Path(config.remote_data.root)),
    }
    original_resolve = Path.resolve

    def record_resolve(self, *args, **kwargs):
        if str(self) in expected_paths:
            resolved.append(str(self))
            return self
        return original_resolve(self, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", record_resolve)
    monkeypatch.setattr(connections.mount_worker, "stop_worker", lambda *a, **k: None)
    calls = []
    monkeypatch.setattr(
        connections.mount,
        "unmount_cifs_source",
        lambda path, **kwargs: calls.append((path, kwargs)) or True,
    )
    monkeypatch.setattr(
        connections,
        "connection_status",
        lambda config: {"state": "disconnected"},
    )

    connections.unmount_connections(config)

    assert set(resolved) == expected_paths
    assert calls == [
        ("/userdata/romcloud/remote", {}),
        ("/userdata/romcloud/source", {}),
    ]
