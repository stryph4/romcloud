"""Unit tests for romcloud.infrastructure.credentials.

Covers the existing TOML-based SMB password storage and the new
`mount.cifs`-format credentials file writer used by the mount integration.
Both must always be written with mode 0600.
"""

from __future__ import annotations

import stat
from pathlib import Path

from romcloud.infrastructure.credentials import (
    migrate_legacy_smb_credentials,
    load_smb_password,
    write_cifs_credentials_file,
    write_smb_password,
)


class TestSmbPasswordToml:
    def test_write_then_load_round_trip(self, tmp_path: Path):
        path = tmp_path / "credentials.toml"
        write_smb_password(path, "hunter2")
        assert load_smb_password(path) == "hunter2"

    def test_written_with_mode_0600(self, tmp_path: Path):
        path = tmp_path / "credentials.toml"
        write_smb_password(path, "hunter2")
        mode = path.stat().st_mode & 0o777
        assert mode == 0o600

    def test_load_missing_file_returns_none(self, tmp_path: Path):
        assert load_smb_password(tmp_path / "nope.toml") is None

    def test_special_characters_are_escaped(self, tmp_path: Path):
        path = tmp_path / "credentials.toml"
        write_smb_password(path, 'pa"ss\\word')
        assert load_smb_password(path) == 'pa"ss\\word'


class TestLegacySmbCredentialsMigration:
    def test_current_credentials_preserve_password_and_remove_legacy(self, tmp_path: Path):
        current = tmp_path / "credentials.toml"
        legacy = tmp_path / "smb.credentials"

        write_smb_password(current, "current-secret")
        legacy.write_text('[smb]\npassword = "legacy-secret"\n', encoding="utf-8")
        legacy.chmod(0o600)

        assert migrate_legacy_smb_credentials(current) is True
        assert load_smb_password(current) == "current-secret"
        assert not legacy.exists()
        assert current.stat().st_mode & 0o777 == 0o600
        assert load_smb_password(current) == "current-secret"

    def test_legacy_only_migrates_into_canonical_file(self, tmp_path: Path):
        current = tmp_path / "credentials.toml"
        legacy = tmp_path / "smb.credentials"

        legacy.write_text('[smb]\npassword = "legacy-secret"\n', encoding="utf-8")
        legacy.chmod(0o600)

        assert migrate_legacy_smb_credentials(current) is True
        assert load_smb_password(current) == "legacy-secret"
        assert current.exists()
        assert current.stat().st_mode & 0o777 == 0o600
        assert not legacy.exists()
        assert load_smb_password(current) == "legacy-secret"

    def test_failed_migration_leaves_legacy_file_in_place(self, tmp_path: Path):
        current = tmp_path / "credentials.toml"
        legacy = tmp_path / "smb.credentials"

        legacy.write_text("not romcloud legacy format", encoding="utf-8")

        assert migrate_legacy_smb_credentials(current) is False
        assert load_smb_password(current) is None
        assert legacy.exists()
        assert not current.exists()

    def test_unrelated_same_named_file_is_preserved(self, tmp_path: Path):
        current = tmp_path / "credentials.toml"
        legacy = tmp_path / "smb.credentials"

        legacy.write_text('[other]\nvalue = "keep me"\n', encoding="utf-8")

        assert migrate_legacy_smb_credentials(current) is False
        assert load_smb_password(current) is None
        assert legacy.exists()

    def test_repeated_migration_is_idempotent(self, tmp_path: Path):
        current = tmp_path / "credentials.toml"
        legacy = tmp_path / "smb.credentials"

        legacy.write_text('password = "legacy-secret"\n', encoding="utf-8")

        assert migrate_legacy_smb_credentials(current) is True
        assert load_smb_password(current) == "legacy-secret"
        assert migrate_legacy_smb_credentials(current) is False
        assert current.exists()
        assert not legacy.exists()


class TestCifsCredentialsFile:
    def test_content_format(self, tmp_path: Path):
        path = tmp_path / "smb-cifs-credentials"
        write_cifs_credentials_file(path, "alice", "hunter2")
        content = path.read_text()
        assert "username=alice" in content
        assert "password=hunter2" in content

    def test_written_with_mode_0600(self, tmp_path: Path):
        path = tmp_path / "smb-cifs-credentials"
        write_cifs_credentials_file(path, "alice", "hunter2")
        mode = path.stat().st_mode & 0o777
        assert mode == 0o600

    def test_domain_included_when_given(self, tmp_path: Path):
        path = tmp_path / "smb-cifs-credentials"
        write_cifs_credentials_file(path, "alice", "hunter2", domain="WORKGROUP")
        assert "domain=WORKGROUP" in path.read_text()

    def test_domain_omitted_when_not_given(self, tmp_path: Path):
        path = tmp_path / "smb-cifs-credentials"
        write_cifs_credentials_file(path, "alice", "hunter2")
        assert "domain=" not in path.read_text()

    def test_overwriting_existing_file_still_ends_with_0600(self, tmp_path: Path):
        path = tmp_path / "smb-cifs-credentials"
        path.write_text("stale content")
        path.chmod(0o644)

        write_cifs_credentials_file(path, "alice", "newpassword")

        assert (path.stat().st_mode & 0o777) == 0o600
        assert "newpassword" in path.read_text()
        assert "stale content" not in path.read_text()

    def test_creates_parent_directory(self, tmp_path: Path):
        path = tmp_path / "nested" / "dir" / "smb-cifs-credentials"
        write_cifs_credentials_file(path, "alice", "hunter2")
        assert path.exists()
