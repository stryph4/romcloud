"""Logical-container adapter for PCSX2 Folder Memory Cards.

The adapter only traverses ordinary host filesystem entries below a directory
that contains PCSX2's ``_pcsx2_superblock`` marker.  It does not parse or write
monolithic ``.ps2`` card images.
"""

from __future__ import annotations

import hashlib
import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Protocol

from romcloud.core.save_containers import (
    CandidateReceipt,
    ContainerEntry,
    ContainerSnapshot,
    EntryIdentity,
    EntryReplacement,
    ExtractedEntry,
    OpaqueContainerError,
    OpaqueReason,
    ProbeResult,
    RebuildPlan,
    ValidationReport,
)

PCSX2_FOLDER_CARD_MARKER = "_pcsx2_superblock"


class Ps2FolderGroupingPolicy(Protocol):
    policy_id: str
    schema_version: int

    def group(self, top_level_names: tuple[bytes, ...]) -> Mapping[bytes, str]: ...


class ConservativePs2FolderGroupingPolicy:
    """Proves only an empty or single-structural-entry card.

    Current PCSX2 GameDB filters can associate multiple top-level directories
    with one title.  Without a complete authoritative mapping, treating two
    names as independent would be unsafe, so this policy rejects that shape.
    """

    policy_id = "pcsx2-folder-single-entry"
    schema_version = 1

    def group(self, top_level_names: tuple[bytes, ...]) -> Mapping[bytes, str]:
        if len(top_level_names) > 1:
            raise OpaqueContainerError(OpaqueReason.AMBIGUOUS_PS2_FOLDER_GROUPING)
        return {name: "pcsx2-save:card" for name in top_level_names}


@dataclass(frozen=True)
class ExplicitPs2FolderGroupingPolicy:
    """Versioned complete mapping supplied by a trustworthy data source.

    This class is the injection seam for a future PCSX2-maintained GameDB
    importer.  A partial mapping always fails closed.
    """

    groups: Mapping[bytes, str]
    policy_id: str = "pcsx2-folder-explicit"
    schema_version: int = 1

    def group(self, top_level_names: tuple[bytes, ...]) -> Mapping[bytes, str]:
        if any(name not in self.groups for name in top_level_names):
            raise OpaqueContainerError(OpaqueReason.AMBIGUOUS_PS2_FOLDER_GROUPING)
        selected = {name: self.groups[name] for name in top_level_names}
        if any(not value for value in selected.values()):
            raise OpaqueContainerError(OpaqueReason.AMBIGUOUS_PS2_FOLDER_GROUPING)
        return selected


@dataclass(frozen=True)
class _FolderEntryPayload:
    source_root: Path
    top_name: bytes


def _safe_children(path: Path) -> tuple[Path, ...]:
    try:
        children = tuple(sorted(path.iterdir(), key=lambda value: os.fsencode(value.name)))
    except OSError as exc:
        raise OpaqueContainerError(OpaqueReason.SYMLINK_OR_PATH_SUBSTITUTION) from exc
    for child in children:
        if child.is_symlink():
            raise OpaqueContainerError(OpaqueReason.SYMLINK_OR_PATH_SUBSTITUTION)
        if not child.is_dir() and not child.is_file():
            raise OpaqueContainerError(OpaqueReason.UNSUPPORTED_FORMAT)
    return children


def _top_directories(root: Path) -> tuple[Path, ...]:
    marker = root / PCSX2_FOLDER_CARD_MARKER
    if root.is_symlink() or not root.is_dir():
        raise OpaqueContainerError(OpaqueReason.UNSUPPORTED_FORMAT)
    if marker.is_symlink() or not marker.is_file():
        raise OpaqueContainerError(OpaqueReason.UNSUPPORTED_FORMAT)
    return tuple(
        child
        for child in _safe_children(root)
        if child.name != PCSX2_FOLDER_CARD_MARKER and child.is_dir()
    )


def _tree_records(entry_root: Path) -> tuple[tuple[bytes, str, bytes], ...]:
    """Return deterministic, host-metadata-neutral records for one save tree."""

    records: list[tuple[bytes, str, bytes]] = []
    stack: list[tuple[Path, tuple[bytes, ...]]] = [(entry_root, ())]
    while stack:
        current, relative = stack.pop()
        if current.is_symlink() or not current.is_dir():
            raise OpaqueContainerError(OpaqueReason.SYMLINK_OR_PATH_SUBSTITUTION)
        if relative:
            records.append((b"/".join(relative), "d", b""))
        children = _safe_children(current)
        directories: list[tuple[Path, tuple[bytes, ...]]] = []
        for child in children:
            name = os.fsencode(child.name)
            if name in {b"", b".", b".."} or b"/" in name or b"\x00" in name:
                raise OpaqueContainerError(OpaqueReason.SYMLINK_OR_PATH_SUBSTITUTION)
            child_relative = (*relative, name)
            if child.is_dir():
                directories.append((child, child_relative))
                continue
            try:
                content = child.read_bytes()
            except OSError as exc:
                raise OpaqueContainerError(
                    OpaqueReason.SYMLINK_OR_PATH_SUBSTITUTION
                ) from exc
            records.append((b"/".join(child_relative), "f", content))
        stack.extend(reversed(directories))
    return tuple(sorted(records, key=lambda item: (item[0], item[1])))


def _entry_hash(records: tuple[tuple[bytes, str, bytes], ...]) -> tuple[str, int]:
    digest = hashlib.sha256()
    total = 0
    for relative, kind, content in records:
        digest.update(kind.encode("ascii"))
        digest.update(len(relative).to_bytes(4, "little"))
        digest.update(relative)
        if kind == "f":
            digest.update(len(content).to_bytes(8, "little"))
            digest.update(content)
            total += len(content)
    return digest.hexdigest(), total


def _physical_tree_hash(root: Path) -> str:
    digest = hashlib.sha256()
    stack: list[tuple[Path, tuple[bytes, ...]]] = [(root, ())]
    while stack:
        current, relative = stack.pop()
        children = _safe_children(current)
        directories: list[tuple[Path, tuple[bytes, ...]]] = []
        for child in children:
            child_relative = (*relative, os.fsencode(child.name))
            encoded = b"/".join(child_relative)
            if child.is_dir():
                digest.update(b"d" + len(encoded).to_bytes(4, "little") + encoded)
                directories.append((child, child_relative))
            else:
                content = child.read_bytes()
                digest.update(b"f" + len(encoded).to_bytes(4, "little") + encoded)
                digest.update(len(content).to_bytes(8, "little") + content)
        stack.extend(reversed(directories))
    return digest.hexdigest()


def _find_top(root: Path, identity: EntryIdentity) -> Path:
    for child in _top_directories(root):
        if os.fsencode(child.name).hex() == identity.value:
            return child
    raise OpaqueContainerError(OpaqueReason.SOURCE_CHANGED_DURING_STAGING)


def _copy_tree(source: Path, destination: Path) -> None:
    destination.mkdir()
    stack: list[tuple[Path, Path]] = [(source, destination)]
    while stack:
        source_dir, destination_dir = stack.pop()
        for child in _safe_children(source_dir):
            target = destination_dir / child.name
            if child.is_dir():
                target.mkdir()
                stack.append((child, target))
            else:
                with child.open("rb") as reader, target.open("xb") as writer:
                    shutil.copyfileobj(reader, writer)


class Pcsx2FolderMemoryCardAdapter:
    adapter_id = "pcsx2-folder-memory-card"
    schema_version = 1

    def __init__(
        self, grouping_policy: Ps2FolderGroupingPolicy | None = None
    ) -> None:
        self.grouping_policy = (
            grouping_policy or ConservativePs2FolderGroupingPolicy()
        )

    @property
    def effective_schema_version(self) -> int:
        # Grouping semantics are part of canonicalization compatibility.
        return self.schema_version * 1000 + self.grouping_policy.schema_version

    @property
    def format_variant(self) -> str:
        return f"pcsx2-folder-card:{self.grouping_policy.policy_id}"

    def probe(self, source: Path) -> ProbeResult:
        try:
            self.snapshot(source, container_id="probe")
        except OpaqueContainerError as exc:
            return ProbeResult(False, opaque_reason=exc.reason)
        return ProbeResult(True, format_variant=self.format_variant)

    def snapshot(self, source: Path, *, container_id: str) -> ContainerSnapshot:
        root = Path(source)
        top = _top_directories(root)
        recorded = tuple(
            (path, _tree_records(path))
            for path in top
        )
        # PCSX2 structural saves contain host files. Empty directory residue
        # has no save payload and the existing file transaction model may
        # legitimately leave such a parent behind after deleting its last
        # selected file.
        recorded = tuple(
            (path, records)
            for path, records in recorded
            if any(kind == "f" for _, kind, _ in records)
        )
        names = tuple(sorted((os.fsencode(path.name) for path, _ in recorded)))
        groups = self.grouping_policy.group(names)
        entries: list[ContainerEntry] = []
        for path, records in recorded:
            name = os.fsencode(path.name)
            canonical_hash, size_bytes = _entry_hash(records)
            entries.append(
                ContainerEntry(
                    identity=EntryIdentity(name.hex()),
                    merge_domain_id=groups[name],
                    canonical_hash=canonical_hash,
                    size_bytes=size_bytes,
                    locator=name.hex(),
                )
            )
        return ContainerSnapshot(
            container_id=container_id,
            adapter_id=self.adapter_id,
            schema_version=self.effective_schema_version,
            format_variant=self.format_variant,
            entries=tuple(sorted(entries, key=lambda entry: entry.identity.value)),
        )

    def enumerate_entries(
        self, snapshot: ContainerSnapshot
    ) -> tuple[ContainerEntry, ...]:
        return snapshot.entries

    def identify_entry(self, entry: ContainerEntry) -> EntryIdentity:
        return entry.identity

    def hash_entry(self, entry: ContainerEntry) -> str:
        return entry.canonical_hash

    def extract_entry(
        self,
        source: Path,
        snapshot: ContainerSnapshot,
        identity: EntryIdentity,
    ) -> ExtractedEntry:
        top = _find_top(Path(source), identity)
        current = self.snapshot(source, container_id=snapshot.container_id)
        entry = next((item for item in current.entries if item.identity == identity), None)
        expected = next((item for item in snapshot.entries if item.identity == identity), None)
        if entry is None or expected is None or entry.canonical_hash != expected.canonical_hash:
            raise OpaqueContainerError(OpaqueReason.SOURCE_CHANGED_DURING_STAGING)
        return ExtractedEntry(
            identity=identity,
            canonical_hash=entry.canonical_hash,
            payload=_FolderEntryPayload(Path(source), os.fsencode(top.name)),
        )

    def replace_entry(self, candidate: Path, replacement: EntryReplacement) -> None:
        payload = replacement.extracted.payload
        if not isinstance(payload, _FolderEntryPayload):
            raise OpaqueContainerError(OpaqueReason.VALIDATION_FAILURE)
        existing = None
        for child in _top_directories(candidate):
            if os.fsencode(child.name).hex() == replacement.entry.identity.value:
                existing = child
                break
        if existing is not None:
            shutil.rmtree(existing)
        source = _find_top(payload.source_root, replacement.entry.identity)
        target_name = os.fsdecode(payload.top_name)
        target = Path(candidate) / target_name
        if target.parent != Path(candidate) or target.exists():
            raise OpaqueContainerError(OpaqueReason.SYMLINK_OR_PATH_SUBSTITUTION)
        _copy_tree(source, target)

    def remove_entry(self, candidate: Path, identity: EntryIdentity) -> None:
        try:
            target = _find_top(Path(candidate), identity)
        except OpaqueContainerError as exc:
            if exc.reason is OpaqueReason.SOURCE_CHANGED_DURING_STAGING:
                return
            raise
        shutil.rmtree(target)

    def rebuild(
        self, destination: Path, plan: RebuildPlan, candidate: Path
    ) -> CandidateReceipt:
        # Validate before copying so shutil never follows a source symlink.
        self.snapshot(destination, container_id=plan.expected.container_id)
        candidate = Path(candidate)
        if candidate.exists():
            raise OpaqueContainerError(OpaqueReason.VALIDATION_FAILURE)
        _copy_tree(Path(destination), candidate)
        for identity in sorted(plan.removals):
            self.remove_entry(candidate, identity)
        for replacement in sorted(
            plan.replacements, key=lambda value: value.entry.identity.value
        ):
            self.replace_entry(candidate, replacement)
        report = self.validate(candidate, plan.expected)
        if not report.valid:
            shutil.rmtree(candidate, ignore_errors=True)
            raise OpaqueContainerError(
                report.opaque_reason or OpaqueReason.VALIDATION_FAILURE,
                report.detail,
            )
        return CandidateReceipt(
            candidate_path=candidate,
            physical_hash=_physical_tree_hash(candidate),
            expected=plan.expected,
        )

    def validate(
        self, candidate: Path, expected: ContainerSnapshot
    ) -> ValidationReport:
        try:
            actual = self.snapshot(candidate, container_id=expected.container_id)
        except OpaqueContainerError as exc:
            return ValidationReport(False, opaque_reason=exc.reason, detail=exc.detail)
        expected_content = tuple(
            (entry.identity, entry.merge_domain_id, entry.canonical_hash, entry.size_bytes)
            for entry in expected.entries
        )
        actual_content = tuple(
            (entry.identity, entry.merge_domain_id, entry.canonical_hash, entry.size_bytes)
            for entry in actual.entries
        )
        if expected_content != actual_content:
            return ValidationReport(
                False,
                snapshot=actual,
                opaque_reason=OpaqueReason.VALIDATION_FAILURE,
                detail="candidate logical snapshot differs from plan",
            )
        return ValidationReport(True, snapshot=actual)
