"""Credential storage.

Credentials (e.g. SMB password) are stored in a separate TOML file at mode
0600, never in the main config.  This file is never logged.
"""

from __future__ import annotations

import os
import stat
import sys
from pathlib import Path
from typing import Optional

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


def load_smb_password(credentials_path: Path) -> Optional[str]:
    """Read the SMB password from the credentials file.

    Returns None if no credentials file exists or no password is set.
    Never raises; logs warnings via the standard library.
    """
    if not credentials_path.exists():
        return None
    if tomllib is None:
        return None

    try:
        with credentials_path.open("rb") as fh:
            data = tomllib.load(fh)
        return data.get("smb", {}).get("password")
    except Exception:  # noqa: BLE001
        return None


def write_smb_password(credentials_path: Path, password: str) -> None:
    """Write the SMB password to the credentials file with mode 0600."""
    credentials_path.parent.mkdir(parents=True, exist_ok=True)

    content = "[smb]\n"
    # Simple escaping: TOML basic strings need backslash and quote escaping.
    escaped = password.replace("\\", "\\\\").replace('"', '\\"')
    content += f'password = "{escaped}"\n'

    credentials_path.write_text(content, encoding="utf-8")
    credentials_path.chmod(stat.S_IRUSR | stat.S_IWUSR)  # 0600


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

    Always written with mode 0600.
    """
    path.parent.mkdir(parents=True, exist_ok=True)

    lines = [f"username={username}", f"password={password}"]
    if domain:
        lines.append(f"domain={domain}")
    content = "\n".join(lines) + "\n"

    # Create/truncate with restrictive permissions from the start, rather than
    # writing world/group-readable content and chmod'ing afterwards.
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(content)
    finally:
        pass
    path.chmod(stat.S_IRUSR | stat.S_IWUSR)  # 0600, in case an existing file had looser perms
