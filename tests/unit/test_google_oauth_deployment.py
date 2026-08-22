from __future__ import annotations

import json
from pathlib import Path

from romcloud.lifecycle.install import reconcile_google_oauth_metadata


def metadata(client_id: str = "legacy-client", secret: str = "legacy-secret") -> bytes:
    return json.dumps(
        {
            "schema_version": 1,
            "client_type": "tv_and_limited_input_device",
            "client_id": client_id,
            "client_secret": secret,
        }
    ).encode("utf-8")


def test_production_source_has_no_google_oauth_download_locator() -> None:
    project = Path(__file__).resolve().parents[2]

    assert not (project / "runtime" / "google-oauth-client.url").exists()
    assert "runtime/google-oauth-client.json" in (
        project / ".gitignore"
    ).read_text(encoding="utf-8").splitlines()


def test_normal_reconcile_ignores_release_and_environment_metadata(
    tmp_path: Path,
) -> None:
    home = tmp_path / "installed"
    project = tmp_path / "project"
    staged = project / "runtime" / "google-oauth-client.json"
    staged.parent.mkdir(parents=True)
    staged.write_bytes(metadata("release-client", "release-secret"))

    result = reconcile_google_oauth_metadata(
        romcloud_home=home,
        project_root=project,
        environment={
            "ROMCLOUD_EXPERIMENTAL_GOOGLE_DRIVE": "1",
            "ROMCLOUD_GOOGLE_OAUTH_CLIENT_ID": "environment-client",
            "ROMCLOUD_GOOGLE_OAUTH_CLIENT_SECRET": "environment-secret",
        },
    )

    assert result.configured is False
    assert result.source == "dormant"
    assert not result.target_path.exists()
    assert not (home / "runtime" / "google-oauth-status.json").exists()


def test_reconcile_preserves_valid_existing_experimental_metadata_byte_for_byte(
    tmp_path: Path,
) -> None:
    home = tmp_path / "installed"
    target = home / "runtime" / "google-oauth-client.json"
    target.parent.mkdir(parents=True)
    before = metadata("existing-client", "existing-secret")
    target.write_bytes(before)

    result = reconcile_google_oauth_metadata(
        romcloud_home=home,
        project_root=tmp_path / "project",
        environment={},
    )

    assert result.configured is True
    assert result.source == "existing_runtime"
    assert target.read_bytes() == before


def test_reconcile_does_not_delete_or_rewrite_malformed_legacy_metadata(
    tmp_path: Path,
) -> None:
    home = tmp_path / "installed"
    target = home / "runtime" / "google-oauth-client.json"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"legacy-unreadable-state")

    result = reconcile_google_oauth_metadata(
        romcloud_home=home,
        project_root=tmp_path / "project",
        environment={},
    )

    assert result.configured is True
    assert result.source == "existing_runtime"
    assert target.read_bytes() == b"legacy-unreadable-state"


def test_reconcile_never_touches_existing_experimental_token_state(
    tmp_path: Path,
) -> None:
    home = tmp_path / "installed"
    token_path = home / "data" / "google-drive" / "token.json"
    token_path.parent.mkdir(parents=True)
    token_path.write_bytes(b"opaque-encrypted-token-envelope")

    reconcile_google_oauth_metadata(
        romcloud_home=home,
        project_root=tmp_path / "project",
        environment={},
    )

    assert token_path.read_bytes() == b"opaque-encrypted-token-envelope"
