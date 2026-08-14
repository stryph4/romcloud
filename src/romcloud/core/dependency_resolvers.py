"""Pure descriptor parsers used by dependency-aware caching."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Callable

from romcloud.core.cue_parser import parse_cue_references
from romcloud.core.exceptions import DependencyResolutionError


@dataclass(frozen=True)
class DependencyReference:
    path: str
    required: bool = True


Resolver = Callable[[str, str], list[DependencyReference]]


def resolve_m3u(_path: str, text: str) -> list[DependencyReference]:
    references: list[DependencyReference] = []
    for raw_line in text.lstrip("\ufeff").splitlines():
        line = raw_line.strip()
        if line and not line.startswith("#"):
            references.append(DependencyReference(line))
    if not references:
        raise DependencyResolutionError("M3U playlist contains no disc references")
    return references


def resolve_cue(path: str, text: str) -> list[DependencyReference]:
    parsed = parse_cue_references(text)
    if parsed.warnings:
        first = parsed.warnings[0]
        raise DependencyResolutionError(
            f"Malformed CUE dependency at line {first.line_number}: {first.reason}"
        )
    if not parsed.references:
        raise DependencyResolutionError(f"CUE descriptor has no FILE entries: {path}")
    return [DependencyReference(reference) for reference in parsed.references]


def resolve_xbox360(path: str, text: str) -> list[DependencyReference]:
    """Match Batocera configgen: the first line names the launch payload."""
    lines = text.splitlines()
    if not lines or not lines[0].strip():
        raise DependencyResolutionError(f"Xbox 360 marker is empty: {path}")
    return [DependencyReference(lines[0].strip())]


def resolve_ccd(path: str, _text: str) -> list[DependencyReference]:
    """CloneCD sets require the same-stem IMG; SUB is optional."""
    descriptor = PurePosixPath(path)
    return [
        DependencyReference(descriptor.with_suffix(".img").name),
        DependencyReference(descriptor.with_suffix(".sub").name, required=False),
    ]


_GDI_TRACK_RE = re.compile(
    r'^\s*\d+\s+\d+\s+\d+\s+\d+\s+(?:"(?P<quoted>[^"]+)"|(?P<bare>\S+))\s+\d+\s*$'
)


def resolve_gdi(path: str, text: str) -> list[DependencyReference]:
    lines = [line for line in text.lstrip("\ufeff").splitlines() if line.strip()]
    if not lines:
        raise DependencyResolutionError(f"GDI descriptor is empty: {path}")
    try:
        expected_tracks = int(lines[0].strip())
    except ValueError as exc:
        raise DependencyResolutionError(
            f"GDI descriptor has an invalid track count: {path}"
        ) from exc
    references: list[DependencyReference] = []
    for line_number, line in enumerate(lines[1:], start=2):
        match = _GDI_TRACK_RE.match(line)
        if match is None:
            raise DependencyResolutionError(
                f"Malformed GDI track at line {line_number}: {path}"
            )
        references.append(
            DependencyReference(match.group("quoted") or match.group("bare"))
        )
    if len(references) != expected_tracks:
        raise DependencyResolutionError(
            f"GDI track count mismatch in {path}: expected {expected_tracks}, "
            f"found {len(references)}"
        )
    return references


DEFAULT_RESOLVERS: dict[str, Resolver] = {
    ".m3u": resolve_m3u,
    ".cue": resolve_cue,
    ".xbox360": resolve_xbox360,
    ".ccd": resolve_ccd,
    ".gdi": resolve_gdi,
}

DESCRIPTOR_EXTENSIONS = frozenset(DEFAULT_RESOLVERS)
