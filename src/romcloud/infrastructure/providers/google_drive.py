"""Google Drive remote-data provider using opaque IDs and app ownership marks."""

from __future__ import annotations

import json
import os
import random
import time
import urllib.parse
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping, Optional

from romcloud.core.exceptions import (
    ProviderAuthError,
    ProviderAuthRequiredError,
    ProviderConflictError,
    ProviderError,
    ProviderNotReachableError,
    ProviderObjectNotFoundError,
    ProviderPermissionError,
    ProviderQuotaError,
    ProviderRateLimitError,
)
from romcloud.core.remote_data import RemoteOperationContext
from romcloud.core.storage import ProviderCapabilities, RemoteEntry, StorageAccessResult
from romcloud.infrastructure.atomic_file import atomic_write_text
from romcloud.infrastructure.google_auth import (
    DEFAULT_HTTP_TIMEOUT,
    GoogleOAuthClientConfig,
    GoogleOAuthDeviceFlow,
    GoogleTokenStore,
    HttpResponse,
    HttpTransport,
    _operation_timeout,
    _sleep_with_context,
)

DRIVE_API = "https://www.googleapis.com/drive/v3"
DRIVE_UPLOAD_API = "https://www.googleapis.com/upload/drive/v3"
FOLDER_MIME_TYPE = "application/vnd.google-apps.folder"
ROMCLOUD_ROOT_NAME = "ROMCloud SaveSync"
ROMCLOUD_OWNER = "romcloud-savesync"
ROOT_ROLE = "root"
PROBE_ROLE = "write-probe"
ROOT_LOGICAL_ID = "romcloud-savesync"
_METADATA_FIELDS = (
    "id,name,mimeType,size,version,md5Checksum,modifiedTime,trashed,"
    "parents,appProperties,ownedByMe,isAppAuthorized"
)

GOOGLE_DRIVE_CAPABILITIES = ProviderCapabilities(
    has_filesystem_semantics=False,
    can_resume_download=False,
    can_resume_upload=False,
    supports_durable_transactions=False,
    supports_remote_data_writes=True,
    supports_conditional_revisions=False,
    supports_object_generations=False,
)


@dataclass(frozen=True)
class GoogleDriveDatasetRoot:
    """Opaque logical handle; only the provider resolves it to Drive IDs."""

    account_slot: str
    namespace: str


@dataclass(frozen=True)
class GoogleDriveObject:
    object_id: str
    name: str
    mime_type: str
    size_bytes: Optional[int]
    revision: Optional[str]
    checksum: Optional[str]
    modified_time: Optional[str]
    parents: tuple[str, ...]
    app_properties: Mapping[str, str]
    owned_by_me: bool
    app_authorized: bool
    trashed: bool = False

    @property
    def is_folder(self) -> bool:
        return self.mime_type == FOLDER_MIME_TYPE

    def as_remote_entry(self) -> RemoteEntry:
        return RemoteEntry(
            name=self.name,
            relative_path=self.name,
            is_directory=self.is_folder,
            size_bytes=self.size_bytes,
            object_id=self.object_id,
            revision=self.revision,
            checksum=self.checksum,
        )


class GoogleDriveRootState:
    """Durable local identity for the one ROMCloud-owned Drive folder."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def load(self) -> tuple[Optional[str], Optional[str]]:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None, None
        except (OSError, ValueError, TypeError) as exc:
            raise ProviderError("Google Drive root identity state is invalid") from exc
        if not isinstance(payload, dict):
            raise ProviderError("Google Drive root identity state is invalid")
        return (
            _optional_string(payload.get("folder_id")),
            _optional_string(payload.get("pending_folder_id")),
        )

    def save(
        self, *, folder_id: Optional[str] = None, pending_folder_id: Optional[str] = None
    ) -> None:
        payload = {
            "version": 1,
            "folder_id": folder_id,
            "pending_folder_id": pending_folder_id,
        }
        atomic_write_text(
            self.path,
            json.dumps(payload, sort_keys=True, separators=(",", ":")),
            mode=0o600,
        )


class GoogleDriveApiClient:
    """Bounded Drive v3 REST primitives with normalized provider errors."""

    def __init__(
        self,
        oauth: GoogleOAuthDeviceFlow,
        *,
        transport: Optional[HttpTransport] = None,
        max_attempts: int = 3,
        sleeper: Callable[[float], None] = time.sleep,
        random_source: Callable[[], float] = random.random,
    ) -> None:
        self.oauth = oauth
        self.transport = transport or oauth.transport
        self.max_attempts = max(1, max_attempts)
        self.sleeper = sleeper
        self.random_source = random_source

    def about(self, operation: Optional[RemoteOperationContext] = None) -> None:
        self._request_json("GET", f"{DRIVE_API}/about?fields=user", operation=operation)

    def generate_id(self, operation: Optional[RemoteOperationContext] = None) -> str:
        payload = self._request_json(
            "GET",
            f"{DRIVE_API}/files/generateIds?count=1&space=drive&type=files",
            operation=operation,
        )
        ids = payload.get("ids")
        if not isinstance(ids, list) or len(ids) != 1 or not str(ids[0]):
            raise ProviderError("Google Drive did not return an object ID")
        return str(ids[0])

    def get_metadata(
        self,
        object_id: str,
        operation: Optional[RemoteOperationContext] = None,
    ) -> GoogleDriveObject:
        object_id = _validate_object_id(object_id)
        payload = self._request_json(
            "GET",
            f"{DRIVE_API}/files/{urllib.parse.quote(object_id, safe='')}"
            f"?fields={urllib.parse.quote(_METADATA_FIELDS, safe=',')}",
            operation=operation,
        )
        return _object_from_payload(payload)

    def find_owned_roots(
        self, operation: Optional[RemoteOperationContext] = None
    ) -> list[GoogleDriveObject]:
        query = (
            "trashed = false and mimeType = 'application/vnd.google-apps.folder' "
            "and appProperties has { key='romcloudOwner' and value='romcloud-savesync' } "
            "and appProperties has { key='romcloudRole' and value='root' }"
        )
        params = urllib.parse.urlencode(
            {
                "q": query,
                "spaces": "drive",
                "pageSize": "10",
                "fields": f"files({_METADATA_FIELDS}),nextPageToken",
            }
        )
        payload = self._request_json(
            "GET", f"{DRIVE_API}/files?{params}", operation=operation
        )
        raw_files = payload.get("files", [])
        if not isinstance(raw_files, list):
            raise ProviderError("Google Drive returned an invalid root listing")
        return [
            _object_from_payload(item)
            for item in raw_files
            if isinstance(item, dict)
        ]

    def create_folder(
        self,
        object_id: str,
        *,
        name: str,
        app_properties: Mapping[str, str],
        operation: Optional[RemoteOperationContext] = None,
    ) -> GoogleDriveObject:
        body = json.dumps(
            {
                "id": _validate_object_id(object_id),
                "name": name,
                "mimeType": FOLDER_MIME_TYPE,
                "parents": ["root"],
                "appProperties": dict(app_properties),
            },
            separators=(",", ":"),
        ).encode("utf-8")
        payload = self._request_json(
            "POST",
            f"{DRIVE_API}/files?fields={urllib.parse.quote(_METADATA_FIELDS, safe=',')}",
            headers={"Content-Type": "application/json; charset=UTF-8"},
            body=body,
            operation=operation,
            idempotent_create_id=object_id,
        )
        return _object_from_payload(payload)

    def upload_bytes(
        self,
        object_id: str,
        *,
        parent_id: str,
        name: str,
        content: bytes,
        app_properties: Mapping[str, str],
        operation: Optional[RemoteOperationContext] = None,
    ) -> GoogleDriveObject:
        boundary = f"romcloud-{uuid.uuid4().hex}"
        metadata = json.dumps(
            {
                "id": _validate_object_id(object_id),
                "name": name,
                "parents": [_validate_object_id(parent_id)],
                "mimeType": "application/octet-stream",
                "appProperties": dict(app_properties),
            },
            separators=(",", ":"),
        ).encode("utf-8")
        body = b"".join(
            (
                f"--{boundary}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n".encode(),
                metadata,
                f"\r\n--{boundary}\r\nContent-Type: application/octet-stream\r\n\r\n".encode(),
                content,
                f"\r\n--{boundary}--\r\n".encode(),
            )
        )
        payload = self._request_json(
            "POST",
            f"{DRIVE_UPLOAD_API}/files?uploadType=multipart&fields="
            f"{urllib.parse.quote(_METADATA_FIELDS, safe=',')}",
            headers={"Content-Type": f"multipart/related; boundary={boundary}"},
            body=body,
            operation=operation,
            idempotent_create_id=object_id,
        )
        return _object_from_payload(payload)

    def download_bytes(
        self,
        object_id: str,
        operation: Optional[RemoteOperationContext] = None,
    ) -> bytes:
        response = self._request(
            "GET",
            f"{DRIVE_API}/files/{urllib.parse.quote(_validate_object_id(object_id), safe='')}?alt=media",
            operation=operation,
        )
        return response.body

    def delete(
        self,
        object_id: str,
        operation: Optional[RemoteOperationContext] = None,
    ) -> None:
        self._request(
            "DELETE",
            f"{DRIVE_API}/files/{urllib.parse.quote(_validate_object_id(object_id), safe='')}",
            operation=operation,
            expected_statuses={204},
        )

    def _request_json(self, method: str, url: str, **kwargs) -> dict[str, object]:
        response = self._request(method, url, **kwargs)
        try:
            payload = json.loads(response.body.decode("utf-8")) if response.body else {}
        except (UnicodeError, ValueError) as exc:
            raise ProviderError("Google Drive returned an invalid response") from exc
        if not isinstance(payload, dict):
            raise ProviderError("Google Drive returned an unexpected response")
        return payload

    def _request(
        self,
        method: str,
        url: str,
        *,
        headers: Optional[Mapping[str, str]] = None,
        body: Optional[bytes] = None,
        operation: Optional[RemoteOperationContext] = None,
        expected_statuses: Optional[set[int]] = None,
        idempotent_create_id: Optional[str] = None,
    ) -> HttpResponse:
        context = operation or RemoteOperationContext()
        accepted_statuses = expected_statuses or {200}
        token = self.oauth.usable_token(context)
        refreshed_after_401 = False
        last_error: Optional[ProviderError] = None
        for attempt in range(self.max_attempts):
            context.check()
            request_headers = {
                "Authorization": f"Bearer {token.access_token}",
                "Accept": "application/json",
                **dict(headers or {}),
            }
            try:
                response = self.transport.request(
                    method,
                    url,
                    headers=request_headers,
                    body=body,
                    timeout=_operation_timeout(context),
                )
            except ProviderNotReachableError as exc:
                last_error = exc
                if attempt + 1 >= self.max_attempts:
                    raise
                _sleep_with_context(
                    self.sleeper,
                    min(30.0, (2**attempt) + self.random_source()),
                    context,
                )
                continue
            context.check()
            if response.status in accepted_statuses:
                return response
            payload = _safe_error_payload(response.body)
            if response.status == 401 and not refreshed_after_401:
                token = self.oauth.refresh(token, context)
                refreshed_after_401 = True
                continue
            error = _translate_drive_error(response.status, payload)
            retryable = _is_retryable(response.status, payload)
            if retryable and attempt + 1 < self.max_attempts:
                delay = _retry_delay(response.headers, attempt, self.random_source())
                _sleep_with_context(self.sleeper, delay, context)
                last_error = error
                continue
            if (
                response.status == 409
                and idempotent_create_id is not None
            ):
                return HttpResponse(
                    200,
                    {},
                    json.dumps(
                        _object_payload(self.get_metadata(idempotent_create_id, context))
                    ).encode("utf-8"),
                )
            raise error
        raise last_error or ProviderNotReachableError("Google Drive request failed")


class GoogleDriveProvider:
    """Remote-data provider that owns Drive objects, never paths."""

    provider_id = "google_drive"
    capabilities = GOOGLE_DRIVE_CAPABILITIES

    def __init__(
        self,
        api: GoogleDriveApiClient,
        root_state: GoogleDriveRootState,
    ) -> None:
        self.api = api
        self.root_state = root_state

    def is_reachable(self, root: object) -> bool:
        if str(root or ROOT_LOGICAL_ID) != ROOT_LOGICAL_ID:
            return False
        try:
            self.api.about()
        except ProviderError:
            return False
        return True

    def validate_access(
        self,
        root: object,
        operation: Optional[RemoteOperationContext] = None,
    ) -> StorageAccessResult:
        context = operation or RemoteOperationContext()
        if str(root or ROOT_LOGICAL_ID) != ROOT_LOGICAL_ID:
            return StorageAccessResult(
                False,
                False,
                detail="Unknown Google Drive account slot",
            )
        try:
            self.api.about(context)
        except ProviderAuthRequiredError as exc:
            return StorageAccessResult(False, False, detail=str(exc))
        except ProviderAuthError as exc:
            return StorageAccessResult(False, False, detail=f"authentication failed: {exc}")
        except ProviderError as exc:
            return StorageAccessResult(False, False, detail=str(exc))

        try:
            root_object = self.ensure_app_root(context)
        except (ProviderPermissionError, ProviderQuotaError) as exc:
            return StorageAccessResult(
                True, True, write_verified=False, cleanup_verified=None, detail=str(exc)
            )
        except ProviderError as exc:
            return StorageAccessResult(True, False, detail=str(exc))

        probe_id: Optional[str] = None
        write_verified = False
        cleanup_verified: Optional[bool] = None
        detail = ""
        probe_content = b"romcloud-google-drive-write-probe-v1"
        try:
            probe_id = self.api.generate_id(context)
            created = self.api.upload_bytes(
                probe_id,
                parent_id=root_object.object_id,
                name=".romcloud-write-probe",
                content=probe_content,
                app_properties=_owned_properties(PROBE_ROLE),
                operation=context,
            )
            self._require_owned(created, role=PROBE_ROLE, parent_id=root_object.object_id)
            downloaded = self.api.download_bytes(probe_id, context)
            if downloaded != probe_content:
                raise ProviderError("Google Drive write probe read-back did not match")
            write_verified = True
        except ProviderError as exc:
            detail = str(exc)
        finally:
            if probe_id is not None:
                cleanup_context = RemoteOperationContext(
                    deadline=time.monotonic() + DEFAULT_HTTP_TIMEOUT
                )
                try:
                    self.delete_owned_object(
                        probe_id,
                        role=PROBE_ROLE,
                        parent_id=root_object.object_id,
                        operation=cleanup_context,
                    )
                    cleanup_verified = True
                except ProviderObjectNotFoundError:
                    cleanup_verified = True
                except ProviderError as exc:
                    cleanup_verified = False
                    cleanup = f"write-probe cleanup failed: {exc}"
                    detail = f"{detail}; {cleanup}" if detail else cleanup
        return StorageAccessResult(
            True,
            True,
            write_verified=write_verified,
            cleanup_verified=cleanup_verified,
            detail=detail,
        )

    def remote_data_root(self, root: object, namespace: str) -> GoogleDriveDatasetRoot:
        logical = str(root or ROOT_LOGICAL_ID)
        if logical != ROOT_LOGICAL_ID:
            raise ProviderError("Unknown Google Drive account slot")
        if not namespace or "/" in namespace or "\\" in namespace:
            raise ProviderError("Invalid Google Drive dataset namespace")
        return GoogleDriveDatasetRoot(logical, namespace)

    def ensure_app_root(
        self, operation: Optional[RemoteOperationContext] = None
    ) -> GoogleDriveObject:
        context = operation or RemoteOperationContext()
        folder_id, pending_id = self.root_state.load()
        if folder_id is not None:
            try:
                folder = self.api.get_metadata(folder_id, context)
            except ProviderObjectNotFoundError:
                self.root_state.save()
            else:
                self._require_owned(folder, role=ROOT_ROLE, top_level=True)
                return folder

        if pending_id is not None:
            try:
                pending = self.api.get_metadata(pending_id, context)
            except ProviderObjectNotFoundError:
                pass
            else:
                self._require_owned(pending, role=ROOT_ROLE, top_level=True)
                self.root_state.save(folder_id=pending.object_id)
                return pending

        matches = [
            item
            for item in self.api.find_owned_roots(context)
            if self._is_owned(item, role=ROOT_ROLE, top_level=True)
        ]
        if len(matches) > 1:
            raise ProviderConflictError(
                "Multiple ROMCloud-owned Drive roots exist; refusing to choose one"
            )
        if len(matches) == 1:
            self.root_state.save(folder_id=matches[0].object_id)
            return matches[0]

        object_id = pending_id or self.api.generate_id(context)
        self.root_state.save(pending_folder_id=object_id)
        try:
            folder = self.api.create_folder(
                object_id,
                name=ROMCLOUD_ROOT_NAME,
                app_properties=_owned_properties(ROOT_ROLE),
                operation=context,
            )
            self._require_owned(folder, role=ROOT_ROLE, top_level=True)
        except BaseException:
            # Keep the generated ID so retry can recover a create whose reply
            # was lost without creating a duplicate folder.
            raise
        self.root_state.save(folder_id=folder.object_id)
        return folder

    def metadata_by_id(
        self,
        object_id: str,
        *,
        role: Optional[str] = None,
        parent_id: Optional[str] = None,
        operation: Optional[RemoteOperationContext] = None,
    ) -> GoogleDriveObject:
        item = self.api.get_metadata(object_id, operation)
        self._require_owned(item, role=role, parent_id=parent_id)
        return item

    def upload_owned_bytes(
        self,
        *,
        parent_id: str,
        name: str,
        content: bytes,
        role: str,
        operation: Optional[RemoteOperationContext] = None,
    ) -> GoogleDriveObject:
        object_id = self.api.generate_id(operation)
        try:
            item = self.api.upload_bytes(
                object_id,
                parent_id=parent_id,
                name=name,
                content=content,
                app_properties=_owned_properties(role),
                operation=operation,
            )
        except BaseException:
            # The ID is known even if a response was lost. Best-effort cleanup
            # is ownership-checked and can never target an arbitrary Drive file.
            try:
                self.delete_owned_object(
                    object_id,
                    role=role,
                    parent_id=parent_id,
                    operation=operation,
                )
            except ProviderError:
                pass
            raise
        self._require_owned(item, role=role, parent_id=parent_id)
        return item

    def download_owned_object(
        self,
        object_id: str,
        destination: Path,
        *,
        role: Optional[str] = None,
        parent_id: Optional[str] = None,
        operation: Optional[RemoteOperationContext] = None,
    ) -> GoogleDriveObject:
        item = self.metadata_by_id(
            object_id,
            role=role,
            parent_id=parent_id,
            operation=operation,
        )
        content = self.api.download_bytes(object_id, operation)
        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(f".{destination.name}.google-{uuid.uuid4().hex}")
        try:
            descriptor = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
            )
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            temporary.replace(destination)
        finally:
            temporary.unlink(missing_ok=True)
        return item

    def delete_owned_object(
        self,
        object_id: str,
        *,
        role: Optional[str] = None,
        parent_id: Optional[str] = None,
        operation: Optional[RemoteOperationContext] = None,
    ) -> None:
        item = self.metadata_by_id(
            object_id,
            role=role,
            parent_id=parent_id,
            operation=operation,
        )
        if item.is_folder:
            raise ProviderPermissionError("Refusing to delete a Drive folder as an object")
        self.api.delete(object_id, operation)

    def build_remote_save_store(
        self, connectivity_root: object, dataset_root: object
    ):
        from romcloud.infrastructure.google_drive_saves import GoogleDriveRemoteSaveStore

        return GoogleDriveRemoteSaveStore(self, connectivity_root, dataset_root)

    @staticmethod
    def _is_owned(
        item: GoogleDriveObject,
        *,
        role: Optional[str] = None,
        parent_id: Optional[str] = None,
        top_level: bool = False,
    ) -> bool:
        properties = item.app_properties
        return (
            not item.trashed
            and item.owned_by_me
            and item.app_authorized
            and properties.get("romcloudOwner") == ROMCLOUD_OWNER
            and properties.get("romcloudSchema") == "1"
            and (role is None or properties.get("romcloudRole") == role)
            and (parent_id is None or parent_id in item.parents)
            and (not top_level or item.is_folder)
        )

    def _require_owned(self, item: GoogleDriveObject, **kwargs) -> None:
        if not self._is_owned(item, **kwargs):
            raise ProviderPermissionError(
                "Refusing a Google Drive object not verified as ROMCloud-owned"
            )


def build_google_drive_provider(
    romcloud_home: Path,
    data_path: Path,
    *,
    transport: Optional[HttpTransport] = None,
) -> GoogleDriveProvider:
    """Construct the production provider from deployment metadata/local state."""
    client = GoogleOAuthClientConfig.load(Path(romcloud_home))
    state_root = Path(data_path) / "google-drive"
    oauth = GoogleOAuthDeviceFlow(
        client,
        GoogleTokenStore(state_root / "token.json"),
        transport=transport,
    )
    return GoogleDriveProvider(
        GoogleDriveApiClient(oauth, transport=transport),
        GoogleDriveRootState(state_root / "root.json"),
    )


def _owned_properties(role: str) -> dict[str, str]:
    return {
        "romcloudOwner": ROMCLOUD_OWNER,
        "romcloudRole": role,
        "romcloudSchema": "1",
    }


def _object_from_payload(payload: Mapping[str, object]) -> GoogleDriveObject:
    try:
        object_id = _validate_object_id(str(payload["id"]))
        properties_raw = payload.get("appProperties", {})
        properties = (
            {str(key): str(value) for key, value in properties_raw.items()}
            if isinstance(properties_raw, dict)
            else {}
        )
        parents_raw = payload.get("parents", [])
        parents = (
            tuple(str(parent) for parent in parents_raw)
            if isinstance(parents_raw, list)
            else ()
        )
        size_raw = payload.get("size")
        return GoogleDriveObject(
            object_id=object_id,
            name=str(payload.get("name", "")),
            mime_type=str(payload.get("mimeType", "")),
            size_bytes=int(size_raw) if size_raw is not None else None,
            revision=_optional_string(payload.get("version")),
            checksum=_optional_string(payload.get("md5Checksum")),
            modified_time=_optional_string(payload.get("modifiedTime")),
            parents=parents,
            app_properties=properties,
            owned_by_me=bool(payload.get("ownedByMe", False)),
            app_authorized=bool(payload.get("isAppAuthorized", False)),
            trashed=bool(payload.get("trashed", False)),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ProviderError("Google Drive returned incomplete object metadata") from exc


def _object_payload(item: GoogleDriveObject) -> dict[str, object]:
    return {
        "id": item.object_id,
        "name": item.name,
        "mimeType": item.mime_type,
        "size": str(item.size_bytes) if item.size_bytes is not None else None,
        "version": item.revision,
        "md5Checksum": item.checksum,
        "modifiedTime": item.modified_time,
        "parents": list(item.parents),
        "appProperties": dict(item.app_properties),
        "ownedByMe": item.owned_by_me,
        "isAppAuthorized": item.app_authorized,
        "trashed": item.trashed,
    }


def _translate_drive_error(status: int, payload: Mapping[str, object]) -> ProviderError:
    reason = _drive_reason(payload)
    if status == 401 or reason in {"authError", "invalidCredentials"}:
        return ProviderAuthRequiredError(
            "Google authorization expired or was revoked; authenticate again"
        )
    if status == 404 or reason == "notFound":
        return ProviderObjectNotFoundError("Google Drive object was not found")
    if reason in {"storageQuotaExceeded", "dailyLimitExceeded", "quotaExceeded"}:
        return ProviderQuotaError("Google Drive quota is exceeded")
    if status == 429 or reason in {"rateLimitExceeded", "userRateLimitExceeded"}:
        return ProviderRateLimitError("Google Drive rate limit was exceeded")
    if status == 403:
        return ProviderPermissionError("Google Drive permission was denied")
    if status == 409:
        return ProviderConflictError("Google Drive object already exists or changed")
    if status in {408, 500, 502, 503, 504}:
        return ProviderNotReachableError("Google Drive is temporarily unavailable")
    return ProviderError(f"Google Drive request failed (HTTP {status})")


def _safe_error_payload(body: bytes) -> dict[str, object]:
    try:
        payload = json.loads(body.decode("utf-8")) if body else {}
    except (UnicodeError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _drive_reason(payload: Mapping[str, object]) -> str:
    error = payload.get("error")
    if not isinstance(error, dict):
        return ""
    errors = error.get("errors")
    if isinstance(errors, list) and errors and isinstance(errors[0], dict):
        return str(errors[0].get("reason", ""))
    return str(error.get("status", ""))


def _is_retryable(status: int, payload: Mapping[str, object]) -> bool:
    return status in {408, 429, 500, 502, 503, 504} or _drive_reason(payload) in {
        "rateLimitExceeded",
        "userRateLimitExceeded",
        "backendError",
    }


def _retry_delay(headers: Mapping[str, str], attempt: int, jitter: float) -> float:
    retry_after = next(
        (value for key, value in headers.items() if key.casefold() == "retry-after"),
        None,
    )
    if retry_after is not None:
        try:
            return max(0.0, min(30.0, float(retry_after)))
        except ValueError:
            pass
    return min(30.0, (2**attempt) + max(0.0, min(1.0, jitter)))


def _validate_object_id(value: str) -> str:
    value = str(value).strip()
    if not value or len(value) > 256 or any(char.isspace() for char in value):
        raise ProviderError("Invalid Google Drive object identity")
    if any(char in value for char in "/\\?#"):
        raise ProviderError("Invalid Google Drive object identity")
    return value


def _optional_string(value: object) -> Optional[str]:
    if value is None:
        return None
    rendered = str(value)
    return rendered if rendered else None
