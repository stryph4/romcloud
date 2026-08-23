from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from romcloud.core.save_containers import (
    ContainerEntry,
    ContainerSnapshot,
    DomainAction,
    EntryIdentity,
    EntryReplacement,
    LogicalDomainState,
    LogicalEntryState,
    OpaqueContainerError,
    OpaqueReason,
    RebuildPlan,
    baseline_from_snapshot,
    plan_container_reconcile,
    target_snapshot,
)
from romcloud.infrastructure.pcsx2_folder_card import (
    ExplicitPs2FolderGroupingPolicy,
    PCSX2_FOLDER_CARD_MARKER,
    Pcsx2FolderMemoryCardAdapter,
)
from romcloud.infrastructure.save_container_registry import build_save_container_registry
from romcloud.infrastructure.ps1_memory_card import (
    BLOCK_SIZE,
    CARD_SIZE,
    FRAME_SIZE,
    Ps1RawMemoryCardAdapter,
)
from romcloud.core.storage import ProviderCapabilities, StorageProvider
from romcloud.core.models.savesync import SaveSyncState
from romcloud.infrastructure.savesync_state import save_state, state_from_dict, state_to_dict
from romcloud.infrastructure import save_transaction
from romcloud.core.exceptions import SaveSyncVerificationError
from romcloud.services.saves import SaveSyncService


def _entry(identity: str, domain: str, digest: str) -> ContainerEntry:
    return ContainerEntry(EntryIdentity(identity), domain, digest * 64, 1)


def _snapshot(*entries: ContainerEntry, physical_variant: str = "same") -> ContainerSnapshot:
    # physical_variant intentionally has no place in the logical model.
    _ = physical_variant
    return ContainerSnapshot("card", "adapter", 1, "variant", entries)


def test_generic_engine_merges_disjoint_domains_and_preserves_partial_conflict() -> None:
    a0 = _entry("a", "game-a", "0")
    b0 = _entry("b", "game-b", "1")
    c0 = _entry("c", "game-c", "2")
    baseline_snapshot = _snapshot(a0, b0, c0)
    baseline = baseline_from_snapshot(baseline_snapshot)
    a1 = replace(a0, canonical_hash="3" * 64)
    b1 = replace(b0, canonical_hash="4" * 64)
    c_local = replace(c0, canonical_hash="5" * 64)
    c_remote = replace(c0, canonical_hash="6" * 64)

    plan = plan_container_reconcile(
        _snapshot(a1, b0, c_local),
        _snapshot(a0, b1, c_remote),
        baseline,
    )

    actions = {decision.merge_domain_id: decision.action for decision in plan.decisions}
    assert actions == {
        "game-a": DomainAction.USE_LOCAL,
        "game-b": DomainAction.USE_REMOTE,
        "game-c": DomainAction.CONFLICT,
    }
    assert {value.merge_domain_id for value in plan.desired_local} == {
        "game-a",
        "game-b",
        "game-c",
    }
    assert next(
        value for value in plan.desired_local if value.merge_domain_id == "game-c"
    ).entries[0].canonical_hash == "5" * 64
    assert next(
        value for value in plan.desired_remote if value.merge_domain_id == "game-c"
    ).entries[0].canonical_hash == "6" * 64
    assert next(
        value for value in plan.next_baseline.domains if value.merge_domain_id == "game-c"
    ).entries[0].canonical_hash == "2" * 64


def test_generic_engine_propagates_delete_and_conflicts_delete_vs_modify() -> None:
    old = _entry("a", "game", "0")
    baseline = baseline_from_snapshot(_snapshot(old))
    deletion = plan_container_reconcile(_snapshot(), _snapshot(old), baseline)
    assert deletion.decisions[0].action is DomainAction.USE_LOCAL
    assert deletion.desired_remote == ()
    assert deletion.next_baseline.tombstones == ("game",)

    modified = replace(old, canonical_hash="1" * 64)
    conflict = plan_container_reconcile(_snapshot(), _snapshot(modified), baseline)
    assert conflict.decisions[0].action is DomainAction.CONFLICT


def test_generic_engine_add_add_and_adapter_version_invalidation() -> None:
    baseline = baseline_from_snapshot(_snapshot())
    left = _entry("a", "game", "0")
    right = _entry("b", "game", "1")
    assert plan_container_reconcile(
        _snapshot(left), _snapshot(right), baseline
    ).decisions[0].action is DomainAction.CONFLICT

    with pytest.raises(OpaqueContainerError) as error:
        plan_container_reconcile(
            _snapshot(left), replace(_snapshot(left), schema_version=2), baseline
        )
    assert error.value.reason is OpaqueReason.ADAPTER_VERSION_MISMATCH


def test_generic_engine_establishes_logical_baseline_despite_physical_inequality() -> None:
    entry = _entry("a", "game", "0")
    plan = plan_container_reconcile(
        _snapshot(entry, physical_variant="allocation-a"),
        _snapshot(entry, physical_variant="allocation-b"),
        None,
    )
    assert not plan.conflicts
    assert plan.next_baseline.domains[0].entries[0].canonical_hash == "0" * 64


def test_logical_baseline_state_round_trip_and_v3_migration() -> None:
    baseline = baseline_from_snapshot(_snapshot(_entry("a", "game", "0")))
    restored = state_from_dict(
        state_to_dict(SaveSyncState(device_id="device", container_baselines=(baseline,)))
    )
    assert restored.container_baselines == (baseline,)

    migrated = state_from_dict(
        {
            "version": 3,
            "device_id": "legacy-device",
            "shared_manifest": [],
            "groups": [],
            "conflicts": [],
            "remote_observation": {},
            "quick_sync_ready": True,
        }
    )
    assert migrated.container_baselines == ()
    assert migrated.quick_sync_ready is True


def _checksum(frame: bytearray) -> None:
    value = 0
    for byte in frame[:127]:
        value ^= byte
    frame[127] = value


def _ps1_card(entries: list[tuple[bytes, tuple[int, ...], bytes]]) -> bytes:
    raw = bytearray(b"\xff" * CARD_SIZE)
    header = bytearray(FRAME_SIZE)
    header[:2] = b"MC"
    _checksum(header)
    raw[:FRAME_SIZE] = header
    used = {block for _, blocks, _ in entries for block in blocks}
    for block in range(1, 16):
        frame = bytearray(FRAME_SIZE)
        frame[0] = 0xA0
        frame[8:10] = b"\xff\xff"
        _checksum(frame)
        raw[block * FRAME_SIZE : (block + 1) * FRAME_SIZE] = frame
    for filename, blocks, fill in entries:
        for index, block in enumerate(blocks):
            frame = bytearray(FRAME_SIZE)
            frame[0] = 0x51 if index == 0 else (0x53 if index == len(blocks) - 1 else 0x52)
            if index == 0:
                frame[4:8] = (len(blocks) * BLOCK_SIZE).to_bytes(4, "little")
                frame[10 : 10 + len(filename)] = filename
            frame[8:10] = (
                b"\xff\xff"
                if index == len(blocks) - 1
                else (blocks[index + 1] - 1).to_bytes(2, "little")
            )
            _checksum(frame)
            raw[block * FRAME_SIZE : (block + 1) * FRAME_SIZE] = frame
            raw[block * BLOCK_SIZE : (block + 1) * BLOCK_SIZE] = fill * BLOCK_SIZE
    assert used.issubset(set(range(1, 16)))
    return bytes(raw)


def test_ps1_snapshot_is_allocation_independent_and_domain_conservative(tmp_path: Path) -> None:
    adapter = Ps1RawMemoryCardAdapter()
    first = tmp_path / "first.mcd"
    second = tmp_path / "second.mcd"
    filename = b"BASLUS-01251SAVE-A"
    first.write_bytes(_ps1_card([(filename, (1, 4), b"A")]))
    second.write_bytes(_ps1_card([(filename, (7, 2), b"A")]))

    left = adapter.snapshot(first, container_id="duckstation/memcards/card.mcd")
    right = adapter.snapshot(second, container_id="duckstation/memcards/card.mcd")
    assert left.entries[0].canonical_hash == right.entries[0].canonical_hash
    assert left.entries[0].merge_domain_id == "ps1-game:4241534c55532d3031323531"


def test_ps1_rebuild_adds_from_verified_free_and_preserves_deleted_residue(
    tmp_path: Path,
) -> None:
    adapter = Ps1RawMemoryCardAdapter()
    destination = tmp_path / "destination.mcd"
    source = tmp_path / "source.mcd"
    candidate = tmp_path / "candidate.mcd"
    first_name = b"BASLUS-00001SAVE"
    second_name = b"BESCES-00002SAVE"
    destination.write_bytes(_ps1_card([(first_name, (1,), b"A")]))
    source.write_bytes(_ps1_card([(second_name, (9, 3), b"B")]))
    destination_snapshot = adapter.snapshot(destination, container_id="card")
    source_snapshot = adapter.snapshot(source, container_id="card")
    expected = ContainerSnapshot(
        "card",
        adapter.adapter_id,
        adapter.schema_version,
        "ps1-raw-128k",
        tuple(
            sorted(
                (*destination_snapshot.entries, *source_snapshot.entries),
                key=lambda entry: entry.identity.value,
            )
        ),
    )
    extracted = adapter.extract_entry(
        source, source_snapshot, source_snapshot.entries[0].identity
    )
    receipt = adapter.rebuild(
        destination,
        RebuildPlan(
            expected,
            replacements=(EntryReplacement(source_snapshot.entries[0], extracted),),
        ),
        candidate,
    )
    assert receipt.candidate_path == candidate
    assert adapter.validate(candidate, expected).valid
    assert candidate.read_bytes()[1 * BLOCK_SIZE : 2 * BLOCK_SIZE] == b"A" * BLOCK_SIZE


@pytest.mark.parametrize(
    ("mutator", "reason"),
    [
        (lambda raw: raw[:-1], OpaqueReason.UNSUPPORTED_PS1_CARD_SIZE),
        (
            lambda raw: raw[:127] + bytes((raw[127] ^ 1,)) + raw[128:],
            OpaqueReason.CHECKSUM_FAILURE,
        ),
    ],
)
def test_ps1_falls_back_for_invalid_cards(tmp_path: Path, mutator, reason) -> None:
    path = tmp_path / "card.mcd"
    path.write_bytes(mutator(_ps1_card([])))
    probe = Ps1RawMemoryCardAdapter().probe(path)
    assert not probe.supported
    assert probe.opaque_reason is reason


def _rewrite_directory_frame(raw: bytearray, block: int, mutate) -> None:
    start = block * FRAME_SIZE
    frame = bytearray(raw[start : start + FRAME_SIZE])
    mutate(frame)
    _checksum(frame)
    raw[start : start + FRAME_SIZE] = frame


def test_ps1_rejects_loops_crosslinks_duplicates_and_broken_sector_map(
    tmp_path: Path,
) -> None:
    adapter = Ps1RawMemoryCardAdapter()
    cases: list[tuple[bytes, OpaqueReason]] = []

    loop = bytearray(_ps1_card([(b"BASLUS-00001SAVE", (1, 2), b"A")]))
    _rewrite_directory_frame(
        loop,
        2,
        lambda frame: (frame.__setitem__(0, 0x52), frame.__setitem__(slice(8, 10), b"\x00\x00")),
    )
    cases.append((bytes(loop), OpaqueReason.MALFORMED_CHAIN))

    crosslink = bytearray(
        _ps1_card(
            [
                (b"BASLUS-00001SAVE", (1, 2), b"A"),
                (b"BESCES-00002SAVE", (3,), b"B"),
            ]
        )
    )
    _rewrite_directory_frame(
        crosslink,
        3,
        lambda frame: frame.__setitem__(slice(8, 10), b"\x01\x00"),
    )
    cases.append((bytes(crosslink), OpaqueReason.MALFORMED_CHAIN))

    duplicate = _ps1_card(
        [
            (b"BASLUS-00001SAVE", (1,), b"A"),
            (b"BASLUS-00001SAVE", (2,), b"B"),
        ]
    )
    cases.append((duplicate, OpaqueReason.DUPLICATE_IDENTITY))

    broken = bytearray(_ps1_card([]))
    broken[16 * FRAME_SIZE : 16 * FRAME_SIZE + 4] = b"\x00\xff\xff\xff"
    cases.append((bytes(broken), OpaqueReason.PS1_BROKEN_SECTOR_MAPPING))

    for index, (content, reason) in enumerate(cases):
        path = tmp_path / f"invalid-{index}.mcd"
        path.write_bytes(content)
        probe = adapter.probe(path)
        assert not probe.supported
        assert probe.opaque_reason is reason


def test_ps1_rejects_noncommercial_namespace(tmp_path: Path) -> None:
    path = tmp_path / "homebrew.mcd"
    path.write_bytes(_ps1_card([(b"HOMEBREW-SAVE", (1,), b"H")]))
    probe = Ps1RawMemoryCardAdapter().probe(path)
    assert not probe.supported
    assert probe.opaque_reason is OpaqueReason.AMBIGUOUS_NAMESPACE


def test_ps1_never_reuses_deleted_blocks_and_fails_when_verified_free_is_short(
    tmp_path: Path,
) -> None:
    adapter = Ps1RawMemoryCardAdapter()
    destination = tmp_path / "destination.mcd"
    source = tmp_path / "source.mcd"
    candidate = tmp_path / "candidate.mcd"
    old_name = b"BASLUS-00001SAVE"
    new_name = b"BESCES-00002SAVE"
    deleted = bytearray(_ps1_card([(old_name, (1,), b"A")]))
    _rewrite_directory_frame(
        deleted,
        5,
        lambda frame: (
            frame.__setitem__(0, 0xA1),
            frame.__setitem__(slice(8, 10), b"\xff\xff"),
        ),
    )
    deleted[5 * BLOCK_SIZE : 6 * BLOCK_SIZE] = b"D" * BLOCK_SIZE
    destination.write_bytes(deleted)
    source.write_bytes(_ps1_card([(new_name, (7,), b"N")]))
    destination_snapshot = adapter.snapshot(destination, container_id="card")
    source_snapshot = adapter.snapshot(source, container_id="card")
    expected = replace(
        destination_snapshot,
        entries=tuple(
            sorted(
                (*destination_snapshot.entries, *source_snapshot.entries),
                key=lambda entry: entry.identity.value,
            )
        ),
    )
    extracted = adapter.extract_entry(source, source_snapshot, source_snapshot.entries[0].identity)
    adapter.rebuild(
        destination,
        RebuildPlan(expected, (EntryReplacement(source_snapshot.entries[0], extracted),)),
        candidate,
    )
    result = candidate.read_bytes()
    assert result[5 * BLOCK_SIZE : 6 * BLOCK_SIZE] == b"D" * BLOCK_SIZE
    assert result[5 * FRAME_SIZE] == 0xA1

    full = tmp_path / "full.mcd"
    two_blocks = tmp_path / "two-blocks.mcd"
    full.write_bytes(_ps1_card([(old_name, tuple(range(1, 15)), b"F")]))
    two_blocks.write_bytes(_ps1_card([(new_name, (1, 2), b"N")]))
    full_snapshot = adapter.snapshot(full, container_id="full")
    new_snapshot = adapter.snapshot(two_blocks, container_id="full")
    expected_full = replace(
        full_snapshot,
        entries=tuple(
            sorted(
                (*full_snapshot.entries, *new_snapshot.entries),
                key=lambda entry: entry.identity.value,
            )
        ),
    )
    extracted_new = adapter.extract_entry(
        two_blocks, new_snapshot, new_snapshot.entries[0].identity
    )
    with pytest.raises(OpaqueContainerError) as error:
        adapter.rebuild(
            full,
            RebuildPlan(
                expected_full,
                (EntryReplacement(new_snapshot.entries[0], extracted_new),),
            ),
            tmp_path / "insufficient.mcd",
        )
    assert error.value.reason is OpaqueReason.INSUFFICIENT_VERIFIED_FREE_CAPACITY


def _folder_card(root: Path, entries: dict[str, dict[str, bytes]]) -> None:
    root.mkdir(parents=True)
    (root / PCSX2_FOLDER_CARD_MARKER).write_bytes(b"marker")
    for top, files in entries.items():
        for relative, content in files.items():
            path = root / top / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)


def test_ps2_folder_hash_is_order_independent_and_nested(tmp_path: Path) -> None:
    left = tmp_path / "left"
    right = tmp_path / "right"
    _folder_card(left, {"BASLUS-00001": {"icon.sys": b"i", "nested/data": b"d"}})
    _folder_card(right, {"BASLUS-00001": {"nested/data": b"d", "icon.sys": b"i"}})
    adapter = Pcsx2FolderMemoryCardAdapter()
    assert adapter.snapshot(left, container_id="card").entries == adapter.snapshot(
        right, container_id="card"
    ).entries


def test_ps2_folder_multiple_directories_require_complete_grouping(tmp_path: Path) -> None:
    card = tmp_path / "card"
    _folder_card(card, {"SAVE-A": {"data": b"a"}, "SAVE-B": {"data": b"b"}})
    with pytest.raises(OpaqueContainerError) as error:
        Pcsx2FolderMemoryCardAdapter().snapshot(card, container_id="card")
    assert error.value.reason is OpaqueReason.AMBIGUOUS_PS2_FOLDER_GROUPING

    adapter = Pcsx2FolderMemoryCardAdapter(
        ExplicitPs2FolderGroupingPolicy(
            {b"SAVE-A": "game-a", b"SAVE-B": "game-b"},
            policy_id="pcsx2-gamedb-test",
            schema_version=7,
        )
    )
    domains = {
        entry.merge_domain_id
        for entry in adapter.snapshot(card, container_id="card").entries
    }
    assert domains == {
        "game-a",
        "game-b",
    }


def test_ps2_folder_rebuild_and_symlink_fallback(tmp_path: Path) -> None:
    destination = tmp_path / "destination"
    source = tmp_path / "source"
    candidate = tmp_path / "candidate"
    _folder_card(destination, {"SAVE": {"nested/data": b"old"}})
    _folder_card(source, {"SAVE": {"nested/data": b"new"}})
    adapter = Pcsx2FolderMemoryCardAdapter()
    old = adapter.snapshot(destination, container_id="card")
    new = adapter.snapshot(source, container_id="card")
    extracted = adapter.extract_entry(source, new, new.entries[0].identity)
    adapter.rebuild(
        destination,
        RebuildPlan(
            new,
            replacements=(EntryReplacement(new.entries[0], extracted),),
        ),
        candidate,
    )
    assert adapter.validate(candidate, new).valid
    assert (candidate / "SAVE/nested/data").read_bytes() == b"new"

    (candidate / "SAVE/link").symlink_to(tmp_path / "outside")
    probe = adapter.probe(candidate)
    assert not probe.supported
    assert probe.opaque_reason is OpaqueReason.SYMLINK_OR_PATH_SUBSTITUTION


class _FilesystemTestProvider(StorageProvider):
    @property
    def provider_id(self) -> str:
        return "container-test"

    @property
    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            has_filesystem_semantics=True, supports_durable_transactions=True
        )

    def is_reachable(self, root: str) -> bool:
        return True

    def list_systems(self, rom_root: str):
        raise NotImplementedError

    def list_entries(self, rom_root: str, system: str):
        raise NotImplementedError

    def get_size(self, path: str):
        raise NotImplementedError

    def read_text(self, path: str) -> str:
        raise NotImplementedError

    def transfer_to(self, source_path: str, dest_path: str, on_progress=None):
        raise NotImplementedError


def _container_service(
    tmp_path: Path, *, container_registry=None
) -> SaveSyncService:
    local = tmp_path / "local"
    local.mkdir()
    return SaveSyncService(
        provider=_FilesystemTestProvider(),
        connectivity_root=str(tmp_path / "remote-data"),
        local_root=str(local),
        remote_root=str(tmp_path / "remote"),
        state_path=tmp_path / "data/state.json",
        **({"container_registry": container_registry} if container_registry else {}),
    )


def test_service_automatically_merges_disjoint_ps1_domains_through_existing_transaction(
    tmp_path: Path,
) -> None:
    service = _container_service(tmp_path)
    local = tmp_path / "local/duckstation/memcards/card.mcd"
    local.parent.mkdir(parents=True)
    first = b"BASLUS-00001SAVE"
    second = b"BESCES-00002SAVE"
    local.write_bytes(_ps1_card([(first, (1,), b"A"), (second, (2,), b"B")]))
    service.commit_upload(service.preview_upload())
    # Establish the first trusted logical baseline without requiring physical
    # card-byte equality as a persistence invariant.
    service.full_sync()

    remote = tmp_path / "remote/duckstation/memcards/card.mcd"
    local.write_bytes(_ps1_card([(first, (1,), b"L"), (second, (2,), b"B")]))
    remote.write_bytes(_ps1_card([(first, (1,), b"A"), (second, (2,), b"R")]))

    preview = service.preview_reconciliation()
    report = service.reconcile()

    adapter = Ps1RawMemoryCardAdapter()
    local_snapshot = adapter.snapshot(local, container_id="card")
    remote_snapshot = adapter.snapshot(remote, container_id="card")
    assert report.uploaded == 1
    assert report.downloaded == 1
    assert report.conflicts == 0
    assert len(preview.uploads) == 1
    assert len(preview.downloads) == 1
    assert not preview.conflicts
    assert local_snapshot.entries == remote_snapshot.entries
    assert len(service.get_state().container_baselines) == 1


def test_service_persists_ps1_partial_conflict_after_converging_other_domains(
    tmp_path: Path,
) -> None:
    service = _container_service(tmp_path)
    local = tmp_path / "local/duckstation/memcards/card.mcd"
    local.parent.mkdir(parents=True)
    names = (
        b"BASLUS-00001SAVE",
        b"BESCES-00002SAVE",
        b"BISLPS-00003SAVE",
    )
    local.write_bytes(
        _ps1_card([(names[0], (1,), b"A"), (names[1], (2,), b"B"), (names[2], (3,), b"C")])
    )
    service.commit_upload(service.preview_upload())
    remote = tmp_path / "remote/duckstation/memcards/card.mcd"
    local.write_bytes(
        _ps1_card([(names[0], (1,), b"L"), (names[1], (2,), b"B"), (names[2], (3,), b"X")])
    )
    remote.write_bytes(
        _ps1_card([(names[0], (1,), b"A"), (names[1], (2,), b"R"), (names[2], (3,), b"Y")])
    )

    preview = service.preview_reconciliation()
    first = service.reconcile()
    second = service.reconcile()

    assert len(preview.uploads) == 1
    assert len(preview.downloads) == 1
    assert len(preview.conflicts) == 1
    assert first.uploaded == 1 and first.downloaded == 1 and first.conflicts == 1
    assert second.uploaded == 0 and second.downloaded == 0 and second.conflicts == 1
    local_snapshot = Ps1RawMemoryCardAdapter().snapshot(local, container_id="card")
    remote_snapshot = Ps1RawMemoryCardAdapter().snapshot(remote, container_id="card")
    by_domain_local = {entry.merge_domain_id: entry for entry in local_snapshot.entries}
    by_domain_remote = {entry.merge_domain_id: entry for entry in remote_snapshot.entries}
    domains = tuple(f"ps1-game:{name[:12].hex()}" for name in names)
    assert by_domain_local[domains[0]] == by_domain_remote[domains[0]]
    assert by_domain_local[domains[1]] == by_domain_remote[domains[1]]
    assert by_domain_local[domains[2]] != by_domain_remote[domains[2]]
    assert len(service.get_state().active_conflicts) == 1


def test_quick_sync_reconciles_dirty_ps1_container_domain(tmp_path: Path) -> None:
    service = _container_service(tmp_path)
    local = tmp_path / "local/duckstation/memcards/card.mcd"
    local.parent.mkdir(parents=True)
    name = b"BASLUS-00001SAVE"
    local.write_bytes(_ps1_card([(name, (1,), b"A")]))
    service.commit_upload(service.preview_upload())
    service.full_sync()
    local.write_bytes(_ps1_card([(name, (1,), b"L")]))
    service.mark_local_dirty("duckstation/memcards/card.mcd")

    result = service.quick_sync()

    assert result.status == "reconciled"
    assert result.report is not None and result.report.uploaded == 1
    remote = tmp_path / "remote/duckstation/memcards/card.mcd"
    assert Ps1RawMemoryCardAdapter().snapshot(local, container_id="card").entries == (
        Ps1RawMemoryCardAdapter().snapshot(remote, container_id="card").entries
    )


def test_quick_sync_repairs_missing_ps1_card_before_returning_unchanged(
    tmp_path: Path,
) -> None:
    service = _container_service(tmp_path)
    local = tmp_path / "local/duckstation/memcards/card.mcd"
    local.parent.mkdir(parents=True)
    name = b"BASLUS-00001SAVE"
    content = _ps1_card([(name, (1,), b"A")])
    local.write_bytes(content)
    service.commit_upload(service.preview_upload())
    service.full_sync()
    local.unlink()

    repaired = service.quick_sync()
    unchanged = service.quick_sync()

    assert repaired.status == "reconciled"
    assert repaired.report is not None and repaired.report.downloaded == 1
    assert local.read_bytes() == content
    assert unchanged.status == "unchanged"


def test_container_source_change_during_staging_rolls_back_transaction(
    tmp_path: Path, monkeypatch
) -> None:
    service = _container_service(tmp_path)
    local = tmp_path / "local/duckstation/memcards/card.mcd"
    local.parent.mkdir(parents=True)
    first = b"BASLUS-00001SAVE"
    second = b"BESCES-00002SAVE"
    local.write_bytes(_ps1_card([(first, (1,), b"A"), (second, (2,), b"B")]))
    service.commit_upload(service.preview_upload())
    remote = tmp_path / "remote/duckstation/memcards/card.mcd"
    local.write_bytes(_ps1_card([(first, (1,), b"L"), (second, (2,), b"B")]))
    remote.write_bytes(_ps1_card([(first, (1,), b"A"), (second, (2,), b"R")]))
    remote_before = remote.read_bytes()
    real_prepare = save_transaction.prepare_transaction

    def prepare_then_change(*args, **kwargs):
        transaction = real_prepare(*args, **kwargs)
        local.write_bytes(_ps1_card([(first, (1,), b"X"), (second, (2,), b"B")]))
        return transaction

    monkeypatch.setattr(save_transaction, "prepare_transaction", prepare_then_change)

    with pytest.raises(SaveSyncVerificationError, match="changed while staging"):
        service.reconcile()

    assert remote.read_bytes() == remote_before
    assert not (tmp_path / "data/savesync-transaction.json").exists()


def test_adapter_schema_change_invalidates_logical_baseline_and_falls_back_opaque(
    tmp_path: Path,
) -> None:
    service = _container_service(tmp_path)
    local = tmp_path / "local/duckstation/memcards/card.mcd"
    local.parent.mkdir(parents=True)
    name = b"BASLUS-00001SAVE"
    local.write_bytes(_ps1_card([(name, (1,), b"A")]))
    service.commit_upload(service.preview_upload())
    state = service.get_state()
    assert len(state.container_baselines) == 1
    incompatible = replace(
        state.container_baselines[0],
        schema_version=state.container_baselines[0].schema_version + 1,
    )
    save_state(service._state_path, replace(state, container_baselines=(incompatible,)))
    local.write_bytes(_ps1_card([(name, (1,), b"L")]))
    remote = tmp_path / "remote/duckstation/memcards/card.mcd"
    remote.write_bytes(_ps1_card([(name, (1,), b"R")]))

    report = service.reconcile()

    assert report.conflicts == 1
    assert service.get_state().container_baselines == ()


def test_service_keeps_ps2_file_cards_opaque_with_automatic_container_merging(
    tmp_path: Path,
) -> None:
    service = _container_service(tmp_path)
    local = tmp_path / "local/ps2/pcsx2/Mcd001.ps2"
    local.parent.mkdir(parents=True)
    local.write_bytes(b"opaque-base")
    service.commit_upload(service.preview_upload())
    local.write_bytes(b"opaque-local")
    remote = tmp_path / "remote/ps2/pcsx2/Mcd001.ps2"
    remote.write_bytes(b"opaque-remote")

    report = service.reconcile()

    assert report.conflicts == 1
    assert local.read_bytes() == b"opaque-local"
    assert remote.read_bytes() == b"opaque-remote"
    assert service.get_state().container_baselines == ()


def test_service_automatically_merges_ps2_folder_card_with_complete_versioned_grouping(
    tmp_path: Path,
) -> None:
    grouping = ExplicitPs2FolderGroupingPolicy(
        {b"SAVE-A": "game-a", b"SAVE-B": "game-b"},
        policy_id="pcsx2-authoritative-fixture",
        schema_version=9,
    )
    service = _container_service(
        tmp_path,
        container_registry=build_save_container_registry(ps2_grouping_policy=grouping),
    )
    local = tmp_path / "local/ps2/pcsx2/Card1"
    _folder_card(
        local,
        {"SAVE-A": {"data": b"a0"}, "SAVE-B": {"nested/data": b"b0"}},
    )
    service.commit_upload(service.preview_upload())
    service.full_sync()
    remote = tmp_path / "remote/ps2/pcsx2/Card1"
    (local / "SAVE-A/data").write_bytes(b"a-local")
    (remote / "SAVE-B/nested/data").write_bytes(b"b-remote")

    report = service.reconcile()

    adapter = Pcsx2FolderMemoryCardAdapter(grouping)
    assert report.uploaded == 1
    assert report.downloaded == 1
    assert report.conflicts == 0
    assert adapter.snapshot(local, container_id="card").entries == adapter.snapshot(
        remote, container_id="card"
    ).entries
    assert (local / "SAVE-B/nested/data").read_bytes() == b"b-remote"
    assert (remote / "SAVE-A/data").read_bytes() == b"a-local"


def test_service_propagates_ps2_folder_entry_deletion_narrowly(tmp_path: Path) -> None:
    grouping = ExplicitPs2FolderGroupingPolicy(
        {b"SAVE-A": "game-a", b"SAVE-B": "game-b"},
        policy_id="pcsx2-authoritative-fixture",
        schema_version=9,
    )
    service = _container_service(
        tmp_path,
        container_registry=build_save_container_registry(ps2_grouping_policy=grouping),
    )
    local = tmp_path / "local/ps2/pcsx2/Card1"
    _folder_card(local, {"SAVE-A": {"data": b"a"}, "SAVE-B": {"data": b"b"}})
    service.commit_upload(service.preview_upload())
    remote = tmp_path / "remote/ps2/pcsx2/Card1"
    (remote / "SAVE-A/data").unlink()

    report = service.reconcile()

    assert report.downloaded == 1
    assert not (local / "SAVE-A/data").exists()
    assert (local / "SAVE-B/data").read_bytes() == b"b"
    assert (remote / "SAVE-B/data").read_bytes() == b"b"


def test_service_keeps_ambiguous_multi_directory_ps2_folder_card_opaque(
    tmp_path: Path,
) -> None:
    service = _container_service(tmp_path)
    local = tmp_path / "local/ps2/pcsx2/Card1"
    _folder_card(local, {"SAVE-A": {"data": b"a0"}, "SAVE-B": {"data": b"b0"}})
    service.commit_upload(service.preview_upload())
    remote = tmp_path / "remote/ps2/pcsx2/Card1"
    (local / "SAVE-A/data").write_bytes(b"a-local")
    (remote / "SAVE-B/data").write_bytes(b"b-remote")

    report = service.reconcile()

    assert report.conflicts == 3
    assert (local / "SAVE-B/data").read_bytes() == b"b0"
    assert (remote / "SAVE-A/data").read_bytes() == b"a0"
    assert service.get_state().container_baselines == ()
