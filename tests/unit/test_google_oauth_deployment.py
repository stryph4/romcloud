from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from romcloud.core.exceptions import ConfigurationError
from romcloud.infrastructure.google_auth import GoogleOAuthClientConfig
from romcloud.lifecycle.install import (
    _download_google_oauth_metadata,
    reconcile_google_oauth_metadata,
)


def metadata(client_id: str = "release-client", secret: str = "release-secret") -> bytes:
    return json.dumps(
        {
            "schema_version": 1,
            "client_type": "tv_and_limited_input_device",
            "client_id": client_id,
            "client_secret": secret,
        }
    ).encode("utf-8")


def locator_project(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    locator = project / "runtime" / "google-oauth-client.url"
    locator.parent.mkdir(parents=True)
    locator.write_text("https://deploy.example/google.json\n", encoding="utf-8")
    return project


def test_production_source_advertises_external_https_metadata() -> None:
    project = Path(__file__).resolve().parents[2]
    locator = (
        project / "runtime" / "google-oauth-client.url"
    ).read_text(encoding="utf-8").strip()
    parsed = urlsplit(locator)

    assert parsed.scheme == "https"
    assert parsed.hostname
    assert "runtime/google-oauth-client.json" in (
        project / ".gitignore"
    ).read_text(encoding="utf-8").splitlines()


def test_metadata_locator_requires_plain_https() -> None:
    with pytest.raises(ConfigurationError, match="plain HTTPS"):
        _download_google_oauth_metadata("http://deploy.example/google.json")


def test_release_payload_is_deployed_to_exact_runtime_path(tmp_path: Path) -> None:
    home = tmp_path / "installed"
    project = tmp_path / "project"
    source = project / "runtime" / "google-oauth-client.json"
    source.parent.mkdir(parents=True)
    source.write_bytes(metadata())

    result = reconcile_google_oauth_metadata(
        romcloud_home=home,
        project_root=project,
        environment={},
    )

    target = home / "runtime" / "google-oauth-client.json"
    assert result.configured is True
    assert result.target_path == target
    assert result.source == "release_payload"
    assert GoogleOAuthClientConfig.load(home).client_id == "release-client"
    status = (home / "runtime" / "google-oauth-status.json").read_text()
    assert "release-client" not in status
    assert "release-secret" not in status
    if os.name == "posix":
        assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert not (home / "data" / "google-drive" / "token.json").exists()


def test_protected_build_environment_can_supply_metadata(tmp_path: Path) -> None:
    home = tmp_path / "installed"

    result = reconcile_google_oauth_metadata(
        romcloud_home=home,
        project_root=tmp_path / "project",
        environment={
            "ROMCLOUD_GOOGLE_OAUTH_CLIENT_ID": "environment-client",
            "ROMCLOUD_GOOGLE_OAUTH_CLIENT_SECRET": "environment-secret",
        },
    )

    assert result.configured is True
    assert result.source == "build_environment"
    assert GoogleOAuthClientConfig.load(home).client_id == "environment-client"


def test_committed_locator_fetches_external_release_metadata(tmp_path: Path) -> None:
    home = tmp_path / "installed"
    project = locator_project(tmp_path)
    calls = []

    result = reconcile_google_oauth_metadata(
        romcloud_home=home,
        project_root=project,
        environment={},
        fetcher=lambda url: calls.append(url) or metadata(),
    )

    assert result.configured is True
    assert result.source == "deployment_url"
    assert calls == ["https://deploy.example/google.json"]
    assert GoogleOAuthClientConfig.load(home).client_secret == "release-secret"


def test_unadvertised_source_without_existing_metadata_is_non_google_build(
    tmp_path: Path,
) -> None:
    result = reconcile_google_oauth_metadata(
        romcloud_home=tmp_path / "installed",
        project_root=tmp_path / "project",
        environment={},
    )

    assert result.configured is False
    assert result.source == "unavailable"
    assert not result.target_path.exists()


@pytest.mark.parametrize(
    "payload",
    [b"not-json", b"{}", b'{"client_id": 7, "client_secret": "secret"}'],
)
def test_malformed_staged_metadata_disables_optional_google_drive(
    tmp_path: Path, payload: bytes
) -> None:
    project = tmp_path / "project"
    source = project / "runtime" / "google-oauth-client.json"
    source.parent.mkdir(parents=True)
    source.write_bytes(payload)

    result = reconcile_google_oauth_metadata(
        romcloud_home=tmp_path / "installed",
        project_root=project,
        environment={},
    )

    assert result.configured is False
    assert "malformed" in result.warning


def test_malformed_remote_without_existing_metadata_disables_google_drive(
    tmp_path: Path,
) -> None:
    result = reconcile_google_oauth_metadata(
        romcloud_home=tmp_path / "installed",
        project_root=locator_project(tmp_path),
        environment={},
        fetcher=lambda _url: b"not-json",
    )

    assert result.configured is False
    assert "retrieved configuration was malformed" in result.warning
    assert not result.target_path.exists()


def test_endpoint_unavailable_without_existing_metadata_is_warning(
    tmp_path: Path,
) -> None:
    def unavailable(_url: str) -> bytes:
        raise ConfigurationError(
            "Google Drive deployment metadata could not be downloaded"
        )

    result = reconcile_google_oauth_metadata(
        romcloud_home=tmp_path / "installed",
        project_root=locator_project(tmp_path),
        environment={},
        fetcher=unavailable,
    )

    assert result.configured is False
    assert "could not be retrieved" in result.warning
    assert "Other ROMCloud features are unaffected" in result.warning


@pytest.mark.parametrize("remote", [None, b"not-json"])
def test_unavailable_or_malformed_remote_preserves_valid_existing_metadata(
    tmp_path: Path, remote: bytes | None
) -> None:
    home = tmp_path / "installed"
    target = home / "runtime" / "google-oauth-client.json"
    target.parent.mkdir(parents=True)
    target.write_bytes(metadata("existing-client", "existing-secret"))
    before = target.read_bytes()

    def fetch(_url: str) -> bytes:
        if remote is None:
            raise ConfigurationError("metadata could not be downloaded")
        return remote

    result = reconcile_google_oauth_metadata(
        romcloud_home=home,
        project_root=locator_project(tmp_path),
        environment={},
        fetcher=fetch,
    )

    assert result.configured is True
    assert result.source == "existing_runtime"
    assert "kept the previously installed configuration" in result.warning
    assert target.read_bytes() == before
    assert GoogleOAuthClientConfig.load(home).client_id == "existing-client"


def test_successful_remote_refresh_replaces_existing_metadata(tmp_path: Path) -> None:
    home = tmp_path / "installed"
    target = home / "runtime" / "google-oauth-client.json"
    target.parent.mkdir(parents=True)
    target.write_bytes(metadata("existing-client", "existing-secret"))

    result = reconcile_google_oauth_metadata(
        romcloud_home=home,
        project_root=locator_project(tmp_path),
        environment={},
        fetcher=lambda _url: metadata("refreshed-client", "refreshed-secret"),
    )

    assert result.configured is True
    assert result.source == "deployment_url"
    assert result.warning == ""
    assert GoogleOAuthClientConfig.load(home).client_id == "refreshed-client"


def test_explicit_required_marker_retains_fail_closed_behavior(tmp_path: Path) -> None:
    project = locator_project(tmp_path)
    (project / "runtime" / "google-oauth-client.required").write_text("required\n")

    with pytest.raises(ConfigurationError, match="could not be downloaded"):
        reconcile_google_oauth_metadata(
            romcloud_home=tmp_path / "installed",
            project_root=project,
            environment={},
            fetcher=lambda _url: (_ for _ in ()).throw(
                ConfigurationError("metadata could not be downloaded")
            ),
        )


@pytest.mark.parametrize(
    "payload",
    [
        {
            "schema_version": 1,
            "client_type": "desktop",
            "client_id": "id",
            "client_secret": "secret",
        },
        {
            "schema_version": 1,
            "client_type": "tv_and_limited_input_device",
            "client_id": "id",
            "client_secret": "secret",
            "unexpected": True,
        },
    ],
)
def test_download_requires_exact_limited_input_schema(
    tmp_path: Path, payload: dict[str, object]
) -> None:
    result = reconcile_google_oauth_metadata(
        romcloud_home=tmp_path / "installed",
        project_root=locator_project(tmp_path),
        environment={},
        fetcher=lambda _url: json.dumps(payload).encode(),
    )

    assert result.configured is False
    assert not result.target_path.exists()


def test_update_or_repair_preserves_and_validates_existing_runtime_metadata(
    tmp_path: Path,
) -> None:
    home = tmp_path / "installed"
    target = home / "runtime" / "google-oauth-client.json"
    target.parent.mkdir(parents=True)
    target.write_bytes(metadata("existing-client", "existing-secret"))

    result = reconcile_google_oauth_metadata(
        romcloud_home=home,
        project_root=tmp_path / "source-without-google-advertisement",
        environment={},
    )

    assert result.configured is True
    assert result.source == "existing_runtime"
    assert GoogleOAuthClientConfig.load(home).client_id == "existing-client"
