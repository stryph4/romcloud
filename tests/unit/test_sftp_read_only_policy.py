from __future__ import annotations

from contextlib import contextmanager
from types import SimpleNamespace

from ports_gfx.library_sync_screen import LibrarySyncScreenState
from romcloud.core.capabilities import Capability
from romcloud.infrastructure.capabilities import capability_policy
from romcloud.infrastructure.providers.sftp import SFTPProvider


def test_sftp_remote_data_disables_savesync_but_keeps_library_sync() -> None:
    config = SimpleNamespace(
        game_access_mode="smart_cache",
        remote_data=SimpleNamespace(provider="sftp"),
    )

    policy = capability_policy(config)

    assert policy.allows(Capability.SAVE_SYNC) is False
    assert policy.allows(Capability.LIBRARY_SYNC) is True
    assert "read-only SFTP" in str(policy.decision(Capability.SAVE_SYNC).reason)


def test_sftp_validation_never_mutates_even_when_legacy_probe_flag_is_set(
    monkeypatch,
) -> None:
    provider = SFTPProvider(
        host="example.invalid",
        username="reader",
        password="secret",
        trusted_host_key_fingerprint="SHA256:test",
        probe_writable=True,
    )

    class FakeSFTP:
        def listdir(self, root):  # noqa: ANN001
            assert root == "/romcloud"
            return []

        def open(self, *args, **kwargs):  # noqa: ANN002, ANN003
            raise AssertionError("read-only validation must never open a remote file for writing")

        def remove(self, *args, **kwargs):  # noqa: ANN002, ANN003
            raise AssertionError("read-only validation must never delete remote data")

    @contextmanager
    def fake_session():
        yield FakeSFTP()

    monkeypatch.setattr(provider, "_session", fake_session)

    result = provider.validate_access("/romcloud")

    assert result.connected is True
    assert result.read_verified is True
    assert result.write_verified is None
    assert result.writable is False
    assert "read-only" in result.detail.lower()


def test_library_sync_screen_routes_read_only_sftp_to_pull(monkeypatch) -> None:
    state = LibrarySyncScreenState(
        romcloud_bin="romcloud",
        sync_mode="quick",
        pull_only=True,
    )
    calls: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        state,
        "_start_operation",
        lambda action, payload: calls.append((action, payload)),
    )

    state.start_sync()

    assert state.sync_label == "Quick Pull"
    assert calls == [("library-sync-pull", {})]

    state.sync_mode = "full"
    state.start_sync()

    assert state.sync_label == "Full Pull"
    assert calls[-1] == ("library-sync-pull-full", {})
