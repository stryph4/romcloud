"""Cue-sheet dependency parsing — minimal, safe ``FILE`` reference extraction.

This is deliberately **not** a full cue-sheet parser.  ``TRACK``/``INDEX``/
``REM``/etc. lines are ignored entirely — the only thing ROMCloud needs is
the list of companion files a ``.cue`` requires so they can be cached
alongside it.

Format handled
--------------
::

    FILE "Game (Track 1).bin" BINARY
    FILE Game.bin BINARY

- Quoted filenames (with or without spaces) and bare/unquoted filenames are
  both supported.
- The trailing type token (``BINARY``/``WAVE``/``MP3``/``AIFF``/...) is
  required by the cue-sheet format but its value is never inspected.
- Malformed ``FILE`` lines (no filename, unterminated quote, missing type)
  are reported as warnings and skipped — never guessed at, never raised as
  a hard failure, so a single bad line in one game's cue never breaks
  catalog scanning for everything else.

Path resolution
---------------
Cue ``FILE`` references are always relative to the ``.cue`` file's own
directory (never the system root, never the cue's filename stem) — see
:func:`resolve_cue_dependencies`.  Any reference that would resolve outside
the owning Batocera system's source root (path traversal via ``..``) is
rejected individually; it never aborts parsing of the remaining references.
"""

from __future__ import annotations

import posixpath
import re
from dataclasses import dataclass, field

# Matches: FILE "quoted name" TYPE   or   FILE bare_name TYPE
_FILE_LINE_RE = re.compile(
    r'^\s*FILE\s+(?:"(?P<quoted>[^"]*)"|(?P<bare>\S+))\s+(?P<type>\S+)\s*$',
    re.IGNORECASE,
)


@dataclass(frozen=True)
class CueParseWarning:
    """A ``FILE`` line that could not be understood."""

    line_number: int
    line: str
    reason: str


@dataclass(frozen=True)
class CueParseResult:
    """Raw ``FILE`` reference extraction — no path resolution/validation."""

    references: list[str] = field(default_factory=list)
    """Filenames exactly as authored in the cue sheet, in file order."""
    warnings: list[CueParseWarning] = field(default_factory=list)


@dataclass(frozen=True)
class CueRejection:
    """A reference that was resolved but rejected (e.g. path traversal)."""

    raw_reference: str
    reason: str


@dataclass(frozen=True)
class CueDependency:
    """A single resolved, validated companion-file reference."""

    raw_reference: str
    """Filename exactly as authored in the cue sheet."""
    relative_path: str
    """Path relative to the *system* source root (posix separators),
    e.g. ``psx/Game (Track 1).bin`` — same convention as
    :class:`~romcloud.core.models.game.GameAsset.relative_path`."""


@dataclass(frozen=True)
class CueDependencyResult:
    """Fully resolved, validated set of a cue's companion-file dependencies."""

    dependencies: list[CueDependency] = field(default_factory=list)
    rejected: list[CueRejection] = field(default_factory=list)
    warnings: list[CueParseWarning] = field(default_factory=list)


def parse_cue_references(cue_text: str) -> CueParseResult:
    """Extract raw ``FILE`` reference filenames from cue-sheet text.

    Pure/no I/O.  Does not resolve paths or check existence.
    """
    references: list[str] = []
    warnings: list[CueParseWarning] = []

    for line_number, raw_line in enumerate(cue_text.splitlines(), start=1):
        stripped = raw_line.strip()
        if not stripped or not stripped.upper().startswith("FILE"):
            continue

        match = _FILE_LINE_RE.match(raw_line)
        if not match:
            warnings.append(
                CueParseWarning(line_number, raw_line, "malformed FILE line")
            )
            continue

        filename = match.group("quoted")
        if filename is None:
            filename = match.group("bare")
        if not filename:
            warnings.append(
                CueParseWarning(line_number, raw_line, "empty filename")
            )
            continue

        references.append(filename)

    return CueParseResult(references=references, warnings=warnings)


def resolve_cue_dependencies(
    cue_relative_path: str, cue_text: str
) -> CueDependencyResult:
    """Parse *cue_text* and resolve every ``FILE`` reference to a
    system-root-relative path.

    References are resolved relative to *cue_relative_path*'s own directory
    (never the system root directly, never the cue's filename stem) — this
    is what allows two different cue-based games to legally contain
    same-named companion files in different directories without colliding
    (see :mod:`romcloud.core.cache_paths`).

    A reference that normalises outside the cue's own system (i.e. a ``..``
    traversal escaping the system's source root) is reported in
    ``.rejected`` and never included in ``.dependencies`` — it is never
    silently guessed at or substituted.
    """
    parsed = parse_cue_references(cue_text)

    cue_path = posixpath.normpath(cue_relative_path.replace("\\", "/"))
    cue_parts = cue_path.split("/")
    if not cue_parts or not cue_parts[0]:
        # Cannot even determine the owning system — reject everything.
        rejected = [
            CueRejection(ref, "cue path has no system component")
            for ref in parsed.references
        ]
        return CueDependencyResult(rejected=rejected, warnings=parsed.warnings)

    system = cue_parts[0]
    cue_dir = "/".join(cue_parts[:-1]) or system

    dependencies: list[CueDependency] = []
    rejected: list[CueRejection] = []

    for raw_ref in parsed.references:
        normalised_ref = raw_ref.replace("\\", "/")
        candidate = posixpath.normpath(posixpath.join(cue_dir, normalised_ref))
        candidate_parts = [p for p in candidate.split("/") if p not in ("", ".")]

        if not candidate_parts or candidate_parts[0] != system or ".." in candidate_parts:
            rejected.append(
                CueRejection(raw_ref, "path traversal outside system source root")
            )
            continue

        dependencies.append(
            CueDependency(raw_reference=raw_ref, relative_path="/".join(candidate_parts))
        )

    return CueDependencyResult(
        dependencies=dependencies, rejected=rejected, warnings=parsed.warnings
    )
