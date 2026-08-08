"""Atomic file writes.

Writes content to a temporary file in the same directory as the target,
then renames it into place with :func:`os.replace` — which is atomic on
POSIX filesystems (the destination either has its old content or its fully
new content; never a partial write). Used anywhere a file must never be
left half-written if the process crashes or is killed mid-write (e.g.
``romcloud.toml``, credentials files).
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Optional


def atomic_write_text(path: Path, content: str, *, mode: Optional[int] = None) -> None:
    """Atomically write *content* to *path*.

    If *mode* is given, the temp file is created with exactly those
    permission bits from the start (rather than writing then chmod'ing),
    so the final file is never briefly world/group-readable.
    """
    path.parent.mkdir(parents=True, exist_ok=True)

    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    tmp_path = Path(tmp_name)
    try:
        if mode is not None:
            os.chmod(tmp_path, mode)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(content)
        os.replace(tmp_path, path)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise
