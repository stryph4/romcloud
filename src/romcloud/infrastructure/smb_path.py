"""Canonical handling for paths relative to an SMB share."""

from __future__ import annotations


_FORBIDDEN_CHARACTERS = frozenset({'"', "\n", "\r", "\x00", ","})


def normalize_smb_remote_path(value: object) -> str:
    """Return a safe share-relative path.

    Setup UIs historically display a share-relative path with one leading
    slash, so one outer slash is normalized away. Separators within the path
    are not collapsed: empty, current-directory, and parent-directory
    components are rejected before the value reaches ``smbclient`` or a CIFS
    UNC.
    """
    raw = str(value or "").replace("\\", "/")
    if any(character in raw for character in _FORBIDDEN_CHARACTERS):
        raise ValueError("SMB remote path contains unsafe characters.")
    if raw.startswith("//") or (
        len(raw) >= 3 and raw[1] == ":" and raw[2] == "/"
    ):
        raise ValueError("SMB remote path must be relative to the selected share.")

    if raw.startswith("/"):
        raw = raw[1:]
    if raw.endswith("/"):
        raw = raw[:-1]
    if not raw:
        return ""
    if raw.startswith("/") or raw.endswith("/"):
        raise ValueError("SMB remote path contains malformed separators.")
    parts = raw.split("/")
    if any(part in ("", ".", "..") for part in parts):
        raise ValueError("SMB remote path must stay within the selected share.")
    return "/".join(parts)
