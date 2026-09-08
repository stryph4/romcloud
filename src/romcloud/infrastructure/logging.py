"""Logging setup for ROMCloud.

Passwords and credentials must never appear in log output.
The logger name hierarchy is ``romcloud.*``.
"""

from __future__ import annotations

import logging
import logging.handlers
from pathlib import Path
from typing import Optional

from romcloud.infrastructure.diagnostics import (
    DEFAULT_FILENAME,
    SQLiteDiagnosticHandler,
    configure_diagnostics,
)

_LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
_DATE_FORMAT = "%Y-%m-%dT%H:%M:%S"


def configure_logging(
    level: str = "INFO",
    log_dir: Optional[str] = None,
    console: bool = True,
    diagnostic_db: Optional[str] = None,
) -> None:
    """Configure the root ``romcloud`` logger.

    Parameters
    ----------
    level:
        Standard Python log level name (DEBUG, INFO, WARNING, ERROR).
    log_dir:
        If provided, a rotating file handler is added pointing at
        ``{log_dir}/romcloud.log``.
    console:
        Whether to attach a StreamHandler (stdout).  Set False for
        background / daemon contexts.
    """
    root = logging.getLogger("romcloud")
    root.setLevel(level.upper())
    # Avoid duplicate handlers and release file/SQLite handles on reconfigure.
    for existing in tuple(root.handlers):
        root.removeHandler(existing)
        existing.close()

    formatter = logging.Formatter(_LOG_FORMAT, datefmt=_DATE_FORMAT)

    if console:
        handler = logging.StreamHandler()
        handler.setFormatter(formatter)
        root.addHandler(handler)

    if log_dir:
        log_path = Path(log_dir) / "romcloud.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        fh = logging.handlers.RotatingFileHandler(
            log_path,
            maxBytes=5 * 1024 * 1024,   # 5 MiB
            backupCount=3,
            encoding="utf-8",
        )
        fh.setFormatter(formatter)
        root.addHandler(fh)

    # The central DB is primary, while console/text handlers remain a useful
    # fail-open fallback. Derivation preserves compatibility for callers that
    # only know the conventional sibling ``logs`` directory.
    db_path = diagnostic_db
    if db_path is None and log_dir:
        db_path = str(Path(log_dir).parent / "data" / DEFAULT_FILENAME)
    if db_path:
        store = configure_diagnostics(db_path)
        if store is not None:
            root.addHandler(SQLiteDiagnosticHandler(store))


def get_logger(name: str) -> logging.Logger:
    """Return a child logger under the ``romcloud`` namespace."""
    return logging.getLogger(f"romcloud.{name}")
