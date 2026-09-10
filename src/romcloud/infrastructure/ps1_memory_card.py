"""Strict adapter for exact 128 KiB raw PlayStation memory cards.

This is intentionally a small independent implementation of the documented
PS1 card directory format.  It does not attempt to recognize wrappers,
nonstandard cards, or broken-sector replacement maps.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path

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

CARD_SIZE = 128 * 1024
BLOCK_SIZE = 8192
FRAME_SIZE = 128
BLOCK_COUNT = 16
DATA_BLOCKS = tuple(range(1, BLOCK_COUNT))
FILENAME_OFFSET = 0x0A
FILENAME_SIZE = 20

_LIVE_HEADER = 0x51
_LIVE_MIDDLE = 0x52
_LIVE_END = 0x53
_FREE = 0xA0
_DELETED_HEADER = 0xA1
_DELETED_MIDDLE = 0xA2
_DELETED_END = 0xA3
_KNOWN_STATES = frozenset(
    {
        _LIVE_HEADER,
        _LIVE_MIDDLE,
        _LIVE_END,
        _FREE,
        _DELETED_HEADER,
        _DELETED_MIDDLE,
        _DELETED_END,
    }
)


@dataclass(frozen=True)
class _ParsedEntry:
    identity: EntryIdentity
    merge_domain_id: str
    filename: bytes
    blocks: tuple[int, ...]
    frames: tuple[bytes, ...]
    data_blocks: tuple[bytes, ...]
    canonical_hash: str

    @property
    def size_bytes(self) -> int:
        return len(self.blocks) * BLOCK_SIZE


@dataclass(frozen=True)
class _ParsedCard:
    raw: bytes
    live: tuple[_ParsedEntry, ...]
    deleted_blocks: frozenset[int]
    free_blocks: frozenset[int]


@dataclass(frozen=True)
class _ExtractedPs1Entry:
    filename: bytes
    frames: tuple[bytes, ...]
    data_blocks: tuple[bytes, ...]


def _xor_checksum(frame: bytes | bytearray) -> int:
    value = 0
    for byte in frame:
        value ^= byte
    return value


def _set_checksum(frame: bytearray) -> None:
    frame[0x7F] = _xor_checksum(frame[:0x7F])


def _directory_frame(raw: bytes | bytearray, block: int) -> bytes:
    start = block * FRAME_SIZE
    return bytes(raw[start : start + FRAME_SIZE])


def _state(frame: bytes) -> int:
    if frame[1:4] != b"\x00\x00\x00" or frame[0] not in _KNOWN_STATES:
        raise OpaqueContainerError(OpaqueReason.MALFORMED_CHAIN, "invalid directory state")
    return frame[0]


def _next_block(frame: bytes) -> int | None:
    encoded = int.from_bytes(frame[8:10], "little")
    if encoded == 0xFFFF:
        return None
    block = encoded + 1
    if block not in DATA_BLOCKS:
        raise OpaqueContainerError(OpaqueReason.MALFORMED_CHAIN, "invalid next pointer")
    return block


def _filename(frame: bytes) -> bytes:
    field = frame[FILENAME_OFFSET : FILENAME_OFFSET + FILENAME_SIZE]
    value, separator, padding = field.partition(b"\x00")
    if not value:
        raise OpaqueContainerError(OpaqueReason.AMBIGUOUS_NAMESPACE, "empty filename")
    if separator and any(padding):
        raise OpaqueContainerError(
            OpaqueReason.AMBIGUOUS_NAMESPACE, "nonzero filename padding"
        )
    return value


def _commercial_namespace(filename: bytes) -> bytes:
    if len(filename) < 12 or filename[:2] not in {b"BA", b"BE", b"BI"}:
        raise OpaqueContainerError(OpaqueReason.AMBIGUOUS_NAMESPACE)
    code = filename[2:12]
    if (
        len(code) != 10
        or code[4:5] != b"-"
        or not all(65 <= value <= 90 for value in code[:4])
        or not all(48 <= value <= 57 for value in code[5:])
    ):
        raise OpaqueContainerError(OpaqueReason.AMBIGUOUS_NAMESPACE)
    return filename[:12]


def _canonical_hash(
    filename: bytes, frames: tuple[bytes, ...], data_blocks: tuple[bytes, ...]
) -> str:
    digest = hashlib.sha256()
    digest.update(len(filename).to_bytes(2, "little"))
    digest.update(filename)
    digest.update(len(frames).to_bytes(2, "little"))
    for index, source in enumerate(frames):
        frame = bytearray(source)
        frame[0:4] = bytes((
            _LIVE_HEADER
            if index == 0
            else (_LIVE_END if index == len(frames) - 1 else _LIVE_MIDDLE),
            0,
            0,
            0,
        ))
        # Canonical pointers describe logical order, not physical allocation.
        frame[8:10] = (
            b"\xff\xff"
            if index == len(frames) - 1
            else index.to_bytes(2, "little")
        )
        _set_checksum(frame)
        digest.update(frame)
    for block in data_blocks:
        digest.update(block)
    return digest.hexdigest()


def _parse_chain(
    raw: bytes,
    frames_by_block: dict[int, bytes],
    start: int,
    *,
    deleted: bool,
) -> tuple[int, ...]:
    header_state = _DELETED_HEADER if deleted else _LIVE_HEADER
    middle_state = _DELETED_MIDDLE if deleted else _LIVE_MIDDLE
    end_state = _DELETED_END if deleted else _LIVE_END
    if _state(frames_by_block[start]) != header_state:
        raise OpaqueContainerError(OpaqueReason.MALFORMED_CHAIN)
    chain: list[int] = []
    current: int | None = start
    while current is not None:
        if current in chain:
            raise OpaqueContainerError(OpaqueReason.MALFORMED_CHAIN, "directory loop")
        chain.append(current)
        frame = frames_by_block[current]
        next_block = _next_block(frame)
        current_state = _state(frame)
        if len(chain) == 1:
            if current_state != header_state:
                raise OpaqueContainerError(OpaqueReason.MALFORMED_CHAIN)
        elif next_block is None:
            if current_state != end_state:
                raise OpaqueContainerError(OpaqueReason.MALFORMED_CHAIN)
        elif current_state != middle_state:
            raise OpaqueContainerError(OpaqueReason.MALFORMED_CHAIN)
        current = next_block
    return tuple(chain)


def _parse(raw: bytes) -> _ParsedCard:
    if len(raw) != CARD_SIZE:
        raise OpaqueContainerError(OpaqueReason.UNSUPPORTED_PS1_CARD_SIZE)
    header = raw[:FRAME_SIZE]
    if header[:2] != b"MC":
        raise OpaqueContainerError(OpaqueReason.UNSUPPORTED_FORMAT)
    if _xor_checksum(header) != 0:
        raise OpaqueContainerError(OpaqueReason.CHECKSUM_FAILURE, "header")

    frames = {block: _directory_frame(raw, block) for block in DATA_BLOCKS}
    for block, frame in frames.items():
        if _xor_checksum(frame) != 0:
            raise OpaqueContainerError(
                OpaqueReason.CHECKSUM_FAILURE, f"directory-block-{block}"
            )
        _state(frame)

    # Frames 16..35 in block zero are the replacement-sector table.  A first
    # word other than all-ones means the card uses a mapping this adapter does
    # not implement.
    for frame_index in range(16, 36):
        start = frame_index * FRAME_SIZE
        if raw[start : start + 4] != b"\xff\xff\xff\xff":
            raise OpaqueContainerError(OpaqueReason.PS1_BROKEN_SECTOR_MAPPING)

    consumed: set[int] = set()
    live_entries: list[_ParsedEntry] = []
    identities: set[EntryIdentity] = set()
    for block in DATA_BLOCKS:
        if _state(frames[block]) != _LIVE_HEADER:
            continue
        chain = _parse_chain(raw, frames, block, deleted=False)
        if consumed.intersection(chain):
            raise OpaqueContainerError(OpaqueReason.MALFORMED_CHAIN, "cross-linked chain")
        consumed.update(chain)
        first = frames[block]
        size = int.from_bytes(first[4:8], "little")
        if size != len(chain) * BLOCK_SIZE:
            raise OpaqueContainerError(OpaqueReason.MALFORMED_CHAIN, "invalid file size")
        filename = _filename(first)
        identity = EntryIdentity(filename.hex())
        if identity in identities:
            raise OpaqueContainerError(OpaqueReason.DUPLICATE_IDENTITY)
        identities.add(identity)
        namespace = _commercial_namespace(filename)
        entry_frames = tuple(frames[value] for value in chain)
        data_blocks = tuple(
            raw[value * BLOCK_SIZE : (value + 1) * BLOCK_SIZE] for value in chain
        )
        live_entries.append(
            _ParsedEntry(
                identity=identity,
                merge_domain_id=f"ps1-game:{namespace.hex()}",
                filename=filename,
                blocks=chain,
                frames=entry_frames,
                data_blocks=data_blocks,
                canonical_hash=_canonical_hash(filename, entry_frames, data_blocks),
            )
        )

    deleted_blocks: set[int] = set()
    for block in DATA_BLOCKS:
        if _state(frames[block]) != _DELETED_HEADER:
            continue
        chain = _parse_chain(raw, frames, block, deleted=True)
        if consumed.intersection(chain) or deleted_blocks.intersection(chain):
            raise OpaqueContainerError(OpaqueReason.MALFORMED_CHAIN, "overlapping allocation")
        deleted_blocks.update(chain)

    continuation_states = {
        _LIVE_MIDDLE,
        _LIVE_END,
        _DELETED_MIDDLE,
        _DELETED_END,
    }
    for block in DATA_BLOCKS:
        state = _state(frames[block])
        if state in continuation_states and block not in consumed and block not in deleted_blocks:
            raise OpaqueContainerError(OpaqueReason.MALFORMED_CHAIN, "orphan continuation")
        if state == _FREE and _next_block(frames[block]) is not None:
            raise OpaqueContainerError(OpaqueReason.MALFORMED_CHAIN, "linked free block")

    free_blocks = frozenset(
        block for block in DATA_BLOCKS if _state(frames[block]) == _FREE
    )
    return _ParsedCard(
        raw=raw,
        live=tuple(sorted(live_entries, key=lambda item: item.identity.value)),
        deleted_blocks=frozenset(deleted_blocks),
        free_blocks=free_blocks,
    )


def _snapshot(parsed: _ParsedCard, *, container_id: str) -> ContainerSnapshot:
    return ContainerSnapshot(
        container_id=container_id,
        adapter_id=Ps1RawMemoryCardAdapter.adapter_id,
        schema_version=Ps1RawMemoryCardAdapter.schema_version,
        format_variant="ps1-raw-128k",
        entries=tuple(
            ContainerEntry(
                identity=entry.identity,
                merge_domain_id=entry.merge_domain_id,
                canonical_hash=entry.canonical_hash,
                size_bytes=entry.size_bytes,
                locator=str(entry.blocks[0]),
            )
            for entry in parsed.live
        ),
    )


def _write_chain(
    raw: bytearray,
    blocks: tuple[int, ...],
    payload: _ExtractedPs1Entry,
) -> None:
    if len(blocks) != len(payload.data_blocks):
        raise OpaqueContainerError(OpaqueReason.VALIDATION_FAILURE)
    for index, block in enumerate(blocks):
        frame = bytearray(payload.frames[index])
        state = (
            _LIVE_HEADER
            if index == 0
            else (_LIVE_END if index == len(blocks) - 1 else _LIVE_MIDDLE)
        )
        frame[0:4] = bytes((state, 0, 0, 0))
        frame[8:10] = (
            b"\xff\xff"
            if index == len(blocks) - 1
            else (blocks[index + 1] - 1).to_bytes(2, "little")
        )
        _set_checksum(frame)
        frame_start = block * FRAME_SIZE
        raw[frame_start : frame_start + FRAME_SIZE] = frame
        data_start = block * BLOCK_SIZE
        raw[data_start : data_start + BLOCK_SIZE] = payload.data_blocks[index]


def _mark_deleted(raw: bytearray, blocks: tuple[int, ...]) -> None:
    for index, block in enumerate(blocks):
        frame_start = block * FRAME_SIZE
        frame = bytearray(raw[frame_start : frame_start + FRAME_SIZE])
        state = (
            _DELETED_HEADER
            if index == 0
            else (_DELETED_END if index == len(blocks) - 1 else _DELETED_MIDDLE)
        )
        frame[0:4] = bytes((state, 0, 0, 0))
        frame[8:10] = (
            b"\xff\xff"
            if index == len(blocks) - 1
            else (blocks[index + 1] - 1).to_bytes(2, "little")
        )
        _set_checksum(frame)
        raw[frame_start : frame_start + FRAME_SIZE] = frame


class Ps1RawMemoryCardAdapter:
    adapter_id = "ps1-raw-memory-card"
    schema_version = 1

    def probe(self, source: Path) -> ProbeResult:
        try:
            _parse(Path(source).read_bytes())
        except OpaqueContainerError as exc:
            return ProbeResult(False, opaque_reason=exc.reason)
        except OSError:
            return ProbeResult(False, opaque_reason=OpaqueReason.UNSUPPORTED_FORMAT)
        return ProbeResult(True, format_variant="ps1-raw-128k")

    def snapshot(self, source: Path, *, container_id: str) -> ContainerSnapshot:
        return _snapshot(_parse(Path(source).read_bytes()), container_id=container_id)

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
        parsed = _parse(Path(source).read_bytes())
        for entry in parsed.live:
            if entry.identity == identity:
                if not any(
                    value.identity == identity and value.canonical_hash == entry.canonical_hash
                    for value in snapshot.entries
                ):
                    raise OpaqueContainerError(OpaqueReason.SOURCE_CHANGED_DURING_STAGING)
                return ExtractedEntry(
                    identity=identity,
                    canonical_hash=entry.canonical_hash,
                    payload=_ExtractedPs1Entry(
                        filename=entry.filename,
                        frames=entry.frames,
                        data_blocks=entry.data_blocks,
                    ),
                )
        raise OpaqueContainerError(OpaqueReason.SOURCE_CHANGED_DURING_STAGING)

    def replace_entry(self, candidate: Path, replacement: EntryReplacement) -> None:
        candidate = Path(candidate)
        current = self.snapshot(candidate, container_id="staged-card")
        entries = {
            entry.identity: entry for entry in current.entries
        }
        entries[replacement.entry.identity] = replacement.entry
        expected = ContainerSnapshot(
            container_id=current.container_id,
            adapter_id=current.adapter_id,
            schema_version=current.schema_version,
            format_variant=current.format_variant,
            entries=tuple(entries[key] for key in sorted(entries)),
        )
        temporary = candidate.with_name(f".{candidate.name}.{uuid.uuid4().hex}.tmp")
        try:
            self.rebuild(
                candidate,
                RebuildPlan(expected, replacements=(replacement,)),
                temporary,
            )
            os.replace(temporary, candidate)
        finally:
            temporary.unlink(missing_ok=True)

    def remove_entry(self, candidate: Path, identity: EntryIdentity) -> None:
        candidate = Path(candidate)
        current = self.snapshot(candidate, container_id="staged-card")
        expected = replace_snapshot_entries(
            current,
            tuple(entry for entry in current.entries if entry.identity != identity),
        )
        temporary = candidate.with_name(f".{candidate.name}.{uuid.uuid4().hex}.tmp")
        try:
            self.rebuild(
                candidate,
                RebuildPlan(expected, removals=(identity,)),
                temporary,
            )
            os.replace(temporary, candidate)
        finally:
            temporary.unlink(missing_ok=True)

    def rebuild(
        self, destination: Path, plan: RebuildPlan, candidate: Path
    ) -> CandidateReceipt:
        parsed = _parse(Path(destination).read_bytes())
        existing = {entry.identity: entry for entry in parsed.live}
        expected = {entry.identity: entry for entry in plan.expected.entries}
        replacements = {item.entry.identity: item for item in plan.replacements}
        if set(replacements).difference(expected):
            raise OpaqueContainerError(OpaqueReason.VALIDATION_FAILURE)

        raw = bytearray(parsed.raw)
        original_free = list(sorted(parsed.free_blocks))
        removals = set(plan.removals).union(set(existing).difference(expected))
        for identity in sorted(removals):
            old = existing.get(identity)
            if old is not None:
                _mark_deleted(raw, old.blocks)

        for identity in sorted(replacements):
            replacement = replacements[identity]
            payload = replacement.extracted.payload
            if not isinstance(payload, _ExtractedPs1Entry):
                raise OpaqueContainerError(OpaqueReason.VALIDATION_FAILURE)
            if replacement.extracted.canonical_hash != replacement.entry.canonical_hash:
                raise OpaqueContainerError(OpaqueReason.SOURCE_CHANGED_DURING_STAGING)
            old = existing.get(identity)
            required = len(payload.data_blocks)
            reusable = list(old.blocks[:required]) if old is not None else []
            needed = required - len(reusable)
            if needed > len(original_free):
                raise OpaqueContainerError(
                    OpaqueReason.INSUFFICIENT_VERIFIED_FREE_CAPACITY
                )
            allocated = tuple((*reusable, *original_free[:needed]))
            del original_free[:needed]
            if old is not None and len(old.blocks) > required:
                _mark_deleted(raw, old.blocks[required:])
            _write_chain(raw, allocated, payload)

        candidate = Path(candidate)
        candidate.parent.mkdir(parents=True, exist_ok=True)
        candidate.write_bytes(raw)
        report = self.validate(candidate, plan.expected)
        if not report.valid:
            candidate.unlink(missing_ok=True)
            raise OpaqueContainerError(
                report.opaque_reason or OpaqueReason.VALIDATION_FAILURE,
                report.detail,
            )
        physical_hash = hashlib.sha256(raw).hexdigest()
        return CandidateReceipt(candidate, physical_hash, plan.expected)

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
        if actual_content != expected_content:
            return ValidationReport(
                False,
                snapshot=actual,
                opaque_reason=OpaqueReason.VALIDATION_FAILURE,
                detail="candidate logical snapshot differs from plan",
            )
        return ValidationReport(True, snapshot=actual)


def copy_candidate(source: Path, candidate: Path) -> None:
    """Compatibility helper used by transaction staging tests."""

    shutil.copyfile(source, candidate)


def replace_snapshot_entries(
    snapshot: ContainerSnapshot, entries: tuple[ContainerEntry, ...]
) -> ContainerSnapshot:
    return ContainerSnapshot(
        container_id=snapshot.container_id,
        adapter_id=snapshot.adapter_id,
        schema_version=snapshot.schema_version,
        format_variant=snapshot.format_variant,
        entries=tuple(sorted(entries, key=lambda entry: entry.identity.value)),
    )
