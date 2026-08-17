"""Credential storage.

SMB passwords are stored in a separate TOML file (mode 0600), never in the
main config, using a versioned authenticated-encryption envelope (see
:mod:`romcloud.infrastructure.credential_crypto`) rather than plaintext.
This file is never logged.

Two protection levels exist and are always distinguished honestly (see
``credential_crypto.protection_label``): **hardware-bound**, where the key
requires DMI hardware-binding identifiers not stored in ROMCloud's own
persisted data, and **degraded/basic**, used only when no such identifier
is available, which does not protect against a copy of ROMCloud's own
storage. A plaintext ``cryptography``-unavailable fallback is the
narrowest, clearly-flagged last resort.

Legacy Batocera installs may still have a legacy ``smb.credentials`` file,
or a ``credentials.toml`` predating the encrypted envelope format (plain
``password = "..."``). This module owns both migration paths.
"""

from __future__ import annotations

import contextlib
import os
import stat
import sys
import tempfile
from pathlib import Path
from typing import Iterator, Optional

from romcloud.infrastructure import credential_crypto
from romcloud.infrastructure.atomic_file import atomic_write_text
from romcloud.infrastructure.logging import get_logger

if sys.version_info >= (3, 11):
    import tomllib
else:
    try:
        import tomllib  # type: ignore[no-redef]
    except ImportError:
        try:
            import tomli as tomllib  # type: ignore[no-redef]
        except ImportError:
            tomllib = None  # type: ignore[assignment]

log = get_logger("credentials")

_LEGACY_CREDENTIALS_FILENAME = "smb.credentials"
_SECTIONS = ("smb", "remote_data_smb", "sftp", "remote_data_sftp")


def load_smb_password(credentials_path: Path) -> Optional[str]:
    """Read the SMB password from the credentials file.

    Returns None if no credentials file exists, no password is set, or the
    stored envelope cannot be decrypted (e.g. moved to different hardware —
    see :func:`credential_lock_state` to distinguish that case explicitly).
    Never raises.
    """
    return _read_password(credentials_path, "smb")


def load_remote_data_smb_password(credentials_path: Path) -> Optional[str]:
    """Read the independent remote-data SMB password, if configured."""
    return _read_password(credentials_path, "remote_data_smb")


def credential_lock_state(credentials_path: Path, section: str = "smb") -> str:
    """One of ``"missing"``, ``"unlocked"``, ``"locked"``.

    ``"locked"`` means an encrypted envelope exists for *section* but could
    not be decrypted on this machine (hardware-binding material changed, or
    the file is corrupted) — distinct from simply having no password
    configured, so callers can surface an accurate, actionable message.
    """
    data = _load_toml(credentials_path)
    if data is None:
        return "missing"
    section_data = data.get(section)
    if not isinstance(section_data, dict):
        return "missing"
    if credential_crypto.is_envelope(section_data):
        try:
            envelope = credential_crypto.CredentialEnvelope.from_toml_dict(section_data)
            credential_crypto.decrypt_password(envelope)
        except Exception:  # noqa: BLE001
            return "locked"
        return "unlocked"
    password = section_data.get("password")
    return "unlocked" if isinstance(password, str) and password else "missing"


def describe_protection(credentials_path: Path, section: str = "smb") -> Optional[str]:
    """Human-readable, honest protection-level description for *section*,
    or None if no credential is stored there at all."""
    section_data_raw = _load_toml(credentials_path)
    section_data = section_data_raw.get(section) if section_data_raw else None
    if not isinstance(section_data, dict):
        return None
    if credential_crypto.is_envelope(section_data):
        try:
            envelope = credential_crypto.CredentialEnvelope.from_toml_dict(section_data)
        except Exception:  # noqa: BLE001
            return None
        return credential_crypto.protection_label(envelope)
    if isinstance(section_data.get("password"), str) and section_data["password"]:
        return "plaintext storage only (legacy format, not yet migrated)"
    return None


def migrate_legacy_smb_credentials(credentials_path: Path) -> bool:
    """Migrate or clean up a legacy ``smb.credentials`` file.

    Returns True when a legacy file was successfully migrated or removed.
    The function is best-effort, local-only, and safe to call repeatedly.
    """
    legacy_path = _legacy_credentials_path(credentials_path)
    legacy_password = _read_legacy_password(legacy_path)
    if legacy_password is None:
        return False

    if load_smb_password(credentials_path) is not None:
        legacy_path.unlink(missing_ok=True)
        return True

    try:
        write_smb_password(credentials_path, legacy_password)
    except Exception:  # noqa: BLE001
        return False

    legacy_path.unlink(missing_ok=True)
    return True


def migrate_plaintext_credentials(credentials_path: Path) -> bool:
    """Re-encrypt any still-plaintext password section(s) in *credentials_path*.

    Best-effort and safely retryable: any failure leaves the existing,
    still-fully-usable file completely untouched (verified by reading the
    re-encrypted values back and comparing before considering migration
    done; a mismatch restores the pre-migration bytes). A crash at any
    point either leaves the original file (atomic write never completed)
    or the fully-written new file (verification step below simply
    confirms it and does nothing) — never a partial/corrupt state.
    """
    data = _load_toml(credentials_path)
    if data is None:
        return False

    if not any(_is_legacy_plaintext_section(data.get(section)) for section in _SECTIONS):
        return False

    smb_password = load_smb_password(credentials_path)
    remote_password = load_remote_data_smb_password(credentials_path)
    if smb_password is None and remote_password is None:
        return False

    backup = credentials_path.read_bytes()
    original_mode = credentials_path.stat().st_mode & 0o777
    try:
        if smb_password is not None:
            write_smb_password(credentials_path, smb_password)
        else:
            write_remote_data_smb_password(credentials_path, remote_password)  # type: ignore[arg-type]

        if smb_password is not None and load_smb_password(credentials_path) != smb_password:
            raise RuntimeError("post-migration verification failed for [smb]")
        if (
            remote_password is not None
            and load_remote_data_smb_password(credentials_path) != remote_password
        ):
            raise RuntimeError("post-migration verification failed for [remote_data_smb]")
    except Exception:  # noqa: BLE001
        log.warning("Credential migration failed; restoring previous credentials file")
        credentials_path.write_bytes(backup)
        credentials_path.chmod(original_mode)
        return False
    return True


def write_smb_password(credentials_path: Path, password: str) -> None:
    """Atomically write the SMB password to the credentials file, mode 0600."""
    _write_password_section(credentials_path, "smb", password)


def write_remote_data_smb_password(credentials_path: Path, password: str) -> None:
    """Store the remote-data SMB password without replacing source credentials."""
    _write_password_section(credentials_path, "remote_data_smb", password)


def load_sftp_password(credentials_path: Path) -> Optional[str]:
    """Read the ROM-source SFTP password from the credentials file."""
    return _read_password(credentials_path, "sftp")


def load_remote_data_sftp_password(credentials_path: Path) -> Optional[str]:
    """Read the independent remote-data SFTP password, if configured."""
    return _read_password(credentials_path, "remote_data_sftp")


def write_sftp_password(credentials_path: Path, password: str) -> None:
    """Store the ROM-source SFTP password, independent of any other section."""
    _write_password_section(credentials_path, "sftp", password)


def write_remote_data_sftp_password(credentials_path: Path, password: str) -> None:
    """Store the remote-data SFTP password, independent of the source's own
    SFTP credentials (or of any SMB credentials)."""
    _write_password_section(credentials_path, "remote_data_sftp", password)


def cifs_credentials_path(main_credentials_path: Path) -> Path:
    """Legacy fixed location of the (now-retired) permanent ``mount.cifs``
    credentials file. No longer written to — mounting uses
    :func:`ephemeral_cifs_credentials_file` instead. Kept only so
    uninstall/repair can clean up files left behind by older ROMCloud
    versions."""
    return main_credentials_path.parent / "smb-cifs-credentials"


def remote_data_cifs_credentials_path(main_credentials_path: Path) -> Path:
    """Legacy fixed location for the remote-data target — see
    :func:`cifs_credentials_path`."""
    return main_credentials_path.parent / "remote-data-smb-cifs-credentials"


def write_cifs_credentials_file(
    path: Path,
    username: str,
    password: str,
    domain: Optional[str] = None,
) -> None:
    """Write a ``mount.cifs -o credentials=<file>`` style credentials file
    at a caller-chosen fixed *path*.

    Kept for callers that need a specific path (e.g. tests); real mounting
    should prefer :func:`ephemeral_cifs_credentials_file`, which removes
    the file again immediately after use instead of leaving it permanently
    on disk. Always written atomically with mode 0600.
    """
    atomic_write_text(
        path, _cifs_credentials_content(username, password, domain), mode=stat.S_IRUSR | stat.S_IWUSR
    )


@contextlib.contextmanager
def ephemeral_cifs_credentials_file(
    directory: Path,
    username: str,
    password: str,
    *,
    prefix: str = ".romcloud-cifs-",
    domain: Optional[str] = None,
) -> Iterator[Path]:
    """Write a short-lived ``mount.cifs`` credentials file for exactly one
    mount attempt, and always remove it afterward — success or failure.

    ``mount.cifs`` only reads this file to build the initial ``mount(2)``
    call; the kernel's own transparent CIFS reconnect logic re-uses session
    state it already holds, not this file, so nothing needs it to persist
    on disk after the mount succeeds.
    """
    directory.mkdir(parents=True, exist_ok=True)
    fd, path_str = tempfile.mkstemp(prefix=prefix, dir=str(directory))
    path = Path(path_str)
    try:
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)  # 0600 before any content is written
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(_cifs_credentials_content(username, password, domain))
        yield path
    finally:
        path.unlink(missing_ok=True)


def _cifs_credentials_content(username: str, password: str, domain: Optional[str] = None) -> str:
    lines = [f"username={username}", f"password={password}"]
    if domain:
        lines.append(f"domain={domain}")
    return "\n".join(lines) + "\n"


def _legacy_credentials_path(credentials_path: Path) -> Path:
    return credentials_path.with_name(_LEGACY_CREDENTIALS_FILENAME)


def _load_toml(credentials_path: Path) -> Optional[dict]:
    if tomllib is None:
        return None
    try:
        with credentials_path.open("rb") as fh:
            data = tomllib.load(fh)
    except Exception:  # noqa: BLE001
        return None
    return data if isinstance(data, dict) else None


def _is_legacy_plaintext_section(section_data: object) -> bool:
    return (
        isinstance(section_data, dict)
        and isinstance(section_data.get("password"), str)
        and bool(section_data["password"])
        and not credential_crypto.is_envelope(section_data)
    )


def _read_password(credentials_path: Path, section: str) -> Optional[str]:
    data = _load_toml(credentials_path)
    if data is None:
        return None
    return _password_from_mapping(data, section)


def _read_legacy_password(legacy_path: Path) -> Optional[str]:
    if not legacy_path.exists():
        return None

    password = _read_password(legacy_path, "smb")
    if password is not None:
        return password

    return _read_legacy_cifs_password(legacy_path)


def _read_legacy_cifs_password(legacy_path: Path) -> Optional[str]:
    try:
        raw = legacy_path.read_text(encoding="utf-8")
    except OSError:
        return None

    values: dict[str, str] = {}
    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if "=" not in stripped:
            return None
        key, value = stripped.split("=", 1)
        key = key.strip()
        value = value.strip()
        if key not in {"username", "password"}:
            return None
        if key in values:
            return None
        values[key] = value

    if set(values) != {"username", "password"}:
        return None

    password = values["password"]
    if not password:
        return None
    return password


def _password_from_mapping(data, section: str = "smb") -> Optional[str]:
    if not isinstance(data, dict):
        return None

    section_data = data.get(section)
    if isinstance(section_data, dict):
        if credential_crypto.is_envelope(section_data):
            try:
                envelope = credential_crypto.CredentialEnvelope.from_toml_dict(section_data)
                return credential_crypto.decrypt_password(envelope)
            except Exception:  # noqa: BLE001
                return None
        password = section_data.get("password")
        if isinstance(password, str) and password:
            return password

    if section == "smb":
        password = data.get("password")
        if isinstance(password, str) and password:
            return password

    return None


def _encrypt_or_fallback(password: str) -> dict:
    """Return the TOML-serializable mapping to persist for one password.

    Encrypts whenever the ``cryptography`` dependency is available (the
    normal case). Falls back to plaintext only if it is not — the
    narrowest documented compatibility fallback, never silently mistaken
    for real protection.
    """
    try:
        envelope = credential_crypto.encrypt_password(password)
        return envelope.to_toml_dict()
    except credential_crypto.CredentialCryptoUnavailableError:
        log.warning(
            "Credential encryption unavailable ('cryptography' package missing); "
            "storing SMB password in plaintext as a last-resort fallback."
        )
        return {"password": password}


def _write_password_section(credentials_path: Path, section: str, password: str) -> None:
    passwords = {name: _read_password(credentials_path, name) for name in _SECTIONS}
    passwords[section] = password

    lines: list[str] = []
    for name in _SECTIONS:
        value = passwords[name]
        if value is None:
            continue
        payload = _encrypt_or_fallback(value)
        if lines:
            lines.append("\n")
        lines.append(f"[{name}]\n")
        for key, val in payload.items():
            if isinstance(val, str):
                escaped = val.replace("\\", "\\\\").replace('"', '\\"')
                lines.append(f'{key} = "{escaped}"\n')
            else:
                lines.append(f"{key} = {val}\n")

    atomic_write_text(
        credentials_path,
        "".join(lines),
        mode=stat.S_IRUSR | stat.S_IWUSR,
    )
