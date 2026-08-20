"""Safe provider-neutral directory browsing helpers for setup UIs."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from romcloud.core.exceptions import ProviderError


@dataclass(frozen=True)
class DirectoryItem:
    name: str
    path: str
    is_directory: bool

    def as_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "path": self.path,
            "is_directory": self.is_directory,
        }


def normalize_remote_directory(path: str) -> str:
    """Return a share-relative POSIX path and reject traversal/control data."""
    raw = str(path or "").replace("\\", "/").strip("/")
    if "\x00" in raw or "\n" in raw or "\r" in raw:
        raise ValueError("Remote directory contains invalid control characters.")
    if not raw:
        return ""
    parts = PurePosixPath(raw).parts
    if any(part in ("", ".", "..") for part in parts):
        raise ValueError("Remote directory must stay within the selected share.")
    return "/".join(parts)


def remote_parent(path: str) -> str:
    normalized = normalize_remote_directory(path)
    if not normalized:
        return ""
    parent = PurePosixPath(normalized).parent
    return "" if str(parent) == "." else str(parent)


def join_remote_directory(parent: str, name: str) -> str:
    if "/" in name or "\\" in name or name in ("", ".", ".."):
        raise ValueError("Directory name is not a valid single path component.")
    return normalize_remote_directory(str(PurePosixPath(parent) / name))


def normalize_sftp_directory(path: str) -> str:
    """Return an absolute POSIX SFTP path without protocol/host data.

    SFTP paths are paths as seen by the authenticated account (including a
    chrooted ``/``), never URLs. Case is deliberately preserved.
    """
    raw = str(path or "").strip().replace("\\", "/")
    lowered = raw.casefold()
    if "://" in raw or raw.startswith("//") or (
        not raw.startswith("/") and ":/" in raw
    ):
        raise ValueError(
            "Enter only an absolute SFTP path such as /Roms, without "
            "sftp:// or a server name."
        )
    if "\x00" in raw or "\n" in raw or "\r" in raw:
        raise ValueError("SFTP path contains invalid control characters.")
    if not raw.startswith("/"):
        raise ValueError("SFTP path must be an absolute POSIX path such as /Roms.")
    if lowered.startswith(("/sftp:", "/ssh:")):
        raise ValueError(
            "Enter only an absolute SFTP path such as /Roms, without "
            "sftp:// or a server name."
        )
    parts = PurePosixPath(raw).parts
    if any(part in (".", "..") for part in parts):
        raise ValueError("SFTP path must stay within the server-visible root.")
    components = [part for part in parts if part not in ("/", "")]
    return "/" + "/".join(components) if components else "/"


def sftp_parent(path: str, *, boundary: str = "/") -> str:
    """Return a parent path without crossing the configured browse root."""
    current = normalize_sftp_directory(path)
    root = normalize_sftp_directory(boundary)
    if current != root and not current.startswith(root.rstrip("/") + "/"):
        raise ValueError("SFTP path is outside the configured browse root.")
    if current == root:
        return root
    parent = str(PurePosixPath(current).parent)
    return root if parent == "." or len(parent) < len(root) else parent


def browse_local_directory(path: str) -> dict[str, object]:
    current = Path(path).expanduser()
    if not current.is_absolute():
        raise ValueError("Local directory must be an absolute path.")
    current = current.resolve()
    if not current.is_dir():
        raise ProviderError(f"Local folder is not accessible: {current}")
    entries: list[DirectoryItem] = []
    try:
        for child in sorted(current.iterdir(), key=lambda item: item.name.casefold()):
            if child.name.startswith("."):
                continue
            entries.append(
                DirectoryItem(child.name, str(child), child.is_dir())
            )
    except OSError as exc:
        raise ProviderError(f"Could not read local folder {current}: {exc}") from exc
    return {
        "path": str(current),
        "parent": str(current.parent) if current.parent != current else "",
        "entries": [entry.as_dict() for entry in entries],
    }
