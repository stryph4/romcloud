"""Unit tests for romcloud.infrastructure.savesync_index.

The index is shadow/descriptive state in this phase: these tests verify its
own format, generation relationships, and durability guarantees in isolation
— never that any reconciliation decision consults it (see
test_save_sync_service.py's Full Sync integration tests for that boundary).
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from romcloud.core.exceptions import SaveSyncError
from romcloud.infrastructure import savesync_index as index


def _artifact(path: str, content: bytes) -> index.IndexArtifact:
    return index.IndexArtifact(path=path, size_bytes=len(content), sha256=hashlib.sha256(content).hexdigest())


def _group(group_id: str, layout_id: str, *, artifacts=(), tombstoned=False, **kwargs) -> index.IndexGroup:
    return index.IndexGroup(
        group_id=group_id,
        layout_id=layout_id,
        system=layout_id.split("-")[-1] if "-" in layout_id else layout_id,
        group_generation=1,
        artifacts=artifacts,
        tombstoned=tombstoned,
        **kwargs,
    )


class TestManifestHash:
    def test_empty_manifest_has_the_fixed_empty_digest(self):
        assert index.compute_manifest_hash(()) == hashlib.sha256(b"").hexdigest()

    def test_same_content_same_hash_regardless_of_input_order(self):
        a = _artifact("snes/Game.srm", b"one")
        b = _artifact("snes/Game.state0", b"two")
        assert index.compute_manifest_hash((a, b)) == index.compute_manifest_hash((b, a))

    def test_different_content_different_hash(self):
        a = _artifact("snes/Game.srm", b"one")
        b = _artifact("snes/Game.srm", b"different")
        assert index.compute_manifest_hash((a,)) != index.compute_manifest_hash((b,))


class TestShardRoundTrip:
    def test_write_then_load_reproduces_the_same_shard(self, tmp_path: Path):
        root = tmp_path / "savesync-index"
        artifact = _artifact("snes/Game.srm", b"save-bytes")
        group = _group("retroarch-root-snes/game", "retroarch-root-snes", artifacts=(artifact,))
        shard = index.IndexShard(
            schema_version=index.SCHEMA_VERSION,
            dataset_id="dataset-1",
            layout_id="retroarch-root-snes",
            generation=1,
            groups=(group,),
        )

        layout_head = index.write_shard(root, shard)
        loaded = index.load_shard(root, "retroarch-root-snes", layout_head)

        assert loaded == shard

    def test_tombstoned_group_round_trips_as_verified_empty(self, tmp_path: Path):
        root = tmp_path / "savesync-index"
        group = _group("retroarch-root-snes/game", "retroarch-root-snes", tombstoned=True)
        shard = index.IndexShard(
            schema_version=index.SCHEMA_VERSION,
            dataset_id="dataset-1",
            layout_id="retroarch-root-snes",
            generation=1,
            groups=(group,),
        )
        layout_head = index.write_shard(root, shard)
        loaded = index.load_shard(root, "retroarch-root-snes", layout_head)

        assert loaded.groups[0].tombstoned is True
        assert loaded.groups[0].artifacts == ()
        assert loaded.groups[0].manifest_hash == hashlib.sha256(b"").hexdigest()

    def test_group_absent_from_shard_is_distinct_from_tombstoned(self, tmp_path: Path):
        root = tmp_path / "savesync-index"
        present = _group(
            "retroarch-root-snes/gamea", "retroarch-root-snes", artifacts=(_artifact("snes/GameA.srm", b"x"),)
        )
        shard = index.IndexShard(
            schema_version=index.SCHEMA_VERSION,
            dataset_id="dataset-1",
            layout_id="retroarch-root-snes",
            generation=1,
            groups=(present,),
        )
        layout_head = index.write_shard(root, shard)
        loaded = index.load_shard(root, "retroarch-root-snes", layout_head)

        group_ids = {group.group_id for group in loaded.groups}
        assert "retroarch-root-snes/gameb" not in group_ids  # never observed, not tombstoned

    def test_rewriting_the_same_generation_with_identical_content_is_idempotent(self, tmp_path: Path):
        root = tmp_path / "savesync-index"
        shard = index.IndexShard(
            schema_version=index.SCHEMA_VERSION, dataset_id="dataset-1",
            layout_id="retroarch-root-snes", generation=1, groups=(),
        )
        first = index.write_shard(root, shard)
        second = index.write_shard(root, shard)
        assert first == second

    def test_rewriting_the_same_generation_with_different_content_is_rejected(self, tmp_path: Path):
        root = tmp_path / "savesync-index"
        shard = index.IndexShard(
            schema_version=index.SCHEMA_VERSION, dataset_id="dataset-1",
            layout_id="retroarch-root-snes", generation=1, groups=(),
        )
        index.write_shard(root, shard)
        mutated = index.IndexShard(
            schema_version=index.SCHEMA_VERSION, dataset_id="dataset-1",
            layout_id="retroarch-root-snes", generation=1,
            groups=(_group("retroarch-root-snes/game", "retroarch-root-snes"),),
        )
        with pytest.raises(SaveSyncError, match="already exists with different content"):
            index.write_shard(root, mutated)

    def test_digest_mismatch_on_disk_is_rejected(self, tmp_path: Path):
        root = tmp_path / "savesync-index"
        shard = index.IndexShard(
            schema_version=index.SCHEMA_VERSION, dataset_id="dataset-1",
            layout_id="retroarch-root-snes", generation=1, groups=(),
        )
        layout_head = index.write_shard(root, shard)
        path = root / "layouts" / layout_head.object
        path.write_text(path.read_text().replace("dataset-1", "dataset-tampered"))

        with pytest.raises(SaveSyncError, match="digest mismatch"):
            index.load_shard(root, "retroarch-root-snes", layout_head)


class TestShardValidation:
    def _valid_payload(self) -> dict:
        return {
            "schema_version": index.SCHEMA_VERSION,
            "dataset_id": "dataset-1",
            "layout_id": "retroarch-root-snes",
            "generation": 1,
            "groups": {
                "retroarch-root-snes/game": {
                    "layout_id": "retroarch-root-snes",
                    "system": "snes",
                    "group_generation": 1,
                    "manifest_hash": hashlib.sha256(b"").hexdigest(),
                    "artifacts": [],
                    "tombstoned": False,
                    "origin_device": "",
                    "completed_transaction_id": None,
                    "container_head": None,
                }
            },
        }

    def test_unsupported_schema_version_is_rejected(self, tmp_path: Path):
        payload = self._valid_payload()
        payload["schema_version"] = 999
        with pytest.raises(SaveSyncError, match="schema version"):
            index.validate_shard_document(payload, path=tmp_path / "x.json")

    def test_group_id_must_belong_to_its_layout(self, tmp_path: Path):
        payload = self._valid_payload()
        payload["groups"] = {"other-layout/game": payload["groups"]["retroarch-root-snes/game"]}
        with pytest.raises(SaveSyncError, match="does not belong to layout"):
            index.validate_shard_document(payload, path=tmp_path / "x.json")

    def test_manifest_hash_mismatch_is_rejected(self, tmp_path: Path):
        payload = self._valid_payload()
        payload["groups"]["retroarch-root-snes/game"]["manifest_hash"] = "1" * 64
        with pytest.raises(SaveSyncError, match="manifest_hash does not match"):
            index.validate_shard_document(payload, path=tmp_path / "x.json")

    def test_duplicate_artifact_paths_are_rejected(self, tmp_path: Path):
        payload = self._valid_payload()
        artifact = {"path": "snes/Game.srm", "size_bytes": 1, "sha256": "a" * 64}
        payload["groups"]["retroarch-root-snes/game"]["artifacts"] = [artifact, dict(artifact)]
        payload["groups"]["retroarch-root-snes/game"]["manifest_hash"] = index.compute_manifest_hash(
            (index.IndexArtifact("snes/Game.srm", 1, "a" * 64), index.IndexArtifact("snes/Game.srm", 1, "a" * 64))
        )
        with pytest.raises(SaveSyncError, match="duplicate artifact path"):
            index.validate_shard_document(payload, path=tmp_path / "x.json")

    def test_artifact_path_cannot_escape_its_system(self, tmp_path: Path):
        payload = self._valid_payload()
        artifact = {"path": "other-system/private.bin", "size_bytes": 1, "sha256": "a" * 64}
        payload["groups"]["retroarch-root-snes/game"]["artifacts"] = [artifact]
        payload["groups"]["retroarch-root-snes/game"]["manifest_hash"] = index.compute_manifest_hash(
            (index.IndexArtifact("other-system/private.bin", 1, "a" * 64),)
        )
        with pytest.raises(SaveSyncError, match="outside its group's system"):
            index.validate_shard_document(payload, path=tmp_path / "x.json")

    @pytest.mark.parametrize("path", ["../escape", "/absolute", "snes/../escape", "snes/"])
    def test_artifact_path_traversal_is_rejected(self, path: str, tmp_path: Path):
        payload = self._valid_payload()
        artifact = {"path": path, "size_bytes": 1, "sha256": "a" * 64}
        payload["groups"]["retroarch-root-snes/game"]["artifacts"] = [artifact]
        payload["groups"]["retroarch-root-snes/game"]["manifest_hash"] = "0" * 64
        with pytest.raises(SaveSyncError):
            index.validate_shard_document(payload, path=tmp_path / "x.json")

    def test_tombstoned_group_with_artifacts_is_rejected(self, tmp_path: Path):
        payload = self._valid_payload()
        artifact = {"path": "snes/Game.srm", "size_bytes": 1, "sha256": "a" * 64}
        payload["groups"]["retroarch-root-snes/game"]["artifacts"] = [artifact]
        payload["groups"]["retroarch-root-snes/game"]["tombstoned"] = True
        payload["groups"]["retroarch-root-snes/game"]["manifest_hash"] = index.compute_manifest_hash(
            (index.IndexArtifact("snes/Game.srm", 1, "a" * 64),)
        )
        with pytest.raises(SaveSyncError, match="cannot be tombstoned"):
            index.validate_shard_document(payload, path=tmp_path / "x.json")

    def test_corrupt_json_fails_closed_without_deleting_the_file(self, tmp_path: Path):
        path = tmp_path / "layouts" / "retroarch-root-snes.1.json"
        path.parent.mkdir(parents=True)
        path.write_text("{not valid json")

        with pytest.raises(SaveSyncError):
            index.load_shard(
                tmp_path,
                "retroarch-root-snes",
                index.IndexLayoutHead(1, "retroarch-root-snes.1.json", "0" * 64),
            )
        assert path.exists()
        assert path.read_text() == "{not valid json"


class TestHeadDurability:
    def _head(self, **overrides) -> index.IndexHead:
        base = dict(
            schema_version=index.SCHEMA_VERSION,
            dataset_id="dataset-1",
            index_generation=1,
            journal_generation=0,
            layouts={
                "retroarch-root-snes": index.IndexLayoutHead(1, "retroarch-root-snes.1.json", "0" * 64)
            },
        )
        base.update(overrides)
        return index.IndexHead(**base)

    def test_write_then_load_round_trips(self, tmp_path: Path):
        root = tmp_path / "savesync-index"
        head = self._head()
        index.write_head(root, head)
        loaded = index.load_head(root)
        assert loaded == head

    def test_missing_head_is_none_not_an_error(self, tmp_path: Path):
        assert index.load_head(tmp_path / "savesync-index") is None
        assert index.load_head_safe(tmp_path / "savesync-index") is None

    def test_second_publish_retains_previous_good_head(self, tmp_path: Path):
        root = tmp_path / "savesync-index"
        first = self._head(index_generation=1)
        index.write_head(root, first)
        second = self._head(index_generation=2)
        index.write_head(root, second)

        assert index.load_head(root) == second
        assert index.load_previous_head_safe(root) == first

    def test_corrupt_head_is_treated_as_absent_never_raised_by_safe_loader(self, tmp_path: Path):
        root = tmp_path / "savesync-index"
        root.mkdir(parents=True)
        (root / "HEAD.json").write_text("not json")

        assert index.load_head_safe(root) is None
        with pytest.raises(SaveSyncError):
            index.load_head(root)
        # Unlike the journal's load_or_reset, this must never rewrite/delete.
        assert (root / "HEAD.json").read_text() == "not json"

    def test_unknown_schema_version_does_not_destroy_the_file(self, tmp_path: Path):
        root = tmp_path / "savesync-index"
        root.mkdir(parents=True)
        payload = index.head_to_dict(self._head())
        payload["schema_version"] = 999
        (root / "HEAD.json").write_text(json.dumps(payload))

        assert index.load_head_safe(root) is None
        assert json.loads((root / "HEAD.json").read_text())["schema_version"] == 999

    def test_journal_diverged_detects_old_client_writes(self):
        head = self._head(journal_generation=5)
        assert index.journal_diverged(head, 5) is False
        assert index.journal_diverged(head, 6) is True


class TestLoadChangedLayoutShards:
    def test_only_advanced_layouts_are_loaded(self, tmp_path: Path):
        root = tmp_path / "savesync-index"
        snes_shard = index.IndexShard(index.SCHEMA_VERSION, "dataset-1", "retroarch-root-snes", 2, ())
        gba_shard = index.IndexShard(index.SCHEMA_VERSION, "dataset-1", "retroarch-root-gba", 1, ())
        snes_head = index.write_shard(root, snes_shard)
        gba_head = index.write_shard(root, gba_shard)
        head = index.IndexHead(
            index.SCHEMA_VERSION, "dataset-1", 2, 0,
            layouts={"retroarch-root-snes": snes_head, "retroarch-root-gba": gba_head},
        )

        changed = index.load_changed_layout_shards(
            root, head, known_generations={"retroarch-root-snes": 1, "retroarch-root-gba": 1}
        )

        assert set(changed) == {"retroarch-root-snes"}
        assert changed["retroarch-root-snes"].generation == 2


class TestPublishFullSyncIndex:
    def test_first_publish_creates_generation_one_for_every_layout(self, tmp_path: Path):
        root = tmp_path / "savesync-index"
        layout_groups = {
            "retroarch-root-snes": (
                _group("retroarch-root-snes/game", "retroarch-root-snes", artifacts=(_artifact("snes/Game.srm", b"x"),)),
            ),
        }

        head = index.publish_full_sync_index(
            root, dataset_id_hint=None, journal_generation=3, layout_groups=layout_groups
        )

        assert head is not None
        assert head.index_generation == 1
        assert head.journal_generation == 3
        assert head.layouts["retroarch-root-snes"].generation == 1
        assert index.load_head(root) == head

    def test_republish_with_unchanged_content_does_not_bump_generation(self, tmp_path: Path):
        root = tmp_path / "savesync-index"
        layout_groups = {
            "retroarch-root-snes": (
                _group("retroarch-root-snes/game", "retroarch-root-snes", artifacts=(_artifact("snes/Game.srm", b"x"),)),
            ),
        }
        first = index.publish_full_sync_index(
            root, dataset_id_hint=None, journal_generation=1, layout_groups=layout_groups
        )
        assert first is not None

        second = index.publish_full_sync_index(
            root, dataset_id_hint=None, journal_generation=1, layout_groups=layout_groups
        )

        assert second is None
        assert index.load_head(root) == first

    def test_changed_group_bumps_layout_and_index_generation(self, tmp_path: Path):
        root = tmp_path / "savesync-index"
        unchanged_layout = {
            "retroarch-root-gba": (
                _group("retroarch-root-gba/game", "retroarch-root-gba", artifacts=(_artifact("gba/Game.srm", b"g"),)),
            ),
        }
        changing_layout = {
            "retroarch-root-snes": (
                _group("retroarch-root-snes/game", "retroarch-root-snes", artifacts=(_artifact("snes/Game.srm", b"x"),)),
            ),
        }
        first = index.publish_full_sync_index(
            root, dataset_id_hint=None, journal_generation=1,
            layout_groups={**unchanged_layout, **changing_layout},
        )
        assert first is not None

        changed_layout = {
            "retroarch-root-snes": (
                _group("retroarch-root-snes/game", "retroarch-root-snes", artifacts=(_artifact("snes/Game.srm", b"changed"),)),
            ),
        }
        second = index.publish_full_sync_index(
            root, dataset_id_hint=None, journal_generation=2,
            layout_groups={**unchanged_layout, **changed_layout},
        )

        assert second is not None
        assert second.index_generation == first.index_generation + 1
        assert second.layouts["retroarch-root-snes"].generation == 2
        # The untouched layout's pointer is carried forward unchanged.
        assert second.layouts["retroarch-root-gba"] == first.layouts["retroarch-root-gba"]

    def test_dataset_id_is_preserved_across_republish(self, tmp_path: Path):
        root = tmp_path / "savesync-index"
        layout_groups = {"retroarch-root-snes": (_group("retroarch-root-snes/game", "retroarch-root-snes"),)}
        first = index.publish_full_sync_index(
            root, dataset_id_hint=None, journal_generation=0, layout_groups=layout_groups
        )
        changed = {
            "retroarch-root-snes": (
                _group(
                    "retroarch-root-snes/game", "retroarch-root-snes",
                    artifacts=(_artifact("snes/Game.srm", b"new"),),
                ),
            )
        }
        second = index.publish_full_sync_index(
            root, dataset_id_hint="ignored-because-head-already-exists", journal_generation=1, layout_groups=changed
        )

        assert first is not None and second is not None
        assert second.dataset_id == first.dataset_id
