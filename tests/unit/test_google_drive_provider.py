from __future__ import annotations

import json
from pathlib import Path

import pytest

from romcloud.core.exceptions import (
    ProviderAuthRequiredError,
    ProviderConflictError,
    ProviderNotReachableError,
    ProviderObjectNotFoundError,
    ProviderPermissionError,
    ProviderQuotaError,
    SaveSyncWriteUnavailableError,
    TransferCancelledError,
)
from romcloud.core.cancellation import TransferCancellationToken
from romcloud.core.remote_data import RemoteOperationContext
from romcloud.infrastructure.google_auth import GoogleOAuthToken, HttpResponse
from romcloud.infrastructure.google_drive_saves import GoogleDriveRemoteSaveStore
from romcloud.infrastructure.providers.google_drive import (
    FOLDER_MIME_TYPE,
    PROBE_ROLE,
    ROOT_LOGICAL_ID,
    ROOT_ROLE,
    GoogleDriveApiClient,
    GoogleDriveDatasetRoot,
    GoogleDriveObject,
    GoogleDriveProvider,
    GoogleDriveRootState,
)
from romcloud.infrastructure.remote_saves import build_remote_save_store


def owned_object(
    object_id: str,
    *,
    role: str,
    name: str = "object",
    folder: bool = False,
    parents: tuple[str, ...] = (),
) -> GoogleDriveObject:
    return GoogleDriveObject(
        object_id=object_id,
        name=name,
        mime_type=FOLDER_MIME_TYPE if folder else "application/octet-stream",
        size_bytes=None if folder else 4,
        revision="7",
        checksum="checksum" if not folder else None,
        modified_time="2026-01-01T00:00:00Z",
        parents=parents,
        app_properties={
            "romcloudOwner": "romcloud-savesync",
            "romcloudRole": role,
            "romcloudSchema": "1",
        },
        owned_by_me=True,
        app_authorized=True,
    )


class FakeApi:
    def __init__(self) -> None:
        self.objects: dict[str, GoogleDriveObject] = {}
        self.content: dict[str, bytes] = {}
        self.roots: list[GoogleDriveObject] = []
        self.generated = iter(["generated-root", "probe-id", "object-id"])
        self.about_error = None
        self.upload_error = None
        self.delete_error = None
        self.created_folders = []

    def about(self, operation=None):
        if operation is not None:
            operation.check()
        if self.about_error:
            raise self.about_error

    def generate_id(self, operation=None):
        if operation is not None:
            operation.check()
        return next(self.generated)

    def get_metadata(self, object_id, operation=None):
        if object_id not in self.objects:
            raise ProviderObjectNotFoundError("missing")
        return self.objects[object_id]

    def find_owned_roots(self, operation=None):
        return list(self.roots)

    def create_folder(self, object_id, *, name, app_properties, operation=None):
        item = owned_object(object_id, role=ROOT_ROLE, name=name, folder=True)
        self.objects[object_id] = item
        self.created_folders.append(object_id)
        return item

    def upload_bytes(
        self,
        object_id,
        *,
        parent_id,
        name,
        content,
        app_properties,
        operation=None,
    ):
        if self.upload_error:
            raise self.upload_error
        item = owned_object(
            object_id,
            role=app_properties["romcloudRole"],
            name=name,
            parents=(parent_id,),
        )
        self.objects[object_id] = item
        self.content[object_id] = content
        return item

    def download_bytes(self, object_id, operation=None):
        return self.content[object_id]

    def delete(self, object_id, operation=None):
        if self.delete_error:
            raise self.delete_error
        self.objects.pop(object_id, None)
        self.content.pop(object_id, None)


def provider(tmp_path: Path, api: FakeApi | None = None):
    fake = api or FakeApi()
    return GoogleDriveProvider(fake, GoogleDriveRootState(tmp_path / "root.json")), fake


def test_create_root_uses_generated_id_and_persists_identity(tmp_path: Path) -> None:
    drive, api = provider(tmp_path)

    root = drive.ensure_app_root()

    assert root.object_id == "generated-root"
    assert api.created_folders == ["generated-root"]
    assert GoogleDriveRootState(tmp_path / "root.json").load() == (
        "generated-root",
        None,
    )


def test_stored_root_id_is_reused_without_name_scan(tmp_path: Path) -> None:
    drive, api = provider(tmp_path)
    root = owned_object("stable-id", role=ROOT_ROLE, folder=True)
    api.objects[root.object_id] = root
    drive.root_state.save(folder_id=root.object_id)

    assert drive.ensure_app_root() == root
    assert api.created_folders == []


def test_deleted_stored_root_recovers_only_marked_root(tmp_path: Path) -> None:
    drive, api = provider(tmp_path)
    drive.root_state.save(folder_id="deleted-id")
    recovered = owned_object("recovered-id", role=ROOT_ROLE, folder=True)
    api.roots = [recovered]

    assert drive.ensure_app_root() == recovered
    assert drive.root_state.load() == ("recovered-id", None)


def test_duplicate_marked_roots_are_never_chosen_arbitrarily(tmp_path: Path) -> None:
    drive, api = provider(tmp_path)
    api.roots = [
        owned_object("first", role=ROOT_ROLE, folder=True),
        owned_object("second", role=ROOT_ROLE, folder=True),
    ]

    with pytest.raises(ProviderConflictError, match="Multiple"):
        drive.ensure_app_root()


def test_unmarked_same_name_folder_is_never_adopted(tmp_path: Path) -> None:
    drive, api = provider(tmp_path)
    external = owned_object(
        "external", role=ROOT_ROLE, name="ROMCloud SaveSync", folder=True
    )
    external = GoogleDriveObject(
        **{**external.__dict__, "app_properties": {}, "app_authorized": False}
    )
    api.roots = [external]

    root = drive.ensure_app_root()

    assert root.object_id == "generated-root"


def test_invalid_stored_object_is_refused_not_replaced(tmp_path: Path) -> None:
    drive, api = provider(tmp_path)
    external = owned_object("external", role=ROOT_ROLE, folder=True)
    external = GoogleDriveObject(**{**external.__dict__, "owned_by_me": False})
    api.objects[external.object_id] = external
    drive.root_state.save(folder_id=external.object_id)

    with pytest.raises(ProviderPermissionError, match="not verified"):
        drive.ensure_app_root()
    assert api.created_folders == []


def test_pending_generated_root_recovers_lost_create_reply(tmp_path: Path) -> None:
    drive, api = provider(tmp_path)
    pending = owned_object("pending-id", role=ROOT_ROLE, folder=True)
    api.objects[pending.object_id] = pending
    drive.root_state.save(pending_folder_id=pending.object_id)

    assert drive.ensure_app_root() == pending
    assert drive.root_state.load() == ("pending-id", None)
    assert api.created_folders == []


def test_read_write_probe_verifies_and_cleans_up(tmp_path: Path) -> None:
    drive, api = provider(tmp_path)

    access = drive.validate_access(ROOT_LOGICAL_ID)

    assert access.connected and access.readable and access.writable
    assert "probe-id" not in api.objects


def test_reachability_check_is_non_mutating(tmp_path: Path) -> None:
    drive, api = provider(tmp_path)

    assert drive.is_reachable(ROOT_LOGICAL_ID)
    assert api.created_folders == []
    assert api.objects == {}


@pytest.mark.parametrize(
    "error, expected",
    [
        (ProviderPermissionError("read only"), "read only"),
        (ProviderQuotaError("quota exceeded"), "quota exceeded"),
    ],
)
def test_write_probe_failures_preserve_readable_state(
    tmp_path: Path, error: Exception, expected: str
) -> None:
    drive, api = provider(tmp_path)
    api.upload_error = error

    access = drive.validate_access(ROOT_LOGICAL_ID)

    assert access.connected and access.readable
    assert not access.writable
    assert expected in access.detail


def test_cleanup_failure_is_reported_separately(tmp_path: Path) -> None:
    drive, api = provider(tmp_path)
    api.delete_error = ProviderPermissionError("cannot delete")

    access = drive.validate_access(ROOT_LOGICAL_ID)

    assert access.write_verified is True
    assert access.cleanup_verified is False
    assert "cleanup failed" in access.detail


@pytest.mark.parametrize(
    "error, connected",
    [
        (ProviderAuthRequiredError("authenticate"), False),
        (ProviderNotReachableError("offline"), False),
    ],
)
def test_unavailable_auth_or_network_is_not_readable(
    tmp_path: Path, error: Exception, connected: bool
) -> None:
    drive, api = provider(tmp_path)
    api.about_error = error
    access = drive.validate_access(ROOT_LOGICAL_ID)
    assert access.connected is connected
    assert not access.readable


def test_unknown_account_slot_is_rejected_without_network(tmp_path: Path) -> None:
    drive, api = provider(tmp_path)

    access = drive.validate_access("someone-elses-drive")

    assert not access.connected
    assert "Unknown" in access.detail
    assert api.created_folders == []


def test_cancelled_readiness_probe_stops_before_network(tmp_path: Path) -> None:
    drive, api = provider(tmp_path)
    cancellation = TransferCancellationToken()
    cancellation.cancel()

    with pytest.raises(TransferCancelledError):
        drive.validate_access(
            ROOT_LOGICAL_ID,
            RemoteOperationContext(cancellation=cancellation),
        )
    assert api.created_folders == []


def test_cancellation_after_probe_creation_still_cleans_up(
    tmp_path: Path, monkeypatch
) -> None:
    drive, api = provider(tmp_path)
    cancellation = TransferCancellationToken()

    def cancel_during_read(object_id, operation=None):
        cancellation.cancel()
        operation.check()

    monkeypatch.setattr(api, "download_bytes", cancel_during_read)

    with pytest.raises(TransferCancelledError):
        drive.validate_access(
            ROOT_LOGICAL_ID,
            RemoteOperationContext(cancellation=cancellation),
        )
    assert "probe-id" not in api.objects


def test_expired_readiness_deadline_stops_before_network(tmp_path: Path) -> None:
    drive, api = provider(tmp_path)

    access = drive.validate_access(
        ROOT_LOGICAL_ID,
        RemoteOperationContext(deadline=1.0, clock=lambda: 2.0),
    )

    assert not access.connected
    assert "deadline" in access.detail
    assert api.created_folders == []


def test_owned_object_upload_download_metadata_and_cleanup(tmp_path: Path) -> None:
    drive, _api = provider(tmp_path)
    root = drive.ensure_app_root()

    item = drive.upload_owned_bytes(
        parent_id=root.object_id,
        name="package.bin",
        content=b"data",
        role="package-test",
    )
    destination = tmp_path / "download" / "package.bin"
    metadata = drive.download_owned_object(
        item.object_id,
        destination,
        role="package-test",
        parent_id=root.object_id,
    )
    drive.delete_owned_object(
        item.object_id, role="package-test", parent_id=root.object_id
    )

    assert metadata.revision == "7"
    assert metadata.checksum == "checksum"
    assert destination.read_bytes() == b"data"


def test_arbitrary_object_delete_is_refused(tmp_path: Path) -> None:
    drive, api = provider(tmp_path)
    external = owned_object("external", role="other")
    external = GoogleDriveObject(**{**external.__dict__, "app_properties": {}})
    api.objects[external.object_id] = external

    with pytest.raises(ProviderPermissionError):
        drive.delete_owned_object("external")
    assert "external" in api.objects


def test_strategy_constructs_without_loose_object_or_filesystem_semantics(
    tmp_path: Path,
) -> None:
    drive, _api = provider(tmp_path)
    dataset = drive.remote_data_root(ROOT_LOGICAL_ID, "saves")

    store = build_remote_save_store(
        drive, connectivity_root=ROOT_LOGICAL_ID, dataset_root=dataset
    )

    assert isinstance(store, GoogleDriveRemoteSaveStore)
    assert store.filesystem_transaction_root is None
    assert store.ensure_root().is_folder
    with pytest.raises(SaveSyncWriteUnavailableError, match="not implemented"):
        store.materialize("psx/game.srm", tmp_path / "game.srm")


class FakeOAuth:
    def __init__(self) -> None:
        self.transport = None
        self.token = GoogleOAuthToken("access", "refresh", 9999999999)

    def usable_token(self, operation=None):
        return self.token

    def refresh(self, token, operation=None):
        return token


class QueueTransport:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = 0

    def request(self, method, url, **kwargs):
        self.calls += 1
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


def test_transient_requests_retry_with_bounded_backoff() -> None:
    transport = QueueTransport(
        [
            HttpResponse(503, {}, json.dumps({"error": {}}).encode()),
            HttpResponse(200, {}, json.dumps({"user": {}}).encode()),
        ]
    )
    sleeps = []
    client = GoogleDriveApiClient(
        FakeOAuth(),  # type: ignore[arg-type]
        transport=transport,
        max_attempts=2,
        sleeper=sleeps.append,
        random_source=lambda: 0.0,
    )

    client.about()

    assert transport.calls == 2
    assert sleeps == [1.0]


def test_transport_failure_retries_with_bounded_backoff() -> None:
    transport = QueueTransport(
        [
            ProviderNotReachableError("offline"),
            HttpResponse(200, {}, json.dumps({"user": {}}).encode()),
        ]
    )
    sleeps = []
    client = GoogleDriveApiClient(
        FakeOAuth(),  # type: ignore[arg-type]
        transport=transport,
        max_attempts=2,
        sleeper=sleeps.append,
        random_source=lambda: 0.0,
    )

    client.about()

    assert transport.calls == 2
    assert sleeps == [1.0]


def test_known_id_create_conflict_recovers_existing_object() -> None:
    existing = owned_object("known-id", role=ROOT_ROLE, folder=True)
    transport = QueueTransport(
        [
            HttpResponse(409, {}, json.dumps({"error": {}}).encode()),
            HttpResponse(
                200,
                {},
                json.dumps(
                    {
                        "id": existing.object_id,
                        "name": existing.name,
                        "mimeType": existing.mime_type,
                        "parents": [],
                        "appProperties": dict(existing.app_properties),
                        "ownedByMe": True,
                        "isAppAuthorized": True,
                        "trashed": False,
                    }
                ).encode(),
            ),
        ]
    )
    client = GoogleDriveApiClient(
        FakeOAuth(),  # type: ignore[arg-type]
        transport=transport,
        sleeper=lambda _seconds: None,
    )

    recovered = client.create_folder(
        "known-id",
        name="ROMCloud SaveSync",
        app_properties=existing.app_properties,
    )

    assert recovered.object_id == "known-id"
    assert transport.calls == 2


def test_quota_is_not_retried() -> None:
    transport = QueueTransport(
        [
            HttpResponse(
                403,
                {},
                json.dumps(
                    {"error": {"errors": [{"reason": "storageQuotaExceeded"}]}}
                ).encode(),
            )
        ]
    )
    client = GoogleDriveApiClient(
        FakeOAuth(),  # type: ignore[arg-type]
        transport=transport,
        sleeper=lambda _seconds: pytest.fail("quota must not retry"),
    )

    with pytest.raises(ProviderQuotaError):
        client.about()
    assert transport.calls == 1
