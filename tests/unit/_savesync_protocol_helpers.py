"""Shared test helpers for the SaveSync remote commit protocol.

These build *realistic* remote dataset states rather than poking at internals:
:func:`seed_peer_commit` leaves the dataset exactly as another new-protocol
device's committed upload would, and :func:`mutate_remote_out_of_band` leaves
it exactly as a manual edit or an old, non-participating client would.
Keeping those two situations distinguishable is the whole point of the
ownership/CAS design, so tests must be able to construct each precisely.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from romcloud.infrastructure import savesync_index
from romcloud.services.saves import SaveSyncService


def index_root_for(remote_root: Path) -> Path:
    return savesync_index.default_index_root(Path(remote_root))


def seed_peer_commit(
    service: SaveSyncService,
    *,
    remote_root: Path,
    relative_path: str,
    content: bytes | None,
    device_id: str = "peer-device",
) -> savesync_index.IndexHead:
    """Commit *relative_path* on the remote as a peer new-protocol device would.

    Writes the payload and publishes the matching authoritative index entry
    through the real publication primitive, so the resulting dataset is
    indistinguishable from one another upgraded device produced. ``content``
    of ``None`` commits a deletion (a verified-empty, tombstoned group).
    """
    remote_root = Path(remote_root)
    target = remote_root / relative_path
    if content is None:
        target.unlink(missing_ok=True)
    else:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)

    descriptor = service.selection_policy.group_for_path(relative_path)
    assert descriptor is not None, f"{relative_path} is not a registered save path"
    artifacts: tuple[savesync_index.IndexArtifact, ...] = ()
    if content is not None:
        artifacts = (
            savesync_index.IndexArtifact(
                relative_path,
                len(content),
                hashlib.sha256(content).hexdigest(),
            ),
        )
    index_root = index_root_for(remote_root)
    return savesync_index.publish_group_updates(
        index_root,
        dataset_id_hint=None,
        journal_generation=_journal_generation(remote_root),
        updated_groups=(
            savesync_index.IndexGroup(
                group_id=descriptor.group_id,
                layout_id=descriptor.layout_id,
                system=descriptor.system,
                group_generation=0,
                artifacts=artifacts,
                tombstoned=not artifacts,
                origin_device=device_id,
            ),
        ),
        operation_id=f"peer-{device_id}-{descriptor.group_id}",
        origin_device=device_id,
    )


def _journal_generation(remote_root: Path) -> int:
    from romcloud.infrastructure import savesync_journal

    path = savesync_journal.default_journal_path(Path(remote_root))
    try:
        return int(savesync_journal.load(path)["generation"])
    except Exception:  # noqa: BLE001 - absent/unreadable journal is generation 0
        return 0


def publish_current_remote_as_peer(
    service: SaveSyncService,
    *,
    remote_root: Path,
    device_id: str = "peer-device",
) -> savesync_index.IndexHead | None:
    """Make the index describe whatever is currently on the remote.

    Equivalent to every current remote group having been committed by an
    upgraded peer. Use this after a test constructs remote divergence by
    writing bytes directly, when the *subject* of the test is reconciliation
    behavior rather than out-of-band divergence handling.
    """
    remote_root = Path(remote_root)
    policy = service.selection_policy
    report = service._scan_remote_layouts(
        frozenset(layout.layout_id for layout in policy.layouts)
    )
    grouped: dict[str, list[savesync_index.IndexArtifact]] = {}
    descriptors = {}
    for path, artifact in sorted(report.artifacts.items()):
        descriptor = policy.group_for_path(path)
        if descriptor is None:
            continue
        descriptors[descriptor.group_id] = descriptor
        grouped.setdefault(descriptor.group_id, []).append(
            savesync_index.IndexArtifact(
                artifact.relative_path, artifact.size_bytes, artifact.content_hash
            )
        )
    if not grouped:
        return None
    updates = tuple(
        savesync_index.IndexGroup(
            group_id=group_id,
            layout_id=descriptors[group_id].layout_id,
            system=descriptors[group_id].system,
            group_generation=0,
            artifacts=tuple(artifacts),
            tombstoned=not artifacts,
            origin_device=device_id,
        )
        for group_id, artifacts in sorted(grouped.items())
    )
    return savesync_index.publish_group_updates(
        index_root_for(remote_root),
        dataset_id_hint=None,
        journal_generation=_journal_generation(remote_root),
        updated_groups=updates,
        operation_id=f"peer-{device_id}-bulk",
        origin_device=device_id,
    )


def mutate_remote_out_of_band(remote_root: Path, relative_path: str, content: bytes | None) -> None:
    """Change remote payload without touching the index.

    Represents a manual filesystem edit or an old client that does not
    participate in the commit protocol.
    """
    target = Path(remote_root) / relative_path
    if content is None:
        target.unlink(missing_ok=True)
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(content)


def strip_protocol_ownership(remote_root: Path, data_root: Path) -> None:
    """Return a dataset to pre-cutover (UNOWNED) state for legacy testing.

    Removes both the remote ownership marker and this device's local record
    of having seen it, so the result is a dataset that was simply never cut
    over rather than one whose marker suspiciously disappeared.
    """
    savesync_index.ownership_path(index_root_for(remote_root)).unlink(missing_ok=True)
    (Path(data_root) / "savesync-protocol.json").unlink(missing_ok=True)
