from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from romcloud.core.exceptions import ConfigurationError
from romcloud.infrastructure.google_auth import GoogleOAuthClientConfig
from romcloud.lifecycle.install import reconcile_google_oauth_metadata


def metadata(client_id: str = "release-client", secret: str = "release-secret") -> bytes:
    return json.dumps(
        {"client_id": client_id, "client_secret": secret}
    ).encode("utf-8")


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
    project = tmp_path / "project"
    locator = project / "runtime" / "google-oauth-client.url"
    locator.parent.mkdir(parents=True)
    locator.write_text("https://deploy.example/google.json\n", encoding="utf-8")
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
def test_malformed_advertised_metadata_fails_deployment(
    tmp_path: Path, payload: bytes
) -> None:
    project = tmp_path / "project"
    source = project / "runtime" / "google-oauth-client.json"
    source.parent.mkdir(parents=True)
    source.write_bytes(payload)

    with pytest.raises(ConfigurationError, match="malformed"):
        reconcile_google_oauth_metadata(
            romcloud_home=tmp_path / "installed",
            project_root=project,
            environment={},
        )


def test_malformed_downloaded_metadata_fails_deployment(tmp_path: Path) -> None:
    project = tmp_path / "project"
    locator = project / "runtime" / "google-oauth-client.url"
    locator.parent.mkdir(parents=True)
    locator.write_text("https://deploy.example/google.json", encoding="utf-8")

    with pytest.raises(ConfigurationError, match="malformed"):
        reconcile_google_oauth_metadata(
            romcloud_home=tmp_path / "installed",
            project_root=project,
            environment={},
            fetcher=lambda _url: b"not-json",
        )


def test_missing_advertised_metadata_fails_deployment(tmp_path: Path) -> None:
    project = tmp_path / "project"
    locator = project / "runtime" / "google-oauth-client.url"
    locator.parent.mkdir(parents=True)
    locator.write_text("https://deploy.example/google.json", encoding="utf-8")

    def unavailable(_url: str) -> bytes:
        raise ConfigurationError(
            "Google Drive deployment metadata could not be downloaded"
        )

    with pytest.raises(ConfigurationError, match="could not be downloaded"):
        reconcile_google_oauth_metadata(
            romcloud_home=tmp_path / "installed",
            project_root=project,
            environment={},
            fetcher=unavailable,
        )


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
