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
from romcloud.infrastructure.google_auth import (
    DEVICE_CODE_ENDPOINT,
    TOKEN_ENDPOINT,
    AuthorizationPending,
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
    oauth.token_store.save(valid_token())

    assert oauth.usable_token().access_token == "access"
    assert transport.calls == []
    if os.name == "posix":
        assert stat.S_IMODE(oauth.token_store.path.stat().st_mode) & 0o077 == 0


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
