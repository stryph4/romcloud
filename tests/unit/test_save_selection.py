"""Unit tests for romcloud.core.save_selection (pure logic, no filesystem)."""

from __future__ import annotations

from romcloud.core.save_selection import (
    DEFAULT_SAVE_SELECTION_POLICY,
    XBOX_HDD_RELATIVE_PATH,
    XBOX_SYSTEM,
    YUZU_ACCOUNT_SAVE_GLOB,
    SaveSelectionPolicy,
    SaveSystemRule,
    RPCS3_INSTALLED_GAMES_GROUP,
)


class TestKnownSystems:
    def test_validated_systems_are_known(self):
        policy = DEFAULT_SAVE_SELECTION_POLICY
        for system in (
            "n64",
            "gb",
            "gba",
            "nes",
            "snes",
            "psx",
            "duckstation",
            "pcsx2",
            "ppsspp",
            "xbox360",
            "yuzu",
            "xbox",
            "ps2",
            "ps3",
        ):
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


class TestRetroArchNativeSaves:
    def test_root_srm_included_for_audited_batocera_systems(self):
        policy = DEFAULT_SAVE_SELECTION_POLICY
        for system in (
            "gb",
            "gbc",
            "gba",
            "nes",
            "snes",
            "megadrive",
            "dreamcast",
        ):
            assert policy.is_included(system, "Game.srm") is True

    def test_srm_rule_is_root_only(self):
        policy = DEFAULT_SAVE_SELECTION_POLICY
        for relative_path in (
            "states/Game.srm",
            "savestates/Game.srm",
            "shaders/Game.srm",
            "config/Game.srm",
            "cache/Game.srm",
            "logs/Game.srm",
        ):
            assert policy.is_included("snes", relative_path) is False

    def test_savestate_included_but_non_progress_formats_excluded(self):
        policy = DEFAULT_SAVE_SELECTION_POLICY
        assert policy.is_included("snes", "Game.state") is True
        assert policy.is_included("snes", "Game.state.auto") is True
        for relative_path in (
            "Game.cfg",
            "retroarch.log",
        ):
            assert policy.is_included("snes", relative_path) is False


class TestN64Native:
    def test_dr_mario_srm_included(self):
        assert DEFAULT_SAVE_SELECTION_POLICY.is_included(
            "n64", "Dr. Mario 64 (USA).srm"
        ) is True

    def test_mupen_native_per_game_formats_included(self):
        policy = DEFAULT_SAVE_SELECTION_POLICY
        for filename in (
            "Game.eep",
            "Game.sra",
            "Game.fla",
            "Game.mpk",
            "Game.1.sav",
        ):
            assert policy.is_included("n64", filename) is True

    def test_n64dd_persistent_disk_formats_included(self):
        policy = DEFAULT_SAVE_SELECTION_POLICY
        for filename in ("Game.ndr", "Game.d6r", "Game.ram"):
            assert policy.is_included("n64dd", filename) is True

    def test_savestates_included_and_nested_artifacts_excluded(self):
        policy = DEFAULT_SAVE_SELECTION_POLICY
        assert policy.is_included("n64", "Dr. Mario 64 (USA).state") is True
        assert policy.is_included("n64", "Dr. Mario 64 (USA).st0") is True
        for relative_path in (
            "savestates/Dr. Mario 64 (USA).srm",
            "shaders/Dr. Mario 64 (USA).srm",
            "config/Dr. Mario 64 (USA).srm",
            "cache/Dr. Mario 64 (USA).srm",
            "logs/Dr. Mario 64 (USA).srm",
        ):
            assert policy.is_included("n64", relative_path) is False


class TestNDSNative:
    def test_root_sav_and_srm_included(self):
        policy = DEFAULT_SAVE_SELECTION_POLICY
        assert policy.is_included("nds", "Game.sav") is True
        assert policy.is_included("nds", "Game.srm") is True

    def test_savestate_included_but_shared_images_excluded(self):
        policy = DEFAULT_SAVE_SELECTION_POLICY
        assert policy.is_included("nds", "Game.mln") is True
        for relative_path in (
            "dldi.bin",
            "dsisd.bin",
            "states/Game.sav",
        ):
            assert policy.is_included("nds", relative_path) is False


class TestMAMENative:
    def test_nvram_included(self):
        assert DEFAULT_SAVE_SELECTION_POLICY.is_included(
            "mame", "nvram/pacman/nvram"
        ) is True

    def test_non_progress_mame_trees_excluded(self):
        policy = DEFAULT_SAVE_SELECTION_POLICY
        assert policy.is_included("mame", "state/pacman/auto.sta") is True
        for relative_path in (
            "cfg/pacman.cfg",
            "input/pacman.inp",
            "diff/disk.chd",
            "comments/pacman.xml",
            "plugins/hiscore.dat",
        ):
            assert policy.is_included("mame", relative_path) is False


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

    def test_sstates_included(self):
        policy = DEFAULT_SAVE_SELECTION_POLICY
        assert policy.is_included("pcsx2", "sstates/Game.p2s") is True

    def test_batocera_v43_nested_memory_cards_and_states_included(self):
        policy = DEFAULT_SAVE_SELECTION_POLICY
        assert policy.is_included("ps2", "pcsx2/Mcd001.ps2") is True
        assert policy.is_included("ps2", "pcsx2/sstates/Game.p2s") is True
        assert policy.is_included("ps2", "pcsx2/videos/Game.mp4") is False

    def test_videos_excluded(self):
        policy = DEFAULT_SAVE_SELECTION_POLICY
        assert policy.is_included("pcsx2", "videos/Game.mp4") is False


class TestPPSSPP:
    def test_savedata_included(self):
        policy = DEFAULT_SAVE_SELECTION_POLICY
        assert policy.is_included("ppsspp", "PSP/SAVEDATA/ULUS12345/save.bin") is True

    def test_state_included(self):
        policy = DEFAULT_SAVE_SELECTION_POLICY
        assert policy.is_included("ppsspp", "PPSSPP_STATE/ULUS12345_1.ppst") is True


class TestRPCS3:
    def test_progress_and_savestate_paths_are_default_content(self):
        policy = DEFAULT_SAVE_SELECTION_POLICY
        for path in (
            "rpcs3/dev_hdd0/home/00000001/savedata/BLUS12345/SAVE.DAT",
            "rpcs3/dev_hdd0/home/00000001/trophy/NPWR00001_00/TROPUSR.DAT",
            "rpcs3/dev_hdd0/savedata/vmc/MemoryCard.VM1",
            "BLUS12345/BLUS12345_2026-08-10_120000.SAVESTAT",
            "BLUS12345/BLUS12345_2026-08-10_120000.SAVESTAT.zst",
        ):
            assert policy.is_included("ps3", path) is True

    def test_installed_games_are_a_disabled_optional_group(self):
        policy = DEFAULT_SAVE_SELECTION_POLICY
        path = "rpcs3/dev_hdd0/game/NPUA12345/USRDIR/EBOOT.BIN"
        decision = policy.classify("ps3", path)
        assert decision.included is False
        assert decision.optional_group == RPCS3_INSTALLED_GAMES_GROUP
        assert policy.is_included(
            "ps3",
            path,
            enabled_optional_groups=frozenset({RPCS3_INSTALLED_GAMES_GROUP}),
        ) is True

    def test_generated_and_ambiguous_rpcs3_content_stays_excluded(self):
        policy = DEFAULT_SAVE_SELECTION_POLICY
        enabled = frozenset({RPCS3_INSTALLED_GAMES_GROUP})
        for path in (
            "rpcs3/dev_hdd0/game/_GDATA_12345/USRDIR/partial.bin",
            "rpcs3/dev_hdd0/tmp/runtime.bin",
            "rpcs3/cache/shaders/cache.bin",
            "rpcs3/dev_hdd0/home/00000001/exdata/license.rap",
        ):
            assert policy.is_included(
                "ps3", path, enabled_optional_groups=enabled
            ) is False


class TestXbox360:
    def test_xbox360_tree_included(self):
        policy = DEFAULT_SAVE_SELECTION_POLICY
        assert policy.is_included("xbox360", "0000000000000000/save/data.bin") is True


class TestYuzu:
    _USER = "0123456789ABCDEF0123456789ABCDEF"
    _TITLE = "0100F2C0115B6000"

    def test_account_title_save_tree_included(self):
        policy = DEFAULT_SAVE_SELECTION_POLICY
        base = f"0000000000000000/{self._USER}/{self._TITLE}"
        assert policy.is_included("yuzu", f"{base}/save_data") is True
        assert policy.is_included("yuzu", f"{base}/slot 1/progress.dat") is True

    def test_rule_requires_exact_yuzu_id_shape(self):
        policy = DEFAULT_SAVE_SELECTION_POLICY
        assert YUZU_ACCOUNT_SAVE_GLOB
        assert policy.is_included(
            "yuzu", f"0000000000000000/too-short/{self._TITLE}/save_data"
        ) is False
        assert policy.is_included(
            "yuzu", f"0000000000000000/{self._USER}/not-a-title/save_data"
        ) is False

    def test_keys_excluded(self):
        policy = DEFAULT_SAVE_SELECTION_POLICY
        assert policy.is_included("yuzu", "keys/prod.keys") is False

    def test_cache_and_shader_data_excluded(self):
        policy = DEFAULT_SAVE_SELECTION_POLICY
        assert policy.is_included("yuzu", "cache/0100F2C0115B6000/index.bin") is False
        assert policy.is_included("yuzu", "shader/0100F2C0115B6000/opengl.bin") is False

    def test_nand_system_firmware_excluded(self):
        policy = DEFAULT_SAVE_SELECTION_POLICY
        assert policy.is_included(
            "yuzu", "nand/system/Contents/registered/0123456789abcdef.nca"
        ) is False
        assert policy.is_included(
            "yuzu", "nand/system/Contents/registered/0123456789abcdef.cnmt.nca"
        ) is False

    def test_logs_config_and_root_preview_metadata_excluded(self):
        policy = DEFAULT_SAVE_SELECTION_POLICY
        assert policy.is_included("yuzu", "log/yuzu_log.txt") is False
        assert policy.is_included("yuzu", "config/qt-config.ini") is False
        assert policy.is_included("yuzu", "preview.pv.txt") is False

    def test_unrelated_nand_user_content_excluded(self):
        policy = DEFAULT_SAVE_SELECTION_POLICY
        assert policy.is_included(
            "yuzu",
            f"nand/user/save/0000000000000000/{self._USER}/{self._TITLE}/save_data",
        ) is False


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
