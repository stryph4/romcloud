"""Direct crash-safety tests for targeted SaveSync transactions."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest

from romcloud.core.exceptions import SaveSyncError, SaveSyncVerificationError
from romcloud.core.models.savesync import SaveArtifact
from romcloud.infrastructure import save_transaction, save_tree


_OPERATION_ID = "0123456789abcdef0123456789abcdef"


def _artifact(relative: str, content: bytes) -> SaveArtifact:
    return SaveArtifact(relative, len(content), hashlib.sha256(content).hexdigest())


def _write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)


def _manifest(contents: dict[str, bytes]) -> dict[str, SaveArtifact]:
    return {path: _artifact(path, content) for path, content in contents.items()}


def _prepare(
    tmp_path: Path,
    *,
    current_content: dict[str, bytes],
    desired_content: dict[str, bytes],
    operation_id: str = _OPERATION_ID,
) -> tuple[
    save_transaction.SelectedTransaction,
    Path,
    Path,
    dict[str, SaveArtifact],
    dict[str, SaveArtifact],
]:
    root = tmp_path / "saves"
    source = tmp_path / "source"
    root.mkdir()
    source.mkdir()
    for relative, content in current_content.items():
        _write(root / relative, content)
    for relative, content in desired_content.items():
        _write(source / relative, content)
    current = _manifest(current_content)
    desired = _manifest(desired_content)
    journal = tmp_path / "transaction.json"
    transaction = save_transaction.prepare_transaction(
        journal,
        (
            save_transaction.SelectedView(
                root,
                current,
                desired,
                lambda relative, _artifact: source / relative,
            ),
        ),
        operation_id=operation_id,
    )
    return transaction, journal, root, current, desired


def _apply(
    transaction: save_transaction.SelectedTransaction,
) -> None:
    save_transaction.apply_transaction(
        transaction,
        verify_current=save_transaction._verify_manifest,
        verify_desired=save_transaction._verify_manifest,
    )


def test_operation_snapshot_survives_until_receipt_finalize(tmp_path: Path) -> None:
    stable_previous = tmp_path / "saves.savesync-previous"
    _write(stable_previous / "game.srm", b"historic")
    transaction, journal, root, _, _ = _prepare(
        tmp_path,
        current_content={"game.srm": b"before"},
        desired_content={"game.srm": b"after"},
    )
    candidate = transaction.views[0].previous_candidate

    assert (stable_previous / "game.srm").read_bytes() == b"historic"
    assert (candidate / "game.srm").read_bytes() == b"before"

    _apply(transaction)

    assert (root / "game.srm").read_bytes() == b"after"
    assert (stable_previous / "game.srm").read_bytes() == b"historic"
    assert (candidate / "game.srm").read_bytes() == b"before"
    assert journal.exists()

    transaction.finalize()
    transaction.finalize()

    assert (stable_previous / "game.srm").read_bytes() == b"before"
    assert not candidate.exists()
    assert not journal.exists()


def test_interruption_without_receipt_rolls_back_idempotently(tmp_path: Path) -> None:
    transaction, journal, root, _, _ = _prepare(
        tmp_path,
        current_content={"game.srm": b"before"},
        desired_content={"game.srm": b"after"},
    )
    _apply(transaction)

    assert save_transaction.recover_transaction(
        journal, allowed_roots=(root,), completed_operation_id=None
    )
    assert (root / "game.srm").read_bytes() == b"before"
    assert not journal.exists()
    assert not save_transaction.recover_transaction(
        journal, allowed_roots=(root,), completed_operation_id=None
    )
    assert (root / "game.srm").read_bytes() == b"before"


def test_receipt_recovery_keeps_promotion_and_finalizes_idempotently(
    tmp_path: Path,
) -> None:
    transaction, journal, root, _, _ = _prepare(
        tmp_path,
        current_content={"game.srm": b"before"},
        desired_content={"game.srm": b"after"},
    )
    _apply(transaction)

    assert save_transaction.recover_transaction(
        journal,
        allowed_roots=(root,),
        completed_operation_id=_OPERATION_ID,
    )
    assert (root / "game.srm").read_bytes() == b"after"
    assert (
        tmp_path / "saves.savesync-previous/game.srm"
    ).read_bytes() == b"before"
    assert not journal.exists()
    assert not save_transaction.recover_transaction(
        journal,
        allowed_roots=(root,),
        completed_operation_id=_OPERATION_ID,
    )


def test_legacy_recovery_cannot_promote_selected_only_snapshot(
    tmp_path: Path,
) -> None:
    """Targeted history must never masquerade as a whole-root backup."""
    transaction, _, root, _, _ = _prepare(
        tmp_path,
        current_content={"psx/game.srm": b"before"},
        desired_content={"psx/game.srm": b"after"},
    )
    _apply(transaction)
    transaction.finalize()
    selected_history = tmp_path / "saves.savesync-previous"

    assert (selected_history / "psx/game.srm").read_bytes() == b"before"
    assert not (tmp_path / "saves.previous").exists()

    # Simulate external loss/unmount of the live root followed by the legacy
    # whole-directory recovery hook that the service still runs for upgrades.
    root.rename(tmp_path / "detached-live-root")
    save_tree.recover_interrupted_commit(root)

    assert not root.exists()
    assert (selected_history / "psx/game.srm").read_bytes() == b"before"


def test_recovery_handles_interruption_after_only_one_live_rename(
    tmp_path: Path,
) -> None:
    transaction, journal, root, _, _ = _prepare(
        tmp_path,
        current_content={"a.srm": b"a-before", "b.srm": b"b-before"},
        desired_content={"a.srm": b"a-after", "b.srm": b"b-after"},
    )
    save_transaction._write_journal(transaction, phase="applying")
    os.replace(transaction.views[0].stage / "a.srm", root / "a.srm")

    save_transaction.recover_transaction(
        journal, allowed_roots=(root,), completed_operation_id=None
    )

    assert (root / "a.srm").read_bytes() == b"a-before"
    assert (root / "b.srm").read_bytes() == b"b-before"
    assert not journal.exists()


def test_third_version_blocks_entire_rollback_and_preserves_evidence(
    tmp_path: Path,
) -> None:
    transaction, journal, root, _, _ = _prepare(
        tmp_path,
        current_content={"a.srm": b"a-before", "z.srm": b"z-before"},
        desired_content={"a.srm": b"a-after", "z.srm": b"z-after"},
    )
    _apply(transaction)
    (root / "z.srm").write_bytes(b"third-version")

    with pytest.raises(SaveSyncError, match="third, unrecognized version"):
        save_transaction.recover_transaction(
            journal, allowed_roots=(root,), completed_operation_id=None
        )

    # Preflight finds z before changing a, even though a sorts first.
    assert (root / "a.srm").read_bytes() == b"a-after"
    assert (root / "z.srm").read_bytes() == b"third-version"
    assert journal.exists()
    assert transaction.views[0].previous_candidate.exists()


def test_corrupt_rollback_snapshot_cannot_partially_restore(tmp_path: Path) -> None:
    transaction, journal, root, _, _ = _prepare(
        tmp_path,
        current_content={"a.srm": b"a-before", "z.srm": b"z-before"},
        desired_content={"a.srm": b"a-after", "z.srm": b"z-after"},
    )
    _apply(transaction)
    (transaction.views[0].previous_candidate / "z.srm").write_bytes(b"corrupt")

    with pytest.raises(SaveSyncError, match="recovery journal was preserved"):
        save_transaction.recover_transaction(
            journal, allowed_roots=(root,), completed_operation_id=None
        )

    assert (root / "a.srm").read_bytes() == b"a-after"
    assert (root / "z.srm").read_bytes() == b"z-after"
    assert journal.exists()


@pytest.mark.parametrize(
    "relative",
    [
        "/absolute.srm",
        "../escape.srm",
        "folder\\escape.srm",
        "folder/./save.srm",
        "folder//save.srm",
        "C:/escape.srm",
    ],
)
def test_noncanonical_manifest_paths_fail_before_journaling(
    tmp_path: Path, relative: str
) -> None:
    root = tmp_path / "saves"
    artifact = _artifact(relative, b"save")

    with pytest.raises(SaveSyncError, match="Unsafe SaveSync relative path"):
        save_transaction.prepare_transaction(
            tmp_path / "transaction.json",
            (save_transaction.SelectedView(root, {}, {relative: artifact}, lambda *_: root),),
            operation_id=_OPERATION_ID,
        )

    assert not (tmp_path / "transaction.json").exists()


@pytest.mark.parametrize(
    ("size", "digest"),
    [(-1, "0" * 64), (True, "0" * 64), (1, "0" * 63), (1, "g" * 64)],
)
def test_invalid_manifest_metadata_fails_closed(
    tmp_path: Path, size: object, digest: str
) -> None:
    artifact = SaveArtifact("game.srm", size, digest)  # type: ignore[arg-type]
    with pytest.raises(SaveSyncError):
        save_transaction.prepare_transaction(
            tmp_path / "transaction.json",
            (
                save_transaction.SelectedView(
                    tmp_path / "saves", {}, {"game.srm": artifact}, lambda *_: tmp_path
                ),
            ),
            operation_id=_OPERATION_ID,
        )


def test_hostile_operation_id_cannot_escape_transaction_parent(tmp_path: Path) -> None:
    outside = tmp_path.parent / "escaped-transaction-data"
    with pytest.raises(SaveSyncError, match="operation ID"):
        save_transaction.prepare_transaction(
            tmp_path / "transaction.json",
            (save_transaction.SelectedView(tmp_path / "saves", {}, {}, lambda *_: tmp_path),),
            operation_id="../../escaped-transaction-data",
        )
    assert not outside.exists()


def test_existing_recovery_journal_is_never_overwritten(tmp_path: Path) -> None:
    journal = tmp_path / "transaction.json"
    journal.write_bytes(b"preserve-this-unresolved-record")

    with pytest.raises(SaveSyncError, match="already exists"):
        save_transaction.prepare_transaction(
            journal,
            (save_transaction.SelectedView(tmp_path / "saves", {}, {}, lambda *_: tmp_path),),
            operation_id=_OPERATION_ID,
        )

    assert journal.read_bytes() == b"preserve-this-unresolved-record"


def test_duplicate_and_overlapping_destination_roots_are_rejected(
    tmp_path: Path,
) -> None:
    root = tmp_path / "saves"
    views = (
        save_transaction.SelectedView(root, {}, {}, lambda *_: tmp_path),
        save_transaction.SelectedView(root / "nested", {}, {}, lambda *_: tmp_path),
    )

    with pytest.raises(SaveSyncError, match="overlapping roots"):
        save_transaction.prepare_transaction(
            tmp_path / "transaction.json", views, operation_id=_OPERATION_ID
        )


def test_forged_out_of_scope_journal_root_is_preserved_and_rejected(
    tmp_path: Path,
) -> None:
    transaction, journal, root, _, _ = _prepare(
        tmp_path,
        current_content={"game.srm": b"before"},
        desired_content={"game.srm": b"after"},
    )
    outside = tmp_path / "outside"
    outside.mkdir()
    marker = outside / "marker"
    marker.write_bytes(b"untouched")
    payload = json.loads(journal.read_text(encoding="utf-8"))
    view = payload["views"][0]
    view["root"] = str(outside)
    view["stage"] = str(outside.parent / f".{outside.name}.savesync-stage-{_OPERATION_ID}")
    view["previous"] = str(
        outside.with_name(f"{outside.name}.savesync-previous")
    )
    view["previous_candidate"] = str(
        outside.parent / f".{outside.name}.savesync-previous-{_OPERATION_ID}"
    )
    journal.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(SaveSyncError, match="outside configured"):
        save_transaction.recover_transaction(
            journal, allowed_roots=(root,), completed_operation_id=None
        )

    assert marker.read_bytes() == b"untouched"
    assert journal.exists()
    assert transaction.views[0].previous_candidate.exists()


@pytest.mark.parametrize("corruption", ["duplicate", "hash", "size"])
def test_corrupt_journal_manifest_is_rejected_without_cleanup(
    tmp_path: Path, corruption: str
) -> None:
    transaction, journal, root, _, _ = _prepare(
        tmp_path,
        current_content={"game.srm": b"before"},
        desired_content={"game.srm": b"after"},
    )
    payload = json.loads(journal.read_text(encoding="utf-8"))
    current = payload["views"][0]["current"]
    if corruption == "duplicate":
        current.append(dict(current[0]))
    elif corruption == "hash":
        current[0]["content_hash"] = "0" * 63
    else:
        current[0]["size_bytes"] = True
    journal.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(SaveSyncError, match="journal is invalid"):
        save_transaction.recover_transaction(
            journal, allowed_roots=(root,), completed_operation_id=None
        )

    assert journal.exists()
    assert transaction.views[0].previous_candidate.exists()
    assert (root / "game.srm").read_bytes() == b"before"


def test_symlinked_destination_root_is_rejected_when_supported(
    tmp_path: Path,
) -> None:
    real_root = tmp_path / "real"
    real_root.mkdir()
    linked_root = tmp_path / "linked"
    try:
        linked_root.symlink_to(real_root, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlinks are unavailable on this host")

    with pytest.raises(SaveSyncError, match="symlinked ancestor"):
        save_transaction.prepare_transaction(
            tmp_path / "transaction.json",
            (save_transaction.SelectedView(linked_root, {}, {}, lambda *_: tmp_path),),
            operation_id=_OPERATION_ID,
        )


def test_broken_journal_symlink_is_not_treated_as_absent(
    tmp_path: Path,
) -> None:
    journal = tmp_path / "transaction.json"
    try:
        journal.symlink_to(tmp_path / "missing-journal-target")
    except OSError:
        pytest.skip("file symlinks are unavailable on this host")

    with pytest.raises(SaveSyncError, match="journal is not a regular file"):
        save_transaction.recover_transaction(
            journal,
            allowed_roots=(tmp_path / "saves",),
            completed_operation_id=None,
        )


def test_durable_writer_fsyncs_file_before_atomic_journal_replace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    journal = tmp_path / "transaction.json"
    journal.write_text("old", encoding="utf-8")
    events: list[str] = []
    real_fsync = save_transaction.os.fsync
    real_replace = save_transaction.os.replace

    def tracked_fsync(descriptor: int) -> None:
        events.append("fsync")
        real_fsync(descriptor)

    def tracked_replace(source: object, destination: object) -> None:
        events.append("replace")
        real_replace(source, destination)

    monkeypatch.setattr(save_transaction.os, "fsync", tracked_fsync)
    monkeypatch.setattr(save_transaction.os, "replace", tracked_replace)

    save_transaction._durable_atomic_write_text(journal, "new")

    assert events[:2] == ["fsync", "replace"]
    assert journal.read_text(encoding="utf-8") == "new"


def test_file_fsync_failure_preserves_existing_journal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    journal = tmp_path / "transaction.json"
    journal.write_text("old", encoding="utf-8")

    def fail_fsync(_descriptor: int) -> None:
        raise OSError("simulated fsync failure")

    monkeypatch.setattr(save_transaction.os, "fsync", fail_fsync)

    with pytest.raises(OSError, match="simulated fsync failure"):
        save_transaction._durable_atomic_write_text(journal, "new")

    assert journal.read_text(encoding="utf-8") == "old"
    assert list(tmp_path.glob(".transaction.json.*")) == []
