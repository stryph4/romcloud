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


def test_mount_emits_connecting_then_connected_without_live_server(tmp_path, monkeypatch):
    config = _config(tmp_path)
    monkeypatch.setattr(
        connections.mount_worker,
        "credentials_for_mount",
        lambda config, target: ("secret", Path("/tmp/fake-creds")),
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

    result = connections.mount_connections(
        config, events.append, credential_writer=lambda *args: None
    )

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
