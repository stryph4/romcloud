"""Unit tests for romcloud.infrastructure.save_tree (real filesystem I/O)."""

from __future__ import annotations

from pathlib import Path

import pytest

from romcloud.infrastructure import save_tree
from romcloud.core.exceptions import SaveSyncError
from romcloud.core.save_selection import (
    DEFAULT_SAVE_SELECTION_POLICY,
    SaveSelectionPolicy,
    SaveSystemRule,
    XBOX_SYSTEM,
)


@pytest.fixture
def policy() -> SaveSelectionPolicy:
    return SaveSelectionPolicy({
        "psx": SaveSystemRule(include=("*.srm",)),
        "duckstation": SaveSystemRule(include=("memcards/**",), exclude=("*_resume.sav",)),
        XBOX_SYSTEM: SaveSystemRule(include=("xbox_hdd.qcow2",), optional=True, default_enabled=False),
    })


class TestHashFile:
    def test_hash_is_deterministic(self, tmp_path: Path):
        f = tmp_path / "a.bin"
        f.write_bytes(b"hello world" * 100)
        assert save_tree.hash_file(f) == save_tree.hash_file(f)

    def test_different_content_different_hash(self, tmp_path: Path):
        a = tmp_path / "a.bin"
        b = tmp_path / "b.bin"
        a.write_bytes(b"one")
        b.write_bytes(b"two")
        assert save_tree.hash_file(a) != save_tree.hash_file(b)


class TestScanTree:
    def test_missing_root_returns_empty(self, tmp_path: Path, policy):
        result = save_tree.scan_tree(tmp_path / "nonexistent", policy)
        assert result == {}

    def test_selects_only_policy_matched_files(self, tmp_path: Path, policy):
        root = tmp_path / "saves"
        (root / "psx").mkdir(parents=True)
        (root / "psx" / "Game.srm").write_bytes(b"srm-data")
        (root / "psx" / "Game.mcr").write_bytes(b"not-included")

        result = save_tree.scan_tree(root, policy)

        assert set(result) == {"psx/Game.srm"}
        assert result["psx/Game.srm"].size_bytes == len(b"srm-data")

    def test_excludes_resume_saves_under_memcards(self, tmp_path: Path, policy):
        root = tmp_path / "saves"
        (root / "duckstation" / "memcards").mkdir(parents=True)
        (root / "duckstation" / "memcards" / "card1.mcd").write_bytes(b"card")
        (root / "duckstation" / "memcards" / "Game_resume.sav").write_bytes(b"resume")

        result = save_tree.scan_tree(root, policy)

        assert set(result) == {"duckstation/memcards/card1.mcd"}

    def test_unknown_system_dir_ignored(self, tmp_path: Path, policy):
        root = tmp_path / "saves"
        (root / "dolphin").mkdir(parents=True)
        (root / "dolphin" / "GameSave.gci").write_bytes(b"x")

        result = save_tree.scan_tree(root, policy)

        assert result == {}

    def test_optional_system_excluded_unless_enabled(self, tmp_path: Path, policy):
        root = tmp_path / "saves"
        (root / XBOX_SYSTEM).mkdir(parents=True)
        (root / XBOX_SYSTEM / "xbox_hdd.qcow2").write_bytes(b"vhd" * 1000)

        assert save_tree.scan_tree(root, policy) == {}
        enabled = save_tree.scan_tree(root, policy, enabled_optional_systems=frozenset({XBOX_SYSTEM}))
        assert set(enabled) == {"xbox/xbox_hdd.qcow2"}

    def test_flatpak_top_level_dir_skipped(self, tmp_path: Path, policy):
        root = tmp_path / "saves"
        (root / "flatpak").mkdir(parents=True)
        (root / "flatpak" / "whatever.dat").write_bytes(b"x")

        assert save_tree.scan_tree(root, policy) == {}

    def test_real_yuzu_tree_selects_only_account_title_saves(self, tmp_path: Path):
        root = tmp_path / "saves"
        user = "0123456789ABCDEF0123456789ABCDEF"
        title = "0100F2C0115B6000"
        selected = f"yuzu/0000000000000000/{user}/{title}/slot 1/progress.dat"
        excluded = (
            "yuzu/keys/prod.keys",
            "yuzu/cache/0100F2C0115B6000/index.bin",
            "yuzu/nand/system/Contents/registered/0123456789abcdef.nca",
            "yuzu/nand/system/Contents/registered/0123456789abcdef.cnmt.nca",
            "yuzu/shader/0100F2C0115B6000/vulkan.bin",
            "yuzu/log/yuzu_log.txt",
            "yuzu/preview.pv.txt",
        )
        for relative_path in (selected, *excluded):
            path = root / relative_path
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"data")

        result = save_tree.scan_tree(root, DEFAULT_SAVE_SELECTION_POLICY)

        assert set(result) == {selected}

    def test_real_n64_tree_selects_dr_mario_but_not_states_or_runtime_artifacts(
        self, tmp_path: Path
    ):
        root = tmp_path / "saves"
        selected = "n64/Dr. Mario 64 (USA).srm"
        excluded = (
            "n64/Dr. Mario 64 (USA).state",
            "n64/Dr. Mario 64 (USA).st0",
            "n64/savestates/Dr. Mario 64 (USA).srm",
            "n64/shaders/Dr. Mario 64 (USA).srm",
            "n64/config/Dr. Mario 64 (USA).srm",
            "n64/cache/Dr. Mario 64 (USA).srm",
            "n64/logs/Dr. Mario 64 (USA).srm",
        )
        for relative_path in (selected, *excluded):
            path = root / relative_path
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"data")

        result = save_tree.scan_tree(root, DEFAULT_SAVE_SELECTION_POLICY)

        assert set(result) == {selected}


class TestMaterialize:
    def test_copies_from_fresh_source_when_no_unchanged_source(self, tmp_path: Path):
        src = tmp_path / "src.bin"
        src.write_bytes(b"payload")
        dest = tmp_path / "nested" / "dest.bin"

        save_tree.materialize(dest, fresh_source=src)

        assert dest.read_bytes() == b"payload"

    def test_hardlinks_from_unchanged_source_when_given(self, tmp_path: Path):
        src = tmp_path / "src.bin"
        src.write_bytes(b"unused-if-hardlinked")
        old = tmp_path / "old.bin"
        old.write_bytes(b"unchanged-content")
        dest = tmp_path / "dest.bin"

        save_tree.materialize(dest, fresh_source=src, unchanged_source=old)

        assert dest.read_bytes() == b"unchanged-content"
        assert dest.stat().st_ino == old.stat().st_ino  # a true hardlink, not a copy

    def test_falls_back_to_fresh_source_when_unchanged_source_missing(self, tmp_path: Path):
        src = tmp_path / "src.bin"
        src.write_bytes(b"fresh-content")
        dest = tmp_path / "dest.bin"

        save_tree.materialize(dest, fresh_source=src, unchanged_source=tmp_path / "does-not-exist.bin")

        assert dest.read_bytes() == b"fresh-content"


class TestAtomicReplaceDir:
    def test_creates_target_when_missing(self, tmp_path: Path):
        new_dir = tmp_path / "new"
        new_dir.mkdir()
        (new_dir / "a.txt").write_text("a")
        target = tmp_path / "target"

        save_tree.atomic_replace_dir(new_dir, target)

        assert (target / "a.txt").read_text() == "a"
        assert not new_dir.exists()

    def test_swaps_and_removes_old_content(self, tmp_path: Path):
        target = tmp_path / "target"
        target.mkdir()
        (target / "old.txt").write_text("old")
        new_dir = tmp_path / "new"
        new_dir.mkdir()
        (new_dir / "new.txt").write_text("new")

        save_tree.atomic_replace_dir(new_dir, target)

        assert (target / "new.txt").read_text() == "new"
        assert not (target / "old.txt").exists()
        assert not new_dir.exists()

    def test_restores_original_if_final_rename_fails(self, tmp_path: Path, monkeypatch):
        target = tmp_path / "target"
        target.mkdir()
        (target / "old.txt").write_text("old")
        new_dir = tmp_path / "new"
        new_dir.mkdir()
        (new_dir / "new.txt").write_text("new")

        real_rename = save_tree.os.rename
        calls = {"count": 0}

        def flaky_rename(src, dst):
            calls["count"] += 1
            if calls["count"] == 2:
                raise OSError("simulated failure renaming staging into place")
            real_rename(src, dst)

        monkeypatch.setattr(save_tree.os, "rename", flaky_rename)

        with pytest.raises(OSError):
            save_tree.atomic_replace_dir(new_dir, target)

        assert (target / "old.txt").read_text() == "old"

    def test_reports_backup_location_if_commit_and_restore_both_fail(
        self, tmp_path: Path, monkeypatch
    ):
        target = tmp_path / "target"
        target.mkdir()
        (target / "old.txt").write_text("old")
        new_dir = tmp_path / "new"
        new_dir.mkdir()

        real_rename = save_tree.os.rename
        calls = {"count": 0}

        def fail_commit_and_restore(src, dst):
            calls["count"] += 1
            if calls["count"] >= 2:
                raise OSError("simulated rename failure")
            real_rename(src, dst)

        monkeypatch.setattr(save_tree.os, "rename", fail_commit_and_restore)

        with pytest.raises(SaveSyncError, match="remains at"):
            save_tree.atomic_replace_dir(new_dir, target)

        assert not target.exists()
        assert len(list(tmp_path.glob("target.previous-*"))) == 1


class TestRecoverInterruptedCommit:
    def test_restores_previous_dataset_when_interrupted_between_renames(self, tmp_path: Path):
        target = tmp_path / "remote-saves"
        backup = tmp_path / "remote-saves.previous-deadbeef"
        backup.mkdir()
        (backup / "old.srm").write_bytes(b"complete-old-dataset")
        staging = tmp_path / ".remote-saves.staging-cafebabe"
        staging.mkdir()
        (staging / "new.srm").write_bytes(b"incomplete-new-dataset")

        save_tree.recover_interrupted_commit(target)

        assert (target / "old.srm").read_bytes() == b"complete-old-dataset"
        assert not backup.exists()
        assert not staging.exists()

    def test_live_dataset_wins_and_transaction_debris_is_cleaned(self, tmp_path: Path):
        target = tmp_path / "remote-saves"
        target.mkdir()
        (target / "live.srm").write_bytes(b"live")
        backup = tmp_path / "remote-saves.previous-deadbeef"
        backup.mkdir()
        staging = tmp_path / ".remote-saves.staging-cafebabe"
        staging.mkdir()

        save_tree.recover_interrupted_commit(target)

        assert (target / "live.srm").read_bytes() == b"live"
        assert not backup.exists()
        assert not staging.exists()

    def test_ambiguous_multiple_backups_are_preserved(self, tmp_path: Path):
        target = tmp_path / "remote-saves"
        for suffix in ("one", "two"):
            (tmp_path / f"remote-saves.previous-{suffix}").mkdir()

        with pytest.raises(SaveSyncError, match="found 2 previous datasets"):
            save_tree.recover_interrupted_commit(target)

        assert len(list(tmp_path.glob("remote-saves.previous-*"))) == 2
