from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from romcloud.core.cancellation import TransferCancellationToken
from romcloud.core.exceptions import (
    ConfigurationError,
    ProviderAuthError,
    ProviderAuthRequiredError,
    ProviderNotReachableError,
    TransferCancelledError,
)
from romcloud.core.remote_data import RemoteOperationContext
from romcloud.infrastructure import credential_crypto
from romcloud.infrastructure.google_auth import (
    DEVICE_CODE_ENDPOINT,
    TOKEN_ENDPOINT,
    AuthorizationPending,
    DeviceAuthorization,
    GoogleOAuthClientConfig,
    GoogleOAuthDeviceFlow,
    GoogleOAuthToken,
    GoogleTokenStore,
    HttpResponse,
)


class FakeTransport:
    def __init__(self, *responses: HttpResponse) -> None:
        self.responses = list(responses)
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        if not self.responses:
            raise AssertionError("unexpected HTTP request")
        return self.responses.pop(0)


def response(status: int, payload: dict) -> HttpResponse:
    return HttpResponse(status, {}, json.dumps(payload).encode())


def flow(tmp_path: Path, transport: FakeTransport, *, now: float = 1000.0):
    return GoogleOAuthDeviceFlow(
        GoogleOAuthClientConfig("client-id", "client-secret"),
        GoogleTokenStore(tmp_path / "token.json"),
        transport=transport,
        clock=lambda: now,
        sleeper=lambda _seconds: None,
    )


def valid_token(*, expires_at: float = 2000.0) -> GoogleOAuthToken:
    return GoogleOAuthToken("access", "refresh", expires_at)


def test_no_token_requires_interactive_auth(tmp_path: Path) -> None:
    with pytest.raises(ProviderAuthRequiredError, match="required"):
        flow(tmp_path, FakeTransport()).usable_token()


def test_valid_token_never_calls_network(tmp_path: Path) -> None:
    transport = FakeTransport()
    oauth = flow(tmp_path, transport)
    stored = GoogleOAuthToken(
        "distinctive-access-secret-12345",
        "distinctive-refresh-secret-67890",
        2000.0,
    )
    oauth.token_store.save(stored)

    assert oauth.usable_token() == stored
    assert transport.calls == []
    raw = oauth.token_store.path.read_text(encoding="utf-8")
    assert json.loads(raw)["version"] == 2
    assert "distinctive-access-secret-12345" not in raw
    assert "distinctive-refresh-secret-67890" not in raw
    if os.name == "posix":
        assert stat.S_IMODE(oauth.token_store.path.stat().st_mode) & 0o077 == 0


def test_plaintext_phase1_token_is_migrated_one_way_on_load(tmp_path: Path) -> None:
    path = tmp_path / "token.json"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "access_token": "legacy-access-secret",
                "refresh_token": "legacy-refresh-secret",
                "expires_at": 2000.0,
                "scope": "https://www.googleapis.com/auth/drive.file",
                "token_type": "Bearer",
            }
        ),
        encoding="utf-8",
    )

    token = GoogleTokenStore(path).load()

    assert token is not None
    assert token.refresh_token == "legacy-refresh-secret"
    migrated = path.read_text(encoding="utf-8")
    assert json.loads(migrated)["version"] == 2
    assert "legacy-access-secret" not in migrated
    assert "legacy-refresh-secret" not in migrated


def test_failed_plaintext_migration_leaves_original_file_untouched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "token.json"
    original = json.dumps(
        {
            "version": 1,
            "access_token": "never-log-access",
            "refresh_token": "never-log-refresh",
            "expires_at": 2000.0,
        }
    ).encode()
    path.write_bytes(original)
    monkeypatch.setattr(
        credential_crypto,
        "encrypt_password",
        lambda _value: (_ for _ in ()).throw(
            credential_crypto.CredentialCryptoUnavailableError("unavailable")
        ),
    )

    with pytest.raises(ProviderAuthError, match="stored securely") as captured:
        GoogleTokenStore(path).load()

    assert path.read_bytes() == original
    assert "never-log" not in str(captured.value)


def test_token_and_device_code_repr_are_secret_safe() -> None:
    token = valid_token()
    authorization = DeviceAuthorization(
        device_code="device-secret",
        user_code="ABCD-EFGH",
        verification_url="https://google.com/device",
        expires_at=2000.0,
        interval=5.0,
    )

    assert "access" not in repr(token)
    assert "refresh" not in repr(token)
    assert "device-secret" not in repr(authorization)


def test_corrupt_encrypted_token_error_has_no_decrypted_context(tmp_path: Path) -> None:
    store = GoogleTokenStore(tmp_path / "token.json")
    store.save(
        GoogleOAuthToken(
            "never-log-access-token",
            "never-log-refresh-token",
            2000.0,
        )
    )
    payload = json.loads(store.path.read_text(encoding="utf-8"))
    ciphertext = payload["encrypted_token"]["ciphertext"]
    payload["encrypted_token"]["ciphertext"] = (
        ("A" if ciphertext[0] != "A" else "B") + ciphertext[1:]
    )
    store.path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ProviderAuthError, match="could not be unlocked") as captured:
        store.load()

    assert "never-log" not in str(captured.value)
    assert captured.value.__cause__ is None


def test_device_flow_uses_drive_file_scope_and_persists_tokens(tmp_path: Path) -> None:
    transport = FakeTransport(
        response(
            200,
            {
                "device_code": "device-secret",
                "user_code": "ABCD-EFGH",
                "verification_url": "https://google.com/device",
                "expires_in": 600,
                "interval": 5,
            },
        ),
        response(
            200,
            {
                "access_token": "access-secret",
                "refresh_token": "refresh-secret",
                "expires_in": 3600,
                "scope": "https://www.googleapis.com/auth/drive.file",
                "token_type": "Bearer",
            },
        ),
    )
    oauth = flow(tmp_path, transport)

    authorization = oauth.begin()
    token = oauth.poll_once(authorization)

    assert authorization.user_code == "ABCD-EFGH"
    assert token.refresh_token == "refresh-secret"
    assert transport.calls[0][1] == DEVICE_CODE_ENDPOINT
    assert b"drive.file" in transport.calls[0][2]["body"]
    assert transport.calls[1][1] == TOKEN_ENDPOINT
    assert b"client_secret=client-secret" in transport.calls[1][2]["body"]
    assert oauth.token_store.load() == token


def test_authorization_pending_is_not_treated_as_failure(tmp_path: Path) -> None:
    oauth = flow(
        tmp_path,
        FakeTransport(
            response(
                428,
                {"error": "authorization_pending", "error_description": "pending"},
            )
        ),
    )
    authorization = type("Authorization", (), {
        "device_code": "device",
        "expires_at": 2000.0,
    })()
    with pytest.raises(AuthorizationPending):
        oauth.poll_once(authorization)  # type: ignore[arg-type]


def test_refresh_succeeds_and_preserves_refresh_token(tmp_path: Path) -> None:
    oauth = flow(
        tmp_path,
        FakeTransport(
            response(
                200,
                {
                    "access_token": "new-access",
                    "expires_in": 3600,
                    "scope": "https://www.googleapis.com/auth/drive.file",
                },
            )
        ),
    )
    oauth.token_store.save(valid_token(expires_at=1001.0))

    refreshed = oauth.usable_token()

    assert refreshed.access_token == "new-access"
    assert refreshed.refresh_token == "refresh"
    assert oauth.token_store.load() == refreshed


@pytest.mark.parametrize("error", ["invalid_grant", "unauthorized_client"])
def test_revoked_refresh_requires_reauthentication_and_clears_token(
    tmp_path: Path, error: str
) -> None:
    oauth = flow(tmp_path, FakeTransport(response(400, {"error": error})))
    oauth.token_store.save(valid_token(expires_at=1001.0))

    with pytest.raises(ProviderAuthRequiredError, match="revoked"):
        oauth.usable_token()

    assert not oauth.token_store.path.exists()


def test_auth_cancellation_stops_before_network(tmp_path: Path) -> None:
    token = TransferCancellationToken()
    token.cancel()
    transport = FakeTransport()
    with pytest.raises(TransferCancelledError):
        flow(tmp_path, transport).begin(RemoteOperationContext(cancellation=token))
    assert transport.calls == []


def test_auth_deadline_stops_before_network(tmp_path: Path) -> None:
    transport = FakeTransport()
    with pytest.raises(ProviderNotReachableError, match="deadline"):
        flow(tmp_path, transport).begin(
            RemoteOperationContext(deadline=1.0, clock=lambda: 2.0)
        )
    assert transport.calls == []


def test_declined_auth_is_clear_and_secret_free(tmp_path: Path) -> None:
    oauth = flow(tmp_path, FakeTransport(response(403, {"error": "access_denied"})))
    authorization = type("Authorization", (), {
        "device_code": "do-not-leak",
        "expires_at": 2000.0,
    })()
    with pytest.raises(ProviderAuthError, match="declined") as captured:
        oauth.poll_once(authorization)  # type: ignore[arg-type]
    assert "do-not-leak" not in str(captured.value)


def test_client_metadata_is_external_to_romcloud_config(tmp_path: Path) -> None:
    path = tmp_path / "runtime" / "google-oauth-client.json"
    path.parent.mkdir()
    path.write_text(json.dumps({"client_id": "id", "client_secret": "secret"}))

    loaded = GoogleOAuthClientConfig.load(tmp_path)

    assert loaded == GoogleOAuthClientConfig("id", "secret")


def test_missing_client_metadata_has_clear_build_error(tmp_path: Path) -> None:
    with pytest.raises(
        ConfigurationError,
        match="Google Drive is not configured in this ROMCloud build",
    ):
        GoogleOAuthClientConfig.load(tmp_path)


def test_malformed_client_metadata_has_clear_build_error(tmp_path: Path) -> None:
    path = tmp_path / "runtime" / "google-oauth-client.json"
    path.parent.mkdir()
    path.write_text("not-json", encoding="utf-8")

    with pytest.raises(
        ConfigurationError,
        match="not configured in this ROMCloud build.*malformed",
    ):
        GoogleOAuthClientConfig.load(tmp_path)
