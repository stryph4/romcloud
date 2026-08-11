"""Unit tests for romcloud.infrastructure.credentials.

Covers the encrypted credential envelope (hardware-bound and degraded),
plaintext-legacy migration, the `mount.cifs`-format ephemeral credentials
file used by the mount integration, and lock-state detection used for the
hardware-change recovery UX. Persisted secret files must always be mode
0600.
"""

from __future__ import annotations

import stat
import tomllib
from pathlib import Path

import pytest

from romcloud.infrastructure import credential_crypto
from romcloud.infrastructure.credentials import (
    credential_lock_state,
    describe_protection,
    ephemeral_cifs_credentials_file,
    load_remote_data_smb_password,
    migrate_legacy_smb_credentials,
    migrate_plaintext_credentials,
    load_smb_password,
    write_remote_data_smb_password,
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

    def test_source_and_remote_data_passwords_are_independent(self, tmp_path: Path):
        path = tmp_path / "credentials.toml"
        write_smb_password(path, "source-secret")
        write_remote_data_smb_password(path, "remote-secret")

        assert load_smb_password(path) == "source-secret"
        assert load_remote_data_smb_password(path) == "remote-secret"

        write_smb_password(path, "new-source-secret")
        assert load_smb_password(path) == "new-source-secret"
        assert load_remote_data_smb_password(path) == "remote-secret"

    def test_remote_password_can_exist_without_source_password(self, tmp_path: Path):
        path = tmp_path / "credentials.toml"
        write_remote_data_smb_password(path, "remote-only")

        assert load_smb_password(path) is None
        assert load_remote_data_smb_password(path) == "remote-only"
        assert path.stat().st_mode & 0o777 == 0o600


class TestLegacySmbCredentialsMigration:
    def _write_cifs_legacy(self, path: Path, username: str = "testuser", password: str = "testpass") -> None:
        path.write_text(f"username={username}\npassword={password}\n", encoding="utf-8")

    def test_current_credentials_preserve_password_and_remove_legacy(self, tmp_path: Path):
        current = tmp_path / "credentials.toml"
        legacy = tmp_path / "smb.credentials"

        write_smb_password(current, "current-secret")
        self._write_cifs_legacy(legacy, password="legacy-secret")
        legacy.chmod(0o600)

        assert migrate_legacy_smb_credentials(current) is True
        assert load_smb_password(current) == "current-secret"
        assert not legacy.exists()
        assert current.stat().st_mode & 0o777 == 0o600
        assert load_smb_password(current) == "current-secret"

    def test_legacy_only_migrates_into_canonical_file(self, tmp_path: Path):
        current = tmp_path / "credentials.toml"
        legacy = tmp_path / "smb.credentials"

        self._write_cifs_legacy(legacy)
        legacy.chmod(0o600)

        assert migrate_legacy_smb_credentials(current) is True
        assert load_smb_password(current) == "testpass"
        assert current.exists()
        assert current.stat().st_mode & 0o777 == 0o600
        assert not legacy.exists()
        assert load_smb_password(current) == "testpass"

    def test_cifs_legacy_with_whitespace_variation_is_recognized(self, tmp_path: Path):
        current = tmp_path / "credentials.toml"
        legacy = tmp_path / "smb.credentials"

        legacy.write_text("  username = testuser  \n\npassword = testpass\n", encoding="utf-8")

        assert migrate_legacy_smb_credentials(current) is True
        assert load_smb_password(current) == "testpass"
        assert not legacy.exists()

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

    def test_extra_unexpected_keys_are_preserved_conservatively(self, tmp_path: Path):
        current = tmp_path / "credentials.toml"
        legacy = tmp_path / "smb.credentials"

        legacy.write_text("username=testuser\npassword=testpass\ndomain=WORKGROUP\n", encoding="utf-8")

        assert migrate_legacy_smb_credentials(current) is False
        assert load_smb_password(current) is None
        assert legacy.exists()

    def test_repeated_migration_is_idempotent(self, tmp_path: Path):
        current = tmp_path / "credentials.toml"
        legacy = tmp_path / "smb.credentials"

        self._write_cifs_legacy(legacy)

        assert migrate_legacy_smb_credentials(current) is True
        assert load_smb_password(current) == "testpass"
        assert migrate_legacy_smb_credentials(current) is False
        assert current.exists()
        assert not legacy.exists()

    def test_toml_legacy_format_still_supported(self, tmp_path: Path):
        current = tmp_path / "credentials.toml"
        legacy = tmp_path / "smb.credentials"

        legacy.write_text('[smb]\npassword = "legacy-secret"\n', encoding="utf-8")

        assert migrate_legacy_smb_credentials(current) is True
        assert load_smb_password(current) == "legacy-secret"
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


class TestEphemeralCifsCredentialsFile:
    def test_file_exists_only_within_the_context_manager(self, tmp_path: Path):
        with ephemeral_cifs_credentials_file(tmp_path, "alice", "hunter2") as path:
            assert path.exists()
            assert (path.stat().st_mode & 0o777) == 0o600
            assert "username=alice" in path.read_text()
            assert "password=hunter2" in path.read_text()
            captured = path
        assert not captured.exists()

    def test_removed_even_if_the_mount_attempt_raises(self, tmp_path: Path):
        captured = None
        with pytest.raises(RuntimeError, match="mount failed"):
            with ephemeral_cifs_credentials_file(tmp_path, "alice", "hunter2") as path:
                captured = path
                raise RuntimeError("mount failed")
        assert not captured.exists()

    def test_unique_temp_filename_each_call(self, tmp_path: Path):
        with ephemeral_cifs_credentials_file(tmp_path, "alice", "hunter2") as first:
            first_name = first.name
        with ephemeral_cifs_credentials_file(tmp_path, "alice", "hunter2") as second:
            assert second.name != first_name


class TestEncryptedEnvelopeFormat:
    def test_written_file_is_a_versioned_envelope_not_plaintext(self, tmp_path: Path):
        path = tmp_path / "credentials.toml"
        write_smb_password(path, "hunter2")

        raw = path.read_text()
        assert "hunter2" not in raw
        data = tomllib.loads(raw)
        assert data["smb"]["format_version"] == credential_crypto.FORMAT_VERSION
        assert data["smb"]["scheme"] == credential_crypto.SCHEME
        assert data["smb"]["kdf"] == credential_crypto.KDF_NAME
        assert "ciphertext" in data["smb"]

    def test_hardware_bound_round_trip_via_public_api(self, tmp_path: Path, monkeypatch):
        monkeypatch.setattr(
            "romcloud.infrastructure.hardware_identity.probe_binding_types",
            lambda: ("board_serial",),
        )
        monkeypatch.setattr(
            "romcloud.infrastructure.hardware_identity.gather_binding_material",
            lambda types, **k: "board_serial\x1fREALBOARD123",
        )
        path = tmp_path / "credentials.toml"
        write_smb_password(path, "hunter2")

        assert load_smb_password(path) == "hunter2"
        assert credential_lock_state(path, "smb") == "unlocked"
        assert "hardware-bound" in describe_protection(path, "smb")

    def test_degraded_mode_when_no_hardware_identifier(self, tmp_path: Path, monkeypatch):
        monkeypatch.setattr(
            "romcloud.infrastructure.hardware_identity.probe_binding_types",
            lambda: (),
        )
        monkeypatch.setattr(
            "romcloud.infrastructure.hardware_identity.read_machine_id",
            lambda **k: "fixed-machine-id",
        )
        path = tmp_path / "credentials.toml"
        write_smb_password(path, "hunter2")

        assert load_smb_password(path) == "hunter2"
        protection = describe_protection(path, "smb")
        assert "basic" in protection.lower()
        assert "does not protect" in protection.lower()

    def test_hardware_change_makes_credential_unreadable_but_detectable(
        self, tmp_path: Path, monkeypatch
    ):
        materials = {"value": "board_serial\x1fORIGINAL"}
        monkeypatch.setattr(
            "romcloud.infrastructure.hardware_identity.probe_binding_types",
            lambda: ("board_serial",),
        )
        monkeypatch.setattr(
            "romcloud.infrastructure.hardware_identity.gather_binding_material",
            lambda types, **k: materials["value"],
        )
        path = tmp_path / "credentials.toml"
        write_smb_password(path, "hunter2")

        # Simulate moving the installation to different hardware.
        materials["value"] = "board_serial\x1fDIFFERENT"

        assert load_smb_password(path) is None
        assert credential_lock_state(path, "smb") == "locked"


class TestMigratePlaintextCredentials:
    def _write_legacy_plaintext(self, path: Path, password: str = "old-secret") -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f'[smb]\npassword = "{password}"\n', encoding="utf-8")
        path.chmod(0o600)

    def test_migrates_plaintext_smb_section_to_envelope(self, tmp_path: Path):
        path = tmp_path / "credentials.toml"
        self._write_legacy_plaintext(path, "old-secret")

        assert migrate_plaintext_credentials(path) is True

        raw = path.read_text()
        assert "old-secret" not in raw
        assert load_smb_password(path) == "old-secret"

    def test_no_op_when_already_encrypted(self, tmp_path: Path):
        path = tmp_path / "credentials.toml"
        write_smb_password(path, "hunter2")
        assert migrate_plaintext_credentials(path) is False
        assert load_smb_password(path) == "hunter2"

    def test_no_op_when_file_missing(self, tmp_path: Path):
        assert migrate_plaintext_credentials(tmp_path / "nope.toml") is False

    def test_retrying_after_successful_migration_is_a_no_op(self, tmp_path: Path):
        path = tmp_path / "credentials.toml"
        self._write_legacy_plaintext(path, "old-secret")

        assert migrate_plaintext_credentials(path) is True
        assert migrate_plaintext_credentials(path) is False  # already migrated
        assert load_smb_password(path) == "old-secret"

    def test_failed_verification_restores_the_original_plaintext_file(
        self, tmp_path: Path, monkeypatch
    ):
        path = tmp_path / "credentials.toml"
        self._write_legacy_plaintext(path, "old-secret")
        original_bytes = path.read_bytes()

        # Simulate a verification failure right after the re-encrypted file
        # is written (e.g. a decrypt bug or hardware flake mid-migration):
        # the initial read (to capture the password to re-encrypt) must
        # still see the real value, but the post-write verification read
        # must not match it.
        import romcloud.infrastructure.credentials as credentials_module

        real_load_smb_password = credentials_module.load_smb_password
        calls = {"count": 0}

        def flaky_load_smb_password(p):
            calls["count"] += 1
            if calls["count"] == 1:
                return real_load_smb_password(p)
            return "wrong-value-triggers-rollback"

        monkeypatch.setattr(credentials_module, "load_smb_password", flaky_load_smb_password)

        assert migrate_plaintext_credentials(path) is False
        assert path.read_bytes() == original_bytes
        assert (path.stat().st_mode & 0o777) == 0o600

    def test_migrates_remote_data_section_when_only_that_is_plaintext(self, tmp_path: Path):
        path = tmp_path / "credentials.toml"
        path.write_text('[remote_data_smb]\npassword = "remote-secret"\n', encoding="utf-8")
        path.chmod(0o600)

        assert migrate_plaintext_credentials(path) is True
        assert load_remote_data_smb_password(path) == "remote-secret"
        assert "remote-secret" not in path.read_text()


class TestCredentialLockState:
    def test_missing_file_is_missing(self, tmp_path: Path):
        assert credential_lock_state(tmp_path / "nope.toml", "smb") == "missing"

    def test_no_section_is_missing(self, tmp_path: Path):
        path = tmp_path / "credentials.toml"
        write_smb_password(path, "hunter2")
        assert credential_lock_state(path, "remote_data_smb") == "missing"

    def test_valid_envelope_is_unlocked(self, tmp_path: Path):
        path = tmp_path / "credentials.toml"
        write_smb_password(path, "hunter2")
        assert credential_lock_state(path, "smb") == "unlocked"

    def test_legacy_plaintext_is_unlocked(self, tmp_path: Path):
        path = tmp_path / "credentials.toml"
        path.write_text('[smb]\npassword = "hunter2"\n', encoding="utf-8")
        assert credential_lock_state(path, "smb") == "unlocked"

