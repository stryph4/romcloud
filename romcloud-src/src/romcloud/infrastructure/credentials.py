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
