"""Capability-based write gating for SaveSync remote-data.

Uses a real local directory wrapped by a fake provider that reports
non-durable, non-filesystem-semantics capabilities (mirroring SFTP's real
characteristics) so SaveSync's provider-generic scan/read path is exercised
end-to-end without needing a live network server. Local/mounted-SMB
behavior itself is unaffected by this work and is covered by the existing
SaveSync test suite; these tests are about the new gating.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from romcloud.core.exceptions import SaveSyncWriteUnavailableError
from romcloud.core.storage import ProviderCapabilities
from romcloud.infrastructure.providers.local import LocalFilesystemProvider
from romcloud.services.saves import SaveSyncService

_NON_DURABLE = ProviderCapabilities(
    has_filesystem_semantics=False,
    can_resume_download=False,
    supports_durable_transactions=False,
)


class _NonDurableProvider(LocalFilesystemProvider):
    """Real local I/O (reusing the already-tested Local implementation) but
    reporting SFTP-like capabilities, so SaveSync must route through
    scan_provider_tree_report()/_remote_path()'s provider-generic fetch path
    instead of raw local Path access, exactly as it would for real SFTP."""

    PROVIDER_ID = "fake-protocol"

    @property
    def capabilities(self) -> ProviderCapabilities:
        return _NON_DURABLE


def _service(tmp_path: Path) -> SaveSyncService:
    local = tmp_path / "local"
    remote = tmp_path / "remote"
    local.mkdir()
    remote.mkdir()
    return SaveSyncService(
        provider=_NonDurableProvider(),
        connectivity_root=str(remote),
        local_root=str(local),
        remote_root=str(remote),
        state_path=tmp_path / "data" / "savesync-state.json",
    )


def _write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)


class TestCapabilityHelpers:
    def test_non_durable_provider_reports_no_filesystem_semantics(self, tmp_path):
        service = _service(tmp_path)
        assert service._remote_has_filesystem_semantics() is False
        assert service._remote_supports_durable_transactions() is False


class TestReadOnlyConsumptionRemainsAvailable:
    def test_preview_upload_scans_remote_via_provider(self, tmp_path):
        service = _service(tmp_path)
        _write(Path(tmp_path) / "local" / "nes" / "game.srm", b"local-save")
        diff = service.preview_upload()
        assert any(entry.relative_path == "nes/game.srm" for entry in diff.entries)

    def test_preview_download_scans_remote_via_provider(self, tmp_path):
        service = _service(tmp_path)
        _write(Path(tmp_path) / "remote" / "nes" / "game.srm", b"remote-save")
        diff = service.preview_download()
        assert any(entry.relative_path == "nes/game.srm" for entry in diff.entries)

    def test_commit_download_fetches_remote_content_into_local(self, tmp_path):
        service = _service(tmp_path)
        _write(Path(tmp_path) / "remote" / "nes" / "game.srm", b"remote-save")
        diff = service.preview_download()
        service.commit_download(diff)
        assert (
            Path(tmp_path) / "local" / "nes" / "game.srm"
        ).read_bytes() == b"remote-save"


class TestWriteDependentOperationsAreGated:
    def test_commit_upload_is_gated(self, tmp_path):
        service = _service(tmp_path)
        _write(Path(tmp_path) / "local" / "nes" / "game.srm", b"local-save")
        diff = service.preview_upload()
        with pytest.raises(SaveSyncWriteUnavailableError):
            service.commit_upload(diff)
        # Never partially written to the non-durable remote.
        assert not (Path(tmp_path) / "remote" / "nes" / "game.srm").exists()

    def test_reconcile_with_only_downloads_is_not_gated(self, tmp_path):
        service = _service(tmp_path)
        _write(Path(tmp_path) / "remote" / "nes" / "game.srm", b"remote-save")
        report = service.reconcile()
        assert report.downloaded == 1
        assert report.uploaded == 0
        assert (
            Path(tmp_path) / "local" / "nes" / "game.srm"
        ).read_bytes() == b"remote-save"

    def test_reconcile_requiring_an_upload_is_gated(self, tmp_path):
        service = _service(tmp_path)
        _write(Path(tmp_path) / "local" / "nes" / "game.srm", b"local-save")
        with pytest.raises(SaveSyncWriteUnavailableError):
            service.reconcile()
        # Download-only capability must not be silently downgraded to a
        # partial/half-applied reconciliation.
        assert not (Path(tmp_path) / "remote" / "nes" / "game.srm").exists()
