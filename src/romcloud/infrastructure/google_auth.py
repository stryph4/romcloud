"""Headless Google OAuth support for ROMCloud's Drive remote-data role.

The device authorization flow is designed for TVs and consoles: Batocera
shows a short code and the user completes consent on a phone or computer.
User tokens are durable local state, never part of ``romcloud.toml``.
"""

from __future__ import annotations

import json
import os
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Mapping, Optional, Protocol

from romcloud.core.exceptions import (
    ConfigurationError,
    ProviderAuthError,
    ProviderAuthRequiredError,
    ProviderNotReachableError,
)
from romcloud.core.remote_data import RemoteOperationContext
from romcloud.infrastructure import credential_crypto
from romcloud.infrastructure.atomic_file import atomic_write_text

GOOGLE_DRIVE_FILE_SCOPE = "https://www.googleapis.com/auth/drive.file"
DEVICE_CODE_ENDPOINT = "https://oauth2.googleapis.com/device/code"
TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
DEFAULT_HTTP_TIMEOUT = 10.0
GOOGLE_OAUTH_CLIENT_RELATIVE_PATH = Path("runtime/google-oauth-client.json")
GOOGLE_OAUTH_STATUS_RELATIVE_PATH = Path("runtime/google-oauth-status.json")
GOOGLE_OAUTH_METADATA_SCHEMA_VERSION = 1
GOOGLE_OAUTH_CLIENT_TYPE = "tv_and_limited_input_device"
GOOGLE_OAUTH_NOT_CONFIGURED = (
    "Google Drive is not configured in this ROMCloud build."
)


@dataclass(frozen=True)
class HttpResponse:
    status: int
    headers: Mapping[str, str]
    body: bytes


class HttpTransport(Protocol):
    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Optional[Mapping[str, str]] = None,
        body: Optional[bytes] = None,
        timeout: float = DEFAULT_HTTP_TIMEOUT,
    ) -> HttpResponse: ...


class UrllibHttpTransport:
    """Small standard-library HTTPS transport with injectable test boundary."""

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Optional[Mapping[str, str]] = None,
        body: Optional[bytes] = None,
        timeout: float = DEFAULT_HTTP_TIMEOUT,
    ) -> HttpResponse:
        request = urllib.request.Request(
            url,
            data=body,
            headers=dict(headers or {}),
            method=method,
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return HttpResponse(
                    int(response.status),
                    dict(response.headers.items()),
                    response.read(),
                )
        except urllib.error.HTTPError as exc:
            return HttpResponse(
                int(exc.code),
                dict(exc.headers.items()) if exc.headers is not None else {},
                exc.read(),
            )
        except (urllib.error.URLError, TimeoutError, socket.timeout, OSError) as exc:
            raise ProviderNotReachableError(
                "Google service is unreachable"
            ) from exc


@dataclass(frozen=True)
class GoogleOAuthClientConfig:
    client_id: str
    client_secret: str

    @classmethod
    def load(cls, romcloud_home: Path) -> "GoogleOAuthClientConfig":
        """Load project-owned OAuth client metadata without touching user config."""
        client_id = os.environ.get("ROMCLOUD_GOOGLE_OAUTH_CLIENT_ID", "").strip()
        client_secret = os.environ.get(
            "ROMCLOUD_GOOGLE_OAUTH_CLIENT_SECRET", ""
        ).strip()
        if client_id or client_secret:
            return cls.from_mapping(
                {"client_id": client_id, "client_secret": client_secret}
            )

        path = Path(romcloud_home) / GOOGLE_OAUTH_CLIENT_RELATIVE_PATH
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise ConfigurationError(GOOGLE_OAUTH_NOT_CONFIGURED) from exc
        except (OSError, ValueError, TypeError) as exc:
            raise ConfigurationError(
                f"{GOOGLE_OAUTH_NOT_CONFIGURED} OAuth client metadata is malformed."
            ) from exc
        try:
            config = cls.from_mapping(payload)
        except ConfigurationError as exc:
            raise ConfigurationError(
                f"{GOOGLE_OAUTH_NOT_CONFIGURED} OAuth client metadata is malformed."
            ) from exc
        try:
            path.chmod(0o600)
        except OSError:
            pass
        return config

    @classmethod
    def from_mapping(
        cls,
        payload: object,
        *,
        require_deployment_schema: bool = False,
    ) -> "GoogleOAuthClientConfig":
        if not isinstance(payload, Mapping):
            raise ConfigurationError("Google OAuth client metadata must be an object")
        legacy_keys = {"client_id", "client_secret"}
        deployment_keys = {
            "schema_version",
            "client_type",
            "client_id",
            "client_secret",
        }
        keys = set(payload)
        if require_deployment_schema and keys != deployment_keys:
            raise ConfigurationError(
                "Google OAuth deployment metadata does not match the expected schema"
            )
        if keys not in (legacy_keys, deployment_keys):
            raise ConfigurationError(
                "Google OAuth client metadata does not match the expected schema"
            )
        if keys == deployment_keys and (
            payload.get("schema_version") != GOOGLE_OAUTH_METADATA_SCHEMA_VERSION
            or payload.get("client_type") != GOOGLE_OAUTH_CLIENT_TYPE
        ):
            raise ConfigurationError(
                "Google OAuth client metadata is not for TVs and limited-input devices"
            )
        client_id = payload.get("client_id")
        client_secret = payload.get("client_secret")
        if not isinstance(client_id, str) or not isinstance(client_secret, str):
            raise ConfigurationError(
                "Google OAuth client metadata requires string client_id and client_secret"
            )
        client_id = client_id.strip()
        client_secret = client_secret.strip()
        if (
            not client_id
            or not client_secret
            or len(client_id) > 4096
            or len(client_secret) > 4096
            or any(char in client_id for char in "\r\n")
            or any(char in client_secret for char in "\r\n")
        ):
            raise ConfigurationError(
                "Google OAuth client metadata requires valid client_id and client_secret"
            )
        return cls(client_id=client_id, client_secret=client_secret)

    def serialized(self) -> str:
        return json.dumps(
            {
                "schema_version": GOOGLE_OAUTH_METADATA_SCHEMA_VERSION,
                "client_type": GOOGLE_OAUTH_CLIENT_TYPE,
                "client_id": self.client_id,
                "client_secret": self.client_secret,
            },
            sort_keys=True,
            separators=(",", ":"),
        )


@dataclass(frozen=True)
class GoogleOAuthToken:
    access_token: str = field(repr=False)
    refresh_token: str = field(repr=False)
    expires_at: float
    scope: str = GOOGLE_DRIVE_FILE_SCOPE
    token_type: str = "Bearer"

    @property
    def has_required_scope(self) -> bool:
        return GOOGLE_DRIVE_FILE_SCOPE in self.scope.split()


class GoogleTokenStore:
    """Encrypted, atomic mode-0600 storage for device-local OAuth tokens.

    Version-1 Phase 1 files contained plaintext JSON. A valid legacy file is
    migrated in place on first load only after the established ROMCloud
    credential envelope has been created and verified in memory.
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def load(self) -> Optional[GoogleOAuthToken]:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, ValueError, TypeError):
            raise ProviderAuthError("Stored Google authorization is invalid") from None
        if not isinstance(payload, dict):
            raise ProviderAuthError("Stored Google authorization is invalid")

        version = payload.get("version")
        if version == 2:
            token = self._load_encrypted(payload)
        elif version in (None, 1):
            token = self._token_from_payload(payload)
            # Safe one-way migration: save validates its encrypted envelope
            # before atomically replacing the still-usable plaintext file.
            self.save(token)
        else:
            raise ProviderAuthError("Stored Google authorization is invalid")
        try:
            self.path.chmod(0o600)
        except OSError:
            pass
        return token

    def save(self, token: GoogleOAuthToken) -> None:
        token_payload = {
            "version": 1,
            "access_token": token.access_token,
            "refresh_token": token.refresh_token,
            "expires_at": token.expires_at,
            "scope": token.scope,
            "token_type": token.token_type,
        }
        plaintext = json.dumps(
            token_payload,
            sort_keys=True,
            separators=(",", ":"),
        )
        try:
            envelope = credential_crypto.encrypt_password(plaintext)
            # Verify against the same envelope before replacing any existing
            # token file. Neither plaintext nor ciphertext is ever logged.
            if credential_crypto.decrypt_password(envelope) != plaintext:
                raise credential_crypto.CredentialDecryptionError(
                    "Google token envelope verification failed"
                )
        except (
            credential_crypto.CredentialCryptoUnavailableError,
            credential_crypto.CredentialDecryptionError,
        ) as exc:
            raise ProviderAuthError(
                "Google authorization could not be stored securely"
            ) from exc
        payload = {
            "version": 2,
            "encrypted_token": envelope.to_toml_dict(),
        }
        atomic_write_text(
            self.path,
            json.dumps(payload, sort_keys=True, separators=(",", ":")),
            mode=0o600,
        )

    @staticmethod
    def _token_from_payload(payload: object) -> GoogleOAuthToken:
        if not isinstance(payload, Mapping):
            raise ProviderAuthError("Stored Google authorization is invalid")
        keys = set(payload)
        required = {"access_token", "refresh_token", "expires_at"}
        allowed = required | {"version", "scope", "token_type"}
        if not required.issubset(keys) or not keys.issubset(allowed):
            raise ProviderAuthError("Stored Google authorization is incomplete")
        try:
            token = GoogleOAuthToken(
                access_token=str(payload["access_token"]),
                refresh_token=str(payload["refresh_token"]),
                expires_at=float(payload["expires_at"]),
                scope=str(payload.get("scope", GOOGLE_DRIVE_FILE_SCOPE)),
                token_type=str(payload.get("token_type", "Bearer")),
            )
        except (KeyError, TypeError, ValueError):
            raise ProviderAuthError("Stored Google authorization is incomplete") from None
        if not token.access_token or not token.refresh_token or not token.has_required_scope:
            raise ProviderAuthError("Stored Google authorization is unusable")
        return token

    def _load_encrypted(self, payload: Mapping[str, object]) -> GoogleOAuthToken:
        if set(payload) != {"version", "encrypted_token"}:
            raise ProviderAuthError("Stored Google authorization is invalid")
        envelope_payload = payload.get("encrypted_token")
        if not isinstance(envelope_payload, dict):
            raise ProviderAuthError("Stored Google authorization is incomplete")
        try:
            envelope = credential_crypto.CredentialEnvelope.from_toml_dict(
                envelope_payload
            )
            plaintext = credential_crypto.decrypt_password(envelope)
            token_payload = json.loads(plaintext)
        except Exception:  # noqa: BLE001 - suppress all decrypted-value context
            raise ProviderAuthError(
                "Stored Google authorization could not be unlocked"
            ) from None
        return self._token_from_payload(token_payload)

    def clear(self) -> None:
        self.path.unlink(missing_ok=True)


@dataclass(frozen=True)
class DeviceAuthorization:
    device_code: str = field(repr=False)
    user_code: str
    verification_url: str
    expires_at: float
    interval: float

    def public_dict(self) -> dict[str, object]:
        return {
            "user_code": self.user_code,
            "verification_url": self.verification_url,
            "expires_at": self.expires_at,
            "interval": self.interval,
        }


class AuthorizationPending(ProviderAuthError):
    """The user has not completed the short-lived device authorization yet."""


class GoogleOAuthDeviceFlow:
    def __init__(
        self,
        client: GoogleOAuthClientConfig,
        token_store: GoogleTokenStore,
        *,
        transport: Optional[HttpTransport] = None,
        clock: Callable[[], float] = time.time,
        sleeper: Callable[[float], None] = time.sleep,
        max_attempts: int = 3,
    ) -> None:
        self.client = client
        self.token_store = token_store
        self.transport = transport or UrllibHttpTransport()
        self.clock = clock
        self.sleeper = sleeper
        self.max_attempts = max(1, max_attempts)

    def begin(
        self, operation: Optional[RemoteOperationContext] = None
    ) -> DeviceAuthorization:
        context = operation or RemoteOperationContext()
        context.check()
        response = self._form_request(
            DEVICE_CODE_ENDPOINT,
            {"client_id": self.client.client_id, "scope": GOOGLE_DRIVE_FILE_SCOPE},
            context,
        )
        payload = _json_object(response.body)
        if response.status != 200:
            raise _oauth_error(payload, "Google authorization could not start")
        try:
            return DeviceAuthorization(
                device_code=str(payload["device_code"]),
                user_code=str(payload["user_code"]),
                verification_url=str(
                    payload.get("verification_url")
                    or payload["verification_uri"]
                ),
                expires_at=self.clock() + float(payload["expires_in"]),
                interval=max(1.0, float(payload.get("interval", 5))),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ProviderAuthError(
                "Google authorization returned an incomplete device code"
            ) from exc

    def poll_once(
        self,
        authorization: DeviceAuthorization,
        operation: Optional[RemoteOperationContext] = None,
    ) -> GoogleOAuthToken:
        context = operation or RemoteOperationContext()
        context.check()
        if self.clock() >= authorization.expires_at:
            raise ProviderAuthError("Google authorization code expired; start again")
        response = self._form_request(
            TOKEN_ENDPOINT,
            {
                "client_id": self.client.client_id,
                "client_secret": self.client.client_secret,
                "device_code": authorization.device_code,
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
            },
            context,
        )
        payload = _json_object(response.body)
        error = str(payload.get("error", ""))
        if error in {"authorization_pending", "slow_down"}:
            raise AuthorizationPending("Google authorization is still pending")
        if response.status != 200:
            raise _oauth_error(payload, "Google authorization failed")
        token = self._token_from_response(payload)
        self.token_store.save(token)
        return token

    def poll_until_authorized(
        self,
        authorization: DeviceAuthorization,
        operation: Optional[RemoteOperationContext] = None,
    ) -> GoogleOAuthToken:
        context = operation or RemoteOperationContext()
        interval = authorization.interval
        while True:
            context.check()
            try:
                return self.poll_once(authorization, context)
            except AuthorizationPending:
                remaining = authorization.expires_at - self.clock()
                if remaining <= 0:
                    raise ProviderAuthError(
                        "Google authorization code expired; start again"
                    ) from None
                _sleep_with_context(self.sleeper, min(interval, remaining), context)

    def usable_token(
        self, operation: Optional[RemoteOperationContext] = None
    ) -> GoogleOAuthToken:
        context = operation or RemoteOperationContext()
        token = self.token_store.load()
        if token is None:
            raise ProviderAuthRequiredError(
                "Google Drive authentication is required"
            )
        if token.expires_at > self.clock() + 60:
            return token
        return self.refresh(token, context)

    def refresh(
        self,
        token: GoogleOAuthToken,
        operation: Optional[RemoteOperationContext] = None,
    ) -> GoogleOAuthToken:
        context = operation or RemoteOperationContext()
        response = self._form_request(
            TOKEN_ENDPOINT,
            {
                "client_id": self.client.client_id,
                "client_secret": self.client.client_secret,
                "refresh_token": token.refresh_token,
                "grant_type": "refresh_token",
            },
            context,
        )
        payload = _json_object(response.body)
        if response.status != 200:
            if str(payload.get("error", "")) in {"invalid_grant", "unauthorized_client"}:
                self.token_store.clear()
                raise ProviderAuthRequiredError(
                    "Google authorization expired or was revoked; authenticate again"
                )
            raise _oauth_error(payload, "Google token refresh failed")
        refreshed = self._token_from_response(payload, refresh_token=token.refresh_token)
        self.token_store.save(refreshed)
        return refreshed

    def _token_from_response(
        self, payload: dict[str, object], *, refresh_token: str = ""
    ) -> GoogleOAuthToken:
        try:
            token = GoogleOAuthToken(
                access_token=str(payload["access_token"]),
                refresh_token=str(payload.get("refresh_token") or refresh_token),
                expires_at=self.clock() + float(payload["expires_in"]),
                scope=str(payload.get("scope", GOOGLE_DRIVE_FILE_SCOPE)),
                token_type=str(payload.get("token_type", "Bearer")),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ProviderAuthError("Google returned incomplete token data") from exc
        if not token.access_token or not token.refresh_token or not token.has_required_scope:
            raise ProviderAuthError("Google did not grant the required Drive scope")
        return token

    def _form_request(
        self,
        url: str,
        values: Mapping[str, str],
        operation: RemoteOperationContext,
    ) -> HttpResponse:
        encoded = urllib.parse.urlencode(values).encode("ascii")
        last_error: Optional[ProviderNotReachableError] = None
        for attempt in range(self.max_attempts):
            operation.check()
            try:
                response = self.transport.request(
                    "POST",
                    url,
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                    body=encoded,
                    timeout=_operation_timeout(operation),
                )
            except ProviderNotReachableError as exc:
                last_error = exc
                if attempt + 1 >= self.max_attempts:
                    raise
                _sleep_with_context(self.sleeper, min(4.0, 2**attempt), operation)
                continue
            operation.check()
            if response.status not in {408, 429, 500, 502, 503, 504}:
                return response
            if attempt + 1 >= self.max_attempts:
                return response
            retry_after = next(
                (
                    value
                    for key, value in response.headers.items()
                    if key.casefold() == "retry-after"
                ),
                None,
            )
            try:
                delay = min(30.0, float(retry_after)) if retry_after else min(4.0, 2**attempt)
            except ValueError:
                delay = min(4.0, 2**attempt)
            _sleep_with_context(self.sleeper, max(0.0, delay), operation)
        raise last_error or ProviderNotReachableError(
            "Google authentication service is unreachable"
        )


def save_pending_authorization(path: Path, authorization: DeviceAuthorization) -> None:
    payload = {
        "version": 1,
        "device_code": authorization.device_code,
        "user_code": authorization.user_code,
        "verification_url": authorization.verification_url,
        "expires_at": authorization.expires_at,
        "interval": authorization.interval,
    }
    atomic_write_text(
        path,
        json.dumps(payload, sort_keys=True, separators=(",", ":")),
        mode=0o600,
    )


def load_pending_authorization(path: Path) -> DeviceAuthorization:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return DeviceAuthorization(
            device_code=str(payload["device_code"]),
            user_code=str(payload["user_code"]),
            verification_url=str(payload["verification_url"]),
            expires_at=float(payload["expires_at"]),
            interval=float(payload["interval"]),
        )
    except FileNotFoundError as exc:
        raise ProviderAuthRequiredError(
            "No Google authorization is pending; start authentication again"
        ) from exc
    except (OSError, ValueError, TypeError, KeyError) as exc:
        raise ProviderAuthError("Pending Google authorization is invalid") from exc


def _operation_timeout(operation: RemoteOperationContext) -> float:
    if operation.deadline is None:
        return DEFAULT_HTTP_TIMEOUT
    remaining = operation.deadline - operation.clock()
    if remaining <= 0:
        operation.check()
    return max(0.1, min(DEFAULT_HTTP_TIMEOUT, remaining))


def _sleep_with_context(
    sleeper: Callable[[float], None],
    delay: float,
    operation: RemoteOperationContext,
) -> None:
    operation.check()
    if operation.deadline is not None:
        delay = min(delay, max(0.0, operation.deadline - operation.clock()))
    sleeper(delay)
    operation.check()


def _json_object(body: bytes) -> dict[str, object]:
    try:
        payload = json.loads(body.decode("utf-8")) if body else {}
    except (UnicodeError, ValueError) as exc:
        raise ProviderAuthError("Google returned an invalid response") from exc
    if not isinstance(payload, dict):
        raise ProviderAuthError("Google returned an unexpected response")
    return payload


def _oauth_error(payload: Mapping[str, object], fallback: str) -> ProviderAuthError:
    code = str(payload.get("error", ""))
    if code in {"access_denied", "authorization_declined"}:
        return ProviderAuthError("Google authorization was declined")
    if code in {"invalid_grant", "unauthorized_client", "invalid_client"}:
        return ProviderAuthRequiredError(
            "Google authorization expired, was revoked, or uses invalid app credentials"
        )
    return ProviderAuthError(fallback)
