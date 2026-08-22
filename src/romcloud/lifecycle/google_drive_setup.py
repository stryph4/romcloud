"""Google Drive authentication endpoints shared by CLI and graphical setup."""

from __future__ import annotations

import json
import time
from pathlib import Path

from romcloud.core.exceptions import ConfigurationError
from romcloud.core.remote_data import RemoteOperationContext
from romcloud.infrastructure.config import load_config
from romcloud.infrastructure.google_auth import (
    AuthorizationPending,
    GOOGLE_OAUTH_STATUS_RELATIVE_PATH,
    GoogleOAuthClientConfig,
    GoogleOAuthDeviceFlow,
    GoogleTokenStore,
    load_pending_authorization,
    save_pending_authorization,
)
from romcloud.infrastructure.providers.google_drive import (
    build_google_drive_provider,
)

AUTH_OPERATION_TIMEOUT = 15.0


def google_drive_build_status(config_path: Path) -> dict[str, object]:
    home = Path(config_path).parent.parent
    deployment_status: dict[str, object] = {}
    try:
        raw_status = json.loads(
            (home / GOOGLE_OAUTH_STATUS_RELATIVE_PATH).read_text(encoding="utf-8")
        )
        if isinstance(raw_status, dict) and raw_status.get("version") == 1:
            deployment_status = raw_status
    except (OSError, ValueError, TypeError):
        pass
    try:
        GoogleOAuthClientConfig.load(home)
    except ConfigurationError as exc:
        recorded_reason = deployment_status.get("unavailable_reason")
        reason = (
            str(recorded_reason)
            if deployment_status.get("available") is False and recorded_reason
            else str(exc)
        )
        return {
            "google_drive_available": False,
            "google_drive_unavailable_reason": reason,
            "google_drive_warning": str(deployment_status.get("warning", "")),
        }
    return {
        "google_drive_available": True,
        "google_drive_unavailable_reason": "",
        "google_drive_warning": (
            str(deployment_status.get("warning", ""))
            if deployment_status.get("available") is True
            else ""
        ),
    }


def begin_google_drive_auth(config_path: Path) -> dict[str, object]:
    home, data_path = _runtime_paths(Path(config_path))
    state_root = data_path / "google-drive"
    flow = GoogleOAuthDeviceFlow(
        GoogleOAuthClientConfig.load(home),
        GoogleTokenStore(state_root / "token.json"),
    )
    operation = RemoteOperationContext(deadline=time.monotonic() + AUTH_OPERATION_TIMEOUT)
    authorization = flow.begin(operation)
    save_pending_authorization(state_root / "pending-auth.json", authorization)
    return {"authenticated": False, **authorization.public_dict()}


def complete_google_drive_auth(config_path: Path) -> dict[str, object]:
    home, data_path = _runtime_paths(Path(config_path))
    state_root = data_path / "google-drive"
    pending_path = state_root / "pending-auth.json"
    flow = GoogleOAuthDeviceFlow(
        GoogleOAuthClientConfig.load(home),
        GoogleTokenStore(state_root / "token.json"),
    )
    authorization = load_pending_authorization(pending_path)
    operation = RemoteOperationContext(deadline=time.monotonic() + AUTH_OPERATION_TIMEOUT)
    try:
        flow.poll_once(authorization, operation)
    except AuthorizationPending:
        return {"authenticated": False, "pending": True, **authorization.public_dict()}
    pending_path.unlink(missing_ok=True)

    provider = build_google_drive_provider(home, data_path)
    access = provider.validate_access("romcloud-savesync", operation)
    return {
        "authenticated": True,
        "ready": access.ok,
        "validation": access.as_dict(),
        "detail": access.detail,
    }


def google_drive_auth_status(config_path: Path) -> dict[str, object]:
    home, data_path = _runtime_paths(Path(config_path))
    token_store = GoogleTokenStore(data_path / "google-drive" / "token.json")
    token = token_store.load()
    if token is None:
        return {"authenticated": False, "ready": False}
    provider = build_google_drive_provider(home, data_path)
    access = provider.validate_access(
        "romcloud-savesync",
        RemoteOperationContext(deadline=time.monotonic() + AUTH_OPERATION_TIMEOUT),
    )
    return {
        "authenticated": access.reachable,
        "ready": access.ok,
        "validation": access.as_dict(),
        "detail": access.detail,
    }


def _runtime_paths(config_path: Path) -> tuple[Path, Path]:
    home = config_path.parent.parent
    if config_path.is_file():
        try:
            configured = load_config(str(config_path))
        except Exception:  # setup must remain available to repair bad config
            pass
        else:
            return home, Path(configured.data_path)
    return home, home / "data"
