"""Unit tests for romcloud.core.save_selection (pure logic, no filesystem)."""

from __future__ import annotations

from romcloud.core.save_selection import (
    DEFAULT_SAVE_SELECTION_POLICY,
    XBOX_HDD_RELATIVE_PATH,
    XBOX_SYSTEM,
    SaveSelectionPolicy,
    SaveSystemRule,
)


class TestKnownSystems:
    def test_validated_systems_are_known(self):
        policy = DEFAULT_SAVE_SELECTION_POLICY
        for system in ("psx", "duckstation", "pcsx2", "ppsspp", "xbox360", "yuzu", "xbox"):
            assert policy.is_known_system(system) is True

    def test_unvalidated_systems_are_unsupported(self):
        """3DS/Citra and Dolphin require system-specific selection that
        hasn't been validated yet — they must stay unsupported, not
        blindly included."""
        policy = DEFAULT_SAVE_SELECTION_POLICY
        for system in ("3ds", "citra", "dolphin", "gamecube", "totally-unknown-system"):
            assert policy.is_known_system(system) is False
            assert policy.is_included(system, "anything.bin") is False


class TestPS1Native:
    def test_srm_included(self):
        assert DEFAULT_SAVE_SELECTION_POLICY.is_included("psx", "Game.srm") is True

    def test_non_srm_excluded(self):
        assert DEFAULT_SAVE_SELECTION_POLICY.is_included("psx", "Game.mcr") is False


class TestDuckstation:
    def test_memcard_included(self):
        policy = DEFAULT_SAVE_SELECTION_POLICY
        assert policy.is_included("duckstation", "memcards/shared_card_1.mcd") is True

    def test_resume_save_excluded_even_under_memcards(self):
        policy = DEFAULT_SAVE_SELECTION_POLICY
        assert policy.is_included("duckstation", "memcards/Game_resume.sav") is False

    def test_outside_memcards_excluded(self):
        policy = DEFAULT_SAVE_SELECTION_POLICY
        assert policy.is_included("duckstation", "shadercache/foo.bin") is False


class TestPCSX2:
    def test_memory_card_included(self):
        policy = DEFAULT_SAVE_SELECTION_POLICY
        assert policy.is_included("pcsx2", "Mcd001.ps2") is True

    def test_sstates_excluded(self):
        policy = DEFAULT_SAVE_SELECTION_POLICY
        assert policy.is_included("pcsx2", "sstates/Game.p2s") is False

    def test_videos_excluded(self):
        policy = DEFAULT_SAVE_SELECTION_POLICY
        assert policy.is_included("pcsx2", "videos/Game.mp4") is False


class TestPPSSPP:
    def test_savedata_included(self):
        policy = DEFAULT_SAVE_SELECTION_POLICY
        assert policy.is_included("ppsspp", "PSP/SAVEDATA/ULUS12345/save.bin") is True

    def test_state_excluded(self):
        policy = DEFAULT_SAVE_SELECTION_POLICY
        assert policy.is_included("ppsspp", "PPSSPP_STATE/ULUS12345_1.ppst") is False


class TestGenericWholeTreeSystems:
    def test_xbox360_tree_included(self):
        policy = DEFAULT_SAVE_SELECTION_POLICY
        assert policy.is_included("xbox360", "0000000000000000/save/data.bin") is True

    def test_yuzu_tree_included(self):
        policy = DEFAULT_SAVE_SELECTION_POLICY
        assert policy.is_included("yuzu", "nand/user/save/0/data") is True


class TestFlatpakExclusion:
    def test_flatpak_is_never_a_system(self):
        policy = DEFAULT_SAVE_SELECTION_POLICY
        assert "flatpak" in policy.excluded_top_level_dirs()
        assert policy.is_known_system("flatpak") is False


class TestXboxOptIn:
    def test_xbox_is_optional_and_disabled_by_default(self):
        policy = DEFAULT_SAVE_SELECTION_POLICY
        assert policy.is_optional(XBOX_SYSTEM) is True
        assert policy.default_enabled(XBOX_SYSTEM) is False

    def test_non_optional_systems_default_enabled_true(self):
        policy = DEFAULT_SAVE_SELECTION_POLICY
        assert policy.default_enabled("psx") is True
        assert policy.is_optional("psx") is False

    def test_hdd_relative_path_included_when_present(self):
        policy = DEFAULT_SAVE_SELECTION_POLICY
        assert policy.is_included(XBOX_SYSTEM, XBOX_HDD_RELATIVE_PATH) is True

    def test_other_xbox_files_not_included(self):
        policy = DEFAULT_SAVE_SELECTION_POLICY
        assert policy.is_included(XBOX_SYSTEM, "other_file.bin") is False


class TestExtensibility:
    def test_policy_accepts_custom_rule_table(self):
        """A later emulator layout can be validated and added without
        touching any consumer of SaveSelectionPolicy."""
        custom = SaveSelectionPolicy({"newsystem": SaveSystemRule(include=("*.sav",))})
        assert custom.is_known_system("newsystem") is True
        assert custom.is_included("newsystem", "profile.sav") is True
        assert custom.is_included("newsystem", "profile.bin") is False
        # The default global policy is unaffected by a custom instance.
        assert DEFAULT_SAVE_SELECTION_POLICY.is_known_system("newsystem") is False
