from __future__ import annotations

import base64
import os
from dataclasses import dataclass
from typing import Optional

from romcloud.infrastructure import hardware_identity

try:
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF

    CRYPTO_AVAILABLE = True
except ImportError:  # pragma: no cover - exercised only when the dependency is missing
    CRYPTO_AVAILABLE = False

FORMAT_VERSION = 2
SCHEME = "aes-256-gcm"
KDF_NAME = "hkdf-sha256"
DEGRADED_LABEL = "none"

_HKDF_INFO = b"romcloud-credential-v1"
_SALT_BYTES = 32
_NONCE_BYTES = 12


class CredentialCryptoUnavailableError(Exception):
    """The ``cryptography`` dependency is not importable in this environment."""


class CredentialDecryptionError(Exception):
    """A stored credential envelope exists but could not be decrypted.

    Deliberately does not distinguish "hardware/binding changed" from "file
    corrupted" — both require the identical recovery UX: ask the user to
    re-enter the password and re-encrypt against the current machine.
    """


@dataclass(frozen=True)
class CredentialEnvelope:
    format_version: int
    scheme: str
    kdf: str
    hardware_binding: str  # "+"-joined identifier type names, or "none"
    salt: str  # base64
    nonce: str  # base64
    ciphertext: str  # base64 (AEAD tag included)

    def to_toml_dict(self) -> dict:
        return {
            "format_version": self.format_version,
            "scheme": self.scheme,
            "kdf": self.kdf,
            "hardware_binding": self.hardware_binding,
            "salt": self.salt,
            "nonce": self.nonce,
            "ciphertext": self.ciphertext,
        }

    @classmethod
    def from_toml_dict(cls, data: dict) -> "CredentialEnvelope":
        return cls(
            format_version=int(data["format_version"]),
            scheme=str(data["scheme"]),
            kdf=str(data["kdf"]),
            hardware_binding=str(data["hardware_binding"]),
            salt=str(data["salt"]),
            nonce=str(data["nonce"]),
            ciphertext=str(data["ciphertext"]),
        )


def is_envelope(data: object) -> bool:
    """Whether a parsed TOML section is the new envelope format rather
    than a legacy plaintext ``password = "..."`` section."""
    return isinstance(data, dict) and "format_version" in data and "ciphertext" in data


def is_hardware_bound(binding_types: tuple[str, ...]) -> bool:
    return bool(binding_types)


def current_binding_types() -> tuple[str, ...]:
    return hardware_identity.probe_binding_types()


def protection_label(envelope: CredentialEnvelope) -> str:
    """Human-readable, honest description for setup summaries/healthcheck."""
    if envelope.hardware_binding != DEGRADED_LABEL:
        identifiers = envelope.hardware_binding.replace("+", ", ")
        return f"hardware-bound credential protection ({identifiers})"
    return (
        "basic credential protection only — no hardware-binding identifier "
        "is available on this device, so this does not protect against a "
        "copy of ROMCloud's stored data"
    )


def _require_crypto() -> None:
    if not CRYPTO_AVAILABLE:
        raise CredentialCryptoUnavailableError(
            "The 'cryptography' package is not available in this environment."
        )


def _derive_key(material: str, salt: bytes) -> bytes:
    hkdf = HKDF(algorithm=hashes.SHA256(), length=32, salt=salt, info=_HKDF_INFO)
    return hkdf.derive(material.encode("utf-8"))


def _material_for(binding_types: tuple[str, ...]) -> str:
    if not binding_types:
        machine_id = hardware_identity.read_machine_id() or ""
        return f"machine-id\x1f{machine_id}"
    return hardware_identity.gather_binding_material(binding_types)


def _aad(format_version: int, scheme: str, kdf: str) -> bytes:
    return f"{format_version}|{scheme}|{kdf}".encode("ascii")


def encrypt_password(
    password: str, *, binding_types: Optional[tuple[str, ...]] = None
) -> CredentialEnvelope:
    """Encrypt *password* into a versioned, authenticated envelope.

    *binding_types* defaults to a fresh probe of this machine's currently
    usable hardware-binding identifiers; passing it explicitly is used only
    by tests to make hardware-bound behavior deterministic.
    """
    _require_crypto()
    types = binding_types if binding_types is not None else current_binding_types()
    material = _material_for(types)
    salt = os.urandom(_SALT_BYTES)
    nonce = os.urandom(_NONCE_BYTES)
    key = _derive_key(material, salt)
    aad = _aad(FORMAT_VERSION, SCHEME, KDF_NAME)
    ciphertext = AESGCM(key).encrypt(nonce, password.encode("utf-8"), aad)
    return CredentialEnvelope(
        format_version=FORMAT_VERSION,
        scheme=SCHEME,
        kdf=KDF_NAME,
        hardware_binding="+".join(types) if types else DEGRADED_LABEL,
        salt=base64.b64encode(salt).decode("ascii"),
        nonce=base64.b64encode(nonce).decode("ascii"),
        ciphertext=base64.b64encode(ciphertext).decode("ascii"),
    )


def decrypt_password(envelope: CredentialEnvelope) -> str:
    """Decrypt *envelope*, re-probing hardware-binding material live.

    Raises :class:`CredentialDecryptionError` for any failure — wrong
    hardware, corrupted envelope, or an unsupported format/scheme/kdf. The
    error message is always credential-free.
    """
    _require_crypto()
    if (
        envelope.format_version != FORMAT_VERSION
        or envelope.scheme != SCHEME
        or envelope.kdf != KDF_NAME
    ):
        raise CredentialDecryptionError(
            "Unsupported credential envelope format/scheme/kdf."
        )
    binding_types = (
        () if envelope.hardware_binding == DEGRADED_LABEL else tuple(envelope.hardware_binding.split("+"))
    )
    material = _material_for(binding_types)
    try:
        salt = base64.b64decode(envelope.salt)
        nonce = base64.b64decode(envelope.nonce)
        ciphertext = base64.b64decode(envelope.ciphertext)
        key = _derive_key(material, salt)
        aad = _aad(envelope.format_version, envelope.scheme, envelope.kdf)
        plaintext = AESGCM(key).decrypt(nonce, ciphertext, aad)
    except Exception as exc:  # noqa: BLE001 - any failure means "cannot unlock"
        raise CredentialDecryptionError(
            "Stored network credentials could not be unlocked on this hardware."
        ) from exc
    return plaintext.decode("utf-8")
