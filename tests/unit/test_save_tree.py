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

    def test_real_dolphin_tree_selects_only_audited_gamecube_and_wii_saves(
        self, tmp_path: Path, monkeypatch
    ):
        root = tmp_path / "saves"
        selected = (
            "dolphin-emu/GC/MemoryCardA.USA.raw",
            "dolphin-emu/GC/USA/Card A/01-GAME-progress.gci",
            "dolphin-emu/Wii/title/00010004/524d4345/data/banner.bin",
            "dolphin-emu/Wii/title/00010004/524d4345/data/rksys.dat",
        )
        excluded = (
            "dolphin-emu/Config/Dolphin.ini",
            "dolphin-emu/Cache/Shaders/cache.bin",
            "dolphin-emu/Logs/dolphin.log",
            "dolphin-emu/ScreenShots/RMCE01.png",
            "dolphin-emu/StateSaves/RMCE01.s01",
            "dolphin-emu/Load/Textures/RMCE01/texture.png",
            "dolphin-emu/GameSettings/RMCE01.ini",
            "dolphin-emu/GC/USA/IPL.bin",
            "dolphin-emu/GC/USA/Card A/MC_SYSTEM_AREA",
            "dolphin-emu/GC/USA/Card A/deleted.gci.deleted",
            "dolphin-emu/Wii/shared2/sys/SYSCONF",
            "dolphin-emu/Wii/title/00000001/00000002/data/setting.txt",
            "dolphin-emu/Wii/title/00010002/48414341/data/system-channel.dat",
            "dolphin-emu/Wii/title/00010004/524d4345/content/title.tmd",
        )
        for relative_path in (*selected, *excluded):
            path = root / relative_path
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"data")

        unknown = root / "dolphin-emu/Arbitrary/huge/nested/tree"
        unknown.mkdir(parents=True)
        (unknown / "save.gci").write_bytes(b"unsafe")
        real_iterdir = Path.iterdir

        def guarded_iterdir(path):
            if path == root / "dolphin-emu/Arbitrary":
                raise AssertionError("unknown Dolphin content was traversed")
            return real_iterdir(path)

        monkeypatch.setattr(Path, "iterdir", guarded_iterdir)

        report = save_tree.scan_tree_report(root, DEFAULT_SAVE_SELECTION_POLICY)

        assert set(report.artifacts) == set(selected)
        assert report.excluded_files == 0

    def test_dolphin_save_symlink_is_not_followed(self, tmp_path: Path):
        root = tmp_path / "saves"
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "stolen.dat").write_bytes(b"outside")
        data = root / "dolphin-emu/Wii/title/00010004/524d4345/data"
        data.mkdir(parents=True)
        try:
            (data / "linked").symlink_to(outside, target_is_directory=True)
        except OSError as exc:
            pytest.skip(f"directory symlinks unavailable: {exc}")

        assert save_tree.scan_tree(root, DEFAULT_SAVE_SELECTION_POLICY) == {}

    def test_dolphin_recursive_scan_prunes_symlinked_directories(
        self, tmp_path: Path, monkeypatch
    ):
        root = tmp_path / "saves"
        linked = root / "dolphin-emu/Wii/title/00010004/524d4345/data/linked"
        linked.mkdir(parents=True)
        (linked / "stolen.dat").write_bytes(b"outside")
        real_is_symlink = Path.is_symlink

        def marked_symlink(path):
            return path == linked or real_is_symlink(path)

        monkeypatch.setattr(Path, "is_symlink", marked_symlink)

        assert save_tree.scan_tree(root, DEFAULT_SAVE_SELECTION_POLICY) == {}

    def test_real_n64_tree_selects_progress_and_states_but_not_runtime_artifacts(
        self, tmp_path: Path
    ):
        root = tmp_path / "saves"
        selected = "n64/Dr. Mario 64 (USA).srm"
        states = (
            "n64/Dr. Mario 64 (USA).state",
            "n64/Dr. Mario 64 (USA).st0",
        )
        excluded = (
            "n64/savestates/Dr. Mario 64 (USA).srm",
            "n64/shaders/Dr. Mario 64 (USA).srm",
            "n64/config/Dr. Mario 64 (USA).srm",
            "n64/cache/Dr. Mario 64 (USA).srm",
            "n64/logs/Dr. Mario 64 (USA).srm",
        )
        for relative_path in (selected, *states, *excluded):
            path = root / relative_path
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"data")

        result = save_tree.scan_tree(root, DEFAULT_SAVE_SELECTION_POLICY)

        assert set(result) == {selected, *states}

    def test_rpc3_installed_applications_are_not_traversed_or_reported(
        self, tmp_path: Path
    ):
        root = tmp_path / "saves"
        save = root / "ps3/rpcs3/dev_hdd0/home/00000001/savedata/BLUS1/SAVE.DAT"
        game = root / "ps3/rpcs3/dev_hdd0/game/BLUS1/USRDIR/EBOOT.BIN"
        for path, content in ((save, b"save"), (game, b"large-game")):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)

        report = save_tree.scan_tree_report(root, DEFAULT_SAVE_SELECTION_POLICY)

        assert set(report.artifacts) == {
            "ps3/rpcs3/dev_hdd0/home/00000001/savedata/BLUS1/SAVE.DAT"
        }
        assert report.optional_groups == ()
        assert report.excluded_files == 0

    def test_unknown_system_tree_is_never_entered(self, tmp_path: Path, policy, monkeypatch):
        root = tmp_path / "saves"
        forbidden = root / "totally-unknown" / "large" / "nested"
        forbidden.mkdir(parents=True)
        (forbidden / "payload.bin").write_bytes(b"unsafe")
        real_iterdir = Path.iterdir

        def guarded_iterdir(path):
            if path == root / "totally-unknown":
                raise AssertionError("unknown system was traversed")
            return real_iterdir(path)

        monkeypatch.setattr(Path, "iterdir", guarded_iterdir)

        report = save_tree.scan_tree_report(root, policy)

        assert report.artifacts == {}
        assert report.excluded_files == 0

    def test_arbitrary_nested_content_in_root_file_layout_is_not_entered(
        self, tmp_path: Path, policy, monkeypatch
    ):
        root = tmp_path / "saves"
        selected = root / "psx" / "Game.srm"
        selected.parent.mkdir(parents=True)
        selected.write_bytes(b"progress")
        forbidden = root / "psx" / "unsupported" / "huge" / "tree"
        forbidden.mkdir(parents=True)
        (forbidden / "Game.srm").write_bytes(b"not-eligible")
        real_iterdir = Path.iterdir

        def guarded_iterdir(path):
            if path == root / "psx" / "unsupported":
                raise AssertionError("unsupported nested tree was traversed")
            return real_iterdir(path)

        monkeypatch.setattr(Path, "iterdir", guarded_iterdir)

        report = save_tree.scan_tree_report(root, policy)

        assert set(report.artifacts) == {"psx/Game.srm"}
        assert report.excluded_files == 0

    def test_disabled_xemu_root_is_not_entered(self, tmp_path: Path, policy, monkeypatch):
        root = tmp_path / "saves"
        xbox = root / XBOX_SYSTEM
        xbox.mkdir(parents=True)
        (xbox / "xbox_hdd.qcow2").write_bytes(b"opaque")
        real_iterdir = Path.iterdir

        def guarded_iterdir(path):
            if path == xbox:
                raise AssertionError("disabled xemu root was traversed")
            return real_iterdir(path)

        monkeypatch.setattr(Path, "iterdir", guarded_iterdir)

        assert save_tree.scan_tree(root, policy) == {}

    def test_yuzu_and_rpcs3_dynamic_roots_validate_before_descent(
        self, tmp_path: Path, monkeypatch
    ):
        root = tmp_path / "saves"
        user = "0123456789ABCDEF0123456789ABCDEF"
        title = "0100F2C0115B6000"
        yuzu_save = root / "yuzu" / "0000000000000000" / user / title / "save.dat"
        rpcs3_save = (
            root
            / "ps3/rpcs3/dev_hdd0/home/00000001/savedata/BLUS12345-SAVE/SAVE.DAT"
        )
        for path in (yuzu_save, rpcs3_save):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"progress")
        dangerous = (
            root / "yuzu/cache",
            root / "yuzu/keys",
            root / "ps3/rpcs3/dev_hdd0/game",
            root / "ps3/rpcs3/cache",
        )
        for path in dangerous:
            path.mkdir(parents=True, exist_ok=True)
            (path / "payload.bin").write_bytes(b"unsafe")
        real_iterdir = Path.iterdir

        def guarded_iterdir(path):
            if path in dangerous:
                raise AssertionError(f"dangerous tree was traversed: {path}")
            return real_iterdir(path)

        monkeypatch.setattr(Path, "iterdir", guarded_iterdir)

        report = save_tree.scan_tree_report(root, DEFAULT_SAVE_SELECTION_POLICY)

        assert set(report.artifacts) == {
            yuzu_save.relative_to(root).as_posix(),
            rpcs3_save.relative_to(root).as_posix(),
        }
        assert report.excluded_files == 0

    def test_invalid_yuzu_identity_is_rejected_before_stat_or_descent(
        self, tmp_path: Path, monkeypatch
    ):
        root = tmp_path / "saves"
        invalid = root / "yuzu/0000000000000000/not-a-user-id"
        invalid.mkdir(parents=True)
        (invalid / "payload.bin").write_bytes(b"unsafe")
        real_is_dir = Path.is_dir

        def guarded_is_dir(path):
            if path == invalid:
                raise AssertionError("invalid dynamic segment was inspected")
            return real_is_dir(path)

        monkeypatch.setattr(Path, "is_dir", guarded_is_dir)

        assert save_tree.scan_tree(root, DEFAULT_SAVE_SELECTION_POLICY) == {}

    def test_watch_roots_expose_only_enabled_and_validated_layouts(self, tmp_path: Path):
        root = tmp_path / "saves"
        user = "0123456789ABCDEF0123456789ABCDEF"
        title = "0100F2C0115B6000"
        (root / "snes").mkdir(parents=True)
        (root / "xbox").mkdir()
        (root / "yuzu/0000000000000000" / user / title).mkdir(parents=True)

        default = DEFAULT_SAVE_SELECTION_POLICY.watch_roots(root)
        enabled = DEFAULT_SAVE_SELECTION_POLICY.watch_roots(
            root, enabled_optional_systems=frozenset({XBOX_SYSTEM})
        )

        default_ids = {item.layout_id for item in default}
        assert "retroarch-root-snes" in default_ids
        assert "yuzu-account-title-save" in default_ids
        assert "xemu-hdd" not in default_ids
        assert "xemu-hdd" in {item.layout_id for item in enabled}

    def test_legacy_rpcs3_mapping_resolves_only_save_roots(self, tmp_path: Path):
        root = tmp_path / "dev_hdd0"
        savedata = root / "home/00000001/savedata"
        game = root / "game/BLUS12345"
        savedata.mkdir(parents=True)
        game.mkdir(parents=True)

        roots = DEFAULT_SAVE_SELECTION_POLICY.watch_roots(
            root, canonical_prefix="ps3/rpcs3/dev_hdd0"
        )

        assert {item.canonical_root for item in roots} == {
            "ps3/rpcs3/dev_hdd0/home/00000001/savedata"
        }
        assert all("/game" not in item.canonical_root for item in roots)


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
        assert (tmp_path / "target.previous").is_dir()


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

    def test_stable_previous_generation_is_restored_when_live_tree_is_absent(
        self, tmp_path: Path
    ):
        target = tmp_path / "remote-saves"
        previous = tmp_path / "remote-saves.previous"
        previous.mkdir()
        (previous / "known-good.srm").write_bytes(b"known-good")

        save_tree.recover_interrupted_commit(target)

        assert (target / "known-good.srm").read_bytes() == b"known-good"
        assert not previous.exists()

    def test_stable_previous_generation_is_retained_when_live_tree_exists(
        self, tmp_path: Path
    ):
        target = tmp_path / "remote-saves"
        previous = tmp_path / "remote-saves.previous"
        target.mkdir()
        previous.mkdir()
        (target / "live.srm").write_bytes(b"live")
        (previous / "known-good.srm").write_bytes(b"known-good")

        save_tree.recover_interrupted_commit(target)

        assert (target / "live.srm").read_bytes() == b"live"
        assert (previous / "known-good.srm").read_bytes() == b"known-good"
