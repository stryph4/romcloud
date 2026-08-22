from __future__ import annotations

from pathlib import Path

from romcloud.core.storage import StorageAccessResult
from romcloud.infrastructure.google_auth import (
    AuthorizationPending,
    DeviceAuthorization,
    GoogleOAuthClientConfig,
    save_pending_authorization,
)
from romcloud.lifecycle import google_drive_setup


class FakeFlow:
    authorization = DeviceAuthorization(
        device_code="private-device-code",
        user_code="ABCD-EFGH",
        verification_url="https://google.com/device",
        expires_at=2000.0,
        interval=5.0,
    )
    pending = False

    def __init__(self, *args, **kwargs) -> None:
        pass

    def begin(self, operation):
        operation.check()
        return self.authorization

    def poll_once(self, authorization, operation):
        operation.check()
        if self.pending:
            raise AuthorizationPending("pending")


class FakeProvider:
    def __init__(self, access: StorageAccessResult) -> None:
        self.access = access

    def validate_access(self, root, operation):
        operation.check()
        assert root == "romcloud-savesync"
        return self.access


def configure_fakes(monkeypatch, tmp_path: Path) -> Path:
    config_path = tmp_path / "config" / "romcloud.toml"
    monkeypatch.setattr(
        google_drive_setup,
        "_runtime_paths",
        lambda _path: (tmp_path, tmp_path / "data"),
    )
    monkeypatch.setattr(
        google_drive_setup.GoogleOAuthClientConfig,
        "load",
        lambda _home: GoogleOAuthClientConfig("client", "secret"),
    )
    monkeypatch.setattr(google_drive_setup, "GoogleOAuthDeviceFlow", FakeFlow)
    return config_path


def test_begin_auth_persists_private_code_but_returns_only_public_fields(
    tmp_path: Path, monkeypatch
) -> None:
    config_path = configure_fakes(monkeypatch, tmp_path)

    result = google_drive_setup.begin_google_drive_auth(config_path)

    assert result["user_code"] == "ABCD-EFGH"
    assert "device_code" not in result
    pending = tmp_path / "data" / "google-drive" / "pending-auth.json"
    assert "private-device-code" in pending.read_text(encoding="utf-8")


def test_complete_auth_reports_pending_without_running_readiness(
    tmp_path: Path, monkeypatch
) -> None:
    config_path = configure_fakes(monkeypatch, tmp_path)
    FakeFlow.pending = True
    pending = tmp_path / "data" / "google-drive" / "pending-auth.json"
    save_pending_authorization(pending, FakeFlow.authorization)
    monkeypatch.setattr(
        google_drive_setup,
        "build_google_drive_provider",
        lambda *_args: (_ for _ in ()).throw(AssertionError("readiness must wait")),
    )
    try:
        result = google_drive_setup.complete_google_drive_auth(config_path)
    finally:
        FakeFlow.pending = False

    assert result["pending"] is True
    assert result["authenticated"] is False
    assert pending.is_file()


def test_complete_auth_runs_readiness_and_removes_pending_state(
    tmp_path: Path, monkeypatch
) -> None:
    config_path = configure_fakes(monkeypatch, tmp_path)
    pending = tmp_path / "data" / "google-drive" / "pending-auth.json"
    save_pending_authorization(pending, FakeFlow.authorization)
    access = StorageAccessResult(True, True, True, True)
    monkeypatch.setattr(
        google_drive_setup,
        "build_google_drive_provider",
        lambda *_args: FakeProvider(access),
    )

    result = google_drive_setup.complete_google_drive_auth(config_path)

    assert result["authenticated"] is True
    assert result["ready"] is True
    assert result["validation"] == access.as_dict()
    assert not pending.exists()
