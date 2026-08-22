from __future__ import annotations

from pathlib import Path

import pytest

from ports_gfx.client import BackendResult
from ports_gfx.wizard import WizardState, WizardStep

from romcloud.infrastructure.config import (
    AppConfig,
    CacheConfig,
    RemoteDataConfig,
    SourceConfig,
    load_config,
    write_config,
)
from romcloud.infrastructure.google_auth import GoogleOAuthToken, GoogleTokenStore
from romcloud.lifecycle.setup import SetupRequest, _build_config, setup_state
from romcloud.lifecycle.google_drive_setup import google_drive_build_status


def config(tmp_path: Path) -> AppConfig:
    return AppConfig(
        source=SourceConfig("none", "", selected_systems=()),
        cache=CacheConfig(str(tmp_path / "unused-cache")),
        local_roms_path=str(tmp_path / "local-roms"),
        data_path=str(tmp_path / "data"),
        remote_data=RemoteDataConfig("google_drive", "romcloud-savesync"),
    )


def test_google_drive_configuration_round_trips_as_opaque_logical_root(
    tmp_path: Path,
) -> None:
    path = tmp_path / "config" / "romcloud.toml"
    write_config(config(tmp_path), str(path))

    loaded = load_config(str(path), resolve_paths=False)

    assert loaded.source.provider == "none"
    assert loaded.remote_data == RemoteDataConfig(
        "google_drive", "romcloud-savesync"
    )


def test_token_material_is_never_serialized_to_main_config(tmp_path: Path) -> None:
    path = tmp_path / "config" / "romcloud.toml"
    configured = config(tmp_path)
    write_config(configured, str(path))
    token_store = GoogleTokenStore(
        Path(configured.data_path) / "google-drive" / "token.json"
    )
    token_store.save(GoogleOAuthToken("access-secret", "refresh-secret", 999999))

    rendered = path.read_text(encoding="utf-8")

    assert "google_drive" in rendered
    assert "romcloud-savesync" in rendered
    assert "access-secret" not in rendered
    assert "refresh-secret" not in rendered
    assert token_store.path.is_file()


def test_standalone_setup_request_accepts_google_without_source_or_paths(
    tmp_path: Path,
) -> None:
    request = SetupRequest.from_payload(
        {
            "source_type": "none",
            "remote_data_type": "google_drive",
            "cache_root": "unused",
            "max_size_gb": 50,
            "min_free_gb": 5,
        },
        validate_cache=False,
    )

    built = _build_config(tmp_path / "config" / "romcloud.toml", request, None)

    assert built.source.provider == "none"
    assert built.remote_data == RemoteDataConfig(
        "google_drive", "romcloud-savesync"
    )


def test_wizard_google_choice_starts_auth_and_skips_library_sync(monkeypatch) -> None:
    wizard = WizardState()
    wizard.source_type = "none"
    wizard.step = WizardStep.REMOTE_DATA
    wizard.selected_index = 3
    calls = []

    def start(step, action, romcloud_bin):
        calls.append((step, action, romcloud_bin))
        wizard.step = step

    monkeypatch.setattr(wizard, "_start_operation", start)

    wizard._confirm("romcloud", show_osk=False)

    assert wizard.remote_data_type == "google_drive"
    assert wizard.library_sync_enabled is False
    assert wizard.step == WizardStep.REMOTE_GOOGLE_AUTH
    assert calls == [
        (
            WizardStep.REMOTE_GOOGLE_AUTH,
            "setup-google-drive-auth-start",
            "romcloud",
        )
    ]
    assert wizard._post_storage_step() == WizardStep.REVIEW


def test_wizard_auth_code_is_user_facing() -> None:
    wizard = WizardState()
    wizard.step = WizardStep.REMOTE_GOOGLE_AUTH
    wizard.google_verification_url = "https://google.com/device"
    wizard.google_user_code = "ABCD-EFGH"
    wizard.notice = (
        f"Open {wizard.google_verification_url} and enter {wizard.google_user_code}."
    )

    assert "ABCD-EFGH" in wizard.notice
    assert wizard.options[0] == "I've authorized this device"


def test_missing_build_metadata_is_reported_and_not_started(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.delenv("ROMCLOUD_GOOGLE_OAUTH_CLIENT_ID", raising=False)
    monkeypatch.delenv("ROMCLOUD_GOOGLE_OAUTH_CLIENT_SECRET", raising=False)
    config_path = tmp_path / "config" / "romcloud.toml"
    build_status = google_drive_build_status(config_path)
    assert setup_state(config_path)["google_drive_available"] is False
    wizard = WizardState(BackendResult(True, build_status))
    wizard.step = WizardStep.REMOTE_DATA
    wizard.selected_index = 3
    monkeypatch.setattr(
        wizard,
        "_start_operation",
        lambda *_args: pytest.fail("unconfigured Google Drive must not start"),
    )

    wizard._confirm("romcloud", show_osk=False)

    assert "not configured in this build" in wizard.options[3]
    assert wizard.error == "Google Drive is not configured in this ROMCloud build."


def test_auth_backend_build_error_is_shown_directly(monkeypatch) -> None:
    wizard = WizardState()
    wizard.step = WizardStep.REMOTE_GOOGLE_AUTH
    wizard.runner = type(
        "FinishedRunner",
        (),
        {"is_finished": True, "poll": lambda self: [], "cancel": lambda self: None},
    )()
    monkeypatch.setattr(
        "ports_gfx.wizard.operation_result",
        lambda _runner: BackendResult(
            False,
            {},
            "Google Drive is not configured in this ROMCloud build.",
        ),
    )

    wizard.poll()

    assert wizard.error == "Google Drive is not configured in this ROMCloud build."
