"""Unit tests for romcloud.infrastructure.credential_crypto.

Covers the versioned authenticated-encryption envelope: hardware-bound and
degraded key derivation, round-trip correctness, tamper/wrong-hardware
failure, and honest (non-oversold) protection-level wording.
"""

from __future__ import annotations

import pytest

from romcloud.infrastructure import credential_crypto as cc


class TestEncryptDecryptRoundTrip:
    def test_hardware_bound_round_trip(self, monkeypatch):
        monkeypatch.setattr(
            "romcloud.infrastructure.hardware_identity.gather_binding_material",
            lambda types, **k: "board_serial\x1fREALBOARD123",
        )
        envelope = cc.encrypt_password("hunter2", binding_types=("board_serial",))
        assert envelope.hardware_binding == "board_serial"
        assert cc.decrypt_password(envelope) == "hunter2"

    def test_degraded_round_trip_uses_machine_id(self, monkeypatch):
        monkeypatch.setattr(
            "romcloud.infrastructure.hardware_identity.read_machine_id",
            lambda **k: "fixed-machine-id",
        )
        envelope = cc.encrypt_password("hunter2", binding_types=())
        assert envelope.hardware_binding == cc.DEGRADED_LABEL
        assert cc.decrypt_password(envelope) == "hunter2"

    def test_ciphertext_never_contains_the_plaintext_password(self, monkeypatch):
        monkeypatch.setattr(
            "romcloud.infrastructure.hardware_identity.gather_binding_material",
            lambda types, **k: "board_serial\x1fREALBOARD123",
        )
        envelope = cc.encrypt_password("super-secret-password", binding_types=("board_serial",))
        assert "super-secret-password" not in envelope.ciphertext
        assert "super-secret-password" not in str(envelope.to_toml_dict())


class TestHardwareChangeDetection:
    def test_decrypt_fails_when_binding_material_changes(self, monkeypatch):
        materials = {"value": "board_serial\x1fORIGINAL"}
        monkeypatch.setattr(
            "romcloud.infrastructure.hardware_identity.gather_binding_material",
            lambda types, **k: materials["value"],
        )
        envelope = cc.encrypt_password("hunter2", binding_types=("board_serial",))

        materials["value"] = "board_serial\x1fDIFFERENT"  # simulate moved hardware
        with pytest.raises(cc.CredentialDecryptionError):
            cc.decrypt_password(envelope)

    def test_same_material_and_salt_derive_identical_keys(self):
        salt = b"\x00" * 32
        key1 = cc._derive_key("board_serial\x1fSAME", salt)
        key2 = cc._derive_key("board_serial\x1fSAME", salt)
        assert key1 == key2

    def test_different_material_derives_different_key(self):
        salt = b"\x00" * 32
        key1 = cc._derive_key("board_serial\x1fONE", salt)
        key2 = cc._derive_key("board_serial\x1fTWO", salt)
        assert key1 != key2


class TestTamperDetection:
    def test_tampered_ciphertext_fails_to_decrypt(self, monkeypatch):
        monkeypatch.setattr(
            "romcloud.infrastructure.hardware_identity.gather_binding_material",
            lambda types, **k: "board_serial\x1fREALBOARD123",
        )
        envelope = cc.encrypt_password("hunter2", binding_types=("board_serial",))
        tampered = cc.CredentialEnvelope(
            **{**envelope.to_toml_dict(), "ciphertext": envelope.ciphertext[:-4] + "AAAA"}
        )
        with pytest.raises(cc.CredentialDecryptionError):
            cc.decrypt_password(tampered)

    def test_tampered_associated_metadata_fails_to_decrypt(self, monkeypatch):
        monkeypatch.setattr(
            "romcloud.infrastructure.hardware_identity.gather_binding_material",
            lambda types, **k: "board_serial\x1fREALBOARD123",
        )
        envelope = cc.encrypt_password("hunter2", binding_types=("board_serial",))
        # Flipping the recorded scheme changes the AAD used at decrypt time,
        # so the AEAD tag must no longer verify even though the ciphertext
        # bytes themselves are untouched.
        tampered = cc.CredentialEnvelope(**{**envelope.to_toml_dict(), "scheme": "aes-256-gcm-v2"})
        with pytest.raises(cc.CredentialDecryptionError):
            cc.decrypt_password(tampered)


class TestProtectionLabel:
    def test_hardware_bound_label_names_identifier_types(self, monkeypatch):
        monkeypatch.setattr(
            "romcloud.infrastructure.hardware_identity.gather_binding_material",
            lambda types, **k: "board_serial\x1fREALBOARD123",
        )
        envelope = cc.encrypt_password("hunter2", binding_types=("product_uuid", "board_serial"))
        label = cc.protection_label(envelope)
        assert "hardware-bound" in label
        assert "product_uuid" in label and "board_serial" in label
        # Must never overstate the guarantee as secret/TPM-grade.
        assert "secret" not in label.lower()
        assert "tpm" not in label.lower()

    def test_degraded_label_is_explicit_about_the_limitation(self, monkeypatch):
        monkeypatch.setattr(
            "romcloud.infrastructure.hardware_identity.read_machine_id",
            lambda **k: "fixed-machine-id",
        )
        envelope = cc.encrypt_password("hunter2", binding_types=())
        label = cc.protection_label(envelope)
        assert "basic" in label.lower()
        assert "does not protect" in label.lower()


class TestCryptoUnavailableFallback:
    def test_encrypt_raises_when_cryptography_unavailable(self, monkeypatch):
        monkeypatch.setattr(cc, "CRYPTO_AVAILABLE", False)
        with pytest.raises(cc.CredentialCryptoUnavailableError):
            cc.encrypt_password("hunter2", binding_types=())

    def test_decrypt_raises_when_cryptography_unavailable(self, monkeypatch):
        envelope = cc.encrypt_password("hunter2", binding_types=())
        monkeypatch.setattr(cc, "CRYPTO_AVAILABLE", False)
        with pytest.raises(cc.CredentialCryptoUnavailableError):
            cc.decrypt_password(envelope)


class TestIsEnvelope:
    def test_recognizes_new_format(self):
        envelope = cc.encrypt_password("hunter2", binding_types=())
        assert cc.is_envelope(envelope.to_toml_dict()) is True

    def test_rejects_legacy_plaintext_section(self):
        assert cc.is_envelope({"password": "hunter2"}) is False

    def test_rejects_non_dict(self):
        assert cc.is_envelope("not a dict") is False
        assert cc.is_envelope(None) is False
