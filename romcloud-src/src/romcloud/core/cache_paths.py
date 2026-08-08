"""Cache path construction — the single on-disk cache layout invariant.

Invariant
---------
``cached asset path = <cache_root>/<batocera_system>/<asset path relative to
that system's source root>``

- The original source basename is always preserved verbatim — assets are
  never renamed or hashed.
- Two systems (or two subdirectories within one system) may contain files
  with identical names without colliding, because the full relative path —
  including any subdirectories — is mirrored under the cache root.
- A malformed ``relative_path`` (absolute, empty, mismatched system, or
  containing ``..`` traversal segments) is rejected rather than silently
  resolved, so it can never write outside the cache root.

Both :class:`~romcloud.core.services.transfer.TransferService` (final and
``.partial`` staging paths) and
:class:`~romcloud.core.services.cache.CacheService` (placeholder cache path)
use this module so the on-disk layout stays consistent everywhere.
"""

from __future__ import annotations

from pathlib import Path, PurePosixPath

from romcloud.core.exceptions import CacheError


def _validate_system(system: str) -> None:
    if not system or system in (".", "..") or "/" in system or "\\" in system:
        raise CacheError(f"Invalid system name: {system!r}")


def system_relative_path(system: str, relative_path: str) -> PurePosixPath:
    """Strip the leading ``system`` segment from *relative_path*.

    ``relative_path`` is relative to the *source ROM root*, e.g.
    ``"ps2/Final Fantasy X.iso"`` for system ``"ps2"``.  The returned path is
    relative to that system's own source root, e.g. ``"Final Fantasy X.iso"``.

    Raises :class:`~romcloud.core.exceptions.CacheError` if *relative_path*
    is absolute, empty, does not belong to *system*, or contains a
    path-traversal (``..``) or empty segment.
    """
    _validate_system(system)

    normalised = relative_path.replace("\\", "/")
    raw = PurePosixPath(normalised)

    if raw.is_absolute() or normalised.startswith("/") or not raw.parts:
        raise CacheError(f"Invalid asset relative path: {relative_path!r}")

    if raw.parts[0] != system:
        raise CacheError(
            f"Asset relative path {relative_path!r} does not belong to system {system!r}"
        )

    remainder_parts = raw.parts[1:]
    if not remainder_parts:
        raise CacheError(f"Asset relative path has no filename: {relative_path!r}")

    if any(part in ("..", ".", "") for part in remainder_parts):
        raise CacheError(
            f"Path traversal detected in asset relative path: {relative_path!r}"
        )

    return PurePosixPath(*remainder_parts)


def resolve_cache_path(cache_root: Path | str, system: str, relative_path: str) -> Path:
    """Return the on-disk cache path for an asset, per the layout invariant.

    ``<cache_root>/<system>/<relative_path with the system prefix stripped>``
    """
    remainder = system_relative_path(system, relative_path)
    return Path(cache_root) / system / Path(*remainder.parts)
