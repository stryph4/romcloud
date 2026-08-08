"""Credential storage.

Credentials (e.g. SMB password) are stored in a separate TOML file at mode
0600, never in the main config.  This file is never logged.

Legacy Batocera installs may still have ``smb.credentials`` alongside the
canonical ``credentials.toml``. This module owns the migration path for that
legacy file and keeps the derived ``smb-cifs-credentials`` helper separate.
"""

from __future__ import annotations

import stat
import sys
from pathlib import Path
from typing import Optional

from romcloud.infrastructure.atomic_file import atomic_write_text

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


_LEGACY_CREDENTIALS_FILENAME = "smb.credentials"


def load_smb_password(credentials_path: Path) -> Optional[str]:
    """Read the SMB password from the credentials file.

    Returns None if no credentials file exists or no password is set.
    Never raises; logs warnings via the standard library.
    """
    return _read_toml_smb_password(credentials_path)


def migrate_legacy_smb_credentials(credentials_path: Path) -> bool:
    """Migrate or clean up a legacy ``smb.credentials`` file.

    Returns True when a legacy file was successfully migrated or removed.
    The function is best-effort, local-only, and safe to call repeatedly.
    """
    legacy_path = _legacy_credentials_path(credentials_path)
    legacy_password = _read_legacy_password(legacy_path)
    if legacy_password is None:
        return False

    if credentials_path.exists():
        legacy_path.unlink(missing_ok=True)
        return True

    try:
        write_smb_password(credentials_path, legacy_password)
    except Exception:  # noqa: BLE001
        return False

    legacy_path.unlink(missing_ok=True)
    return True


def write_smb_password(credentials_path: Path, password: str) -> None:
    """Atomically write the SMB password to the credentials file, mode 0600."""
    content = "[smb]\n"
    # Simple escaping: TOML basic strings need backslash and quote escaping.
    escaped = password.replace("\\", "\\\\").replace('"', '\\"')
    content += f'password = "{escaped}"\n'

    atomic_write_text(credentials_path, content, mode=stat.S_IRUSR | stat.S_IWUSR)


def cifs_credentials_path(main_credentials_path: Path) -> Path:
    """Standard location for the ``mount.cifs``-format credentials file,
    derived from the main (TOML) credentials file's own path — kept
    alongside it so both are covered by the same directory permissions."""
    return main_credentials_path.parent / "smb-cifs-credentials"


def write_cifs_credentials_file(
    path: Path,
    username: str,
    password: str,
    domain: Optional[str] = None,
) -> None:
    """Write a ``mount.cifs -o credentials=<file>`` style credentials file.

    This is the format ``mount.cifs`` itself expects — plain ``key=value``
    lines, *not* TOML. Keeping the password out of the mount command line
    (rather than passing ``username=...,password=...`` as a ``-o`` option)
    means it never appears in ``ps`` output, shell history, or logs.

    Always written atomically with mode 0600.
    """
    lines = [f"username={username}", f"password={password}"]
    if domain:
        lines.append(f"domain={domain}")
    content = "\n".join(lines) + "\n"

    atomic_write_text(path, content, mode=stat.S_IRUSR | stat.S_IWUSR)


def _legacy_credentials_path(credentials_path: Path) -> Path:
    return credentials_path.with_name(_LEGACY_CREDENTIALS_FILENAME)


def _read_toml_smb_password(credentials_path: Path) -> Optional[str]:
    if tomllib is None:
        return None

    try:
        with credentials_path.open("rb") as fh:
            data = tomllib.load(fh)
        return _password_from_mapping(data)
    except Exception:  # noqa: BLE001
        return None


def _read_legacy_password(legacy_path: Path) -> Optional[str]:
    if not legacy_path.exists():
        return None

    return _read_toml_smb_password(legacy_path)


def _password_from_mapping(data) -> Optional[str]:
    if not isinstance(data, dict):
        return None

    smb_data = data.get("smb")
    if isinstance(smb_data, dict):
        password = smb_data.get("password")
        if isinstance(password, str) and password:
            return password

    password = data.get("password")
    if isinstance(password, str) and password:
        return password

    return None
