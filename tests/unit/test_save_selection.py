"""Unit tests for romcloud.core.save_selection (pure logic, no filesystem)."""

from __future__ import annotations

import pytest

from romcloud.core.save_selection import (
    DEFAULT_SAVE_SELECTION_POLICY,
    XBOX_HDD_RELATIVE_PATH,
    XBOX_SYSTEM,
    YUZU_ACCOUNT_SAVE_GLOB,
    SaveLayout,
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
            "dolphin-emu",
            "3ds",
            "wiiu",
            "psvita",
            "ymir",
        ):
            assert policy.is_known_system(system) is True

    def test_unvalidated_systems_are_unsupported(self):
        """Unregistered emulator trees stay unsupported, not blindly included."""
        policy = DEFAULT_SAVE_SELECTION_POLICY
        for system in ("citra", "dolphin", "gamecube", "totally-unknown-system"):
            assert policy.is_known_system(system) is False
            assert policy.is_included(system, "anything.bin") is False

    def test_noncanonical_paths_are_never_supported(self):
        policy = DEFAULT_SAVE_SELECTION_POLICY
        for path in (
            "/psx/Game.srm",
            "psx\\Game.srm",
            "psx/../Game.srm",
            "psx//Game.srm",
            "psx/Game.srm/",
        ):
            assert policy.group_for_path(path) is None


class TestPS1Native:
    def test_srm_included(self):
        assert DEFAULT_SAVE_SELECTION_POLICY.is_included("psx", "Game.srm") is True

    def test_libretro_memory_card_variants_are_included(self):
        policy = DEFAULT_SAVE_SELECTION_POLICY
        assert policy.is_included("psx", "Game.mcr") is True
        assert policy.is_included("psx", "pcsx-card2.mcd") is True


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

    def test_direct_classic_namespace_is_complete_and_recursive(self):
        policy = DEFAULT_SAVE_SELECTION_POLICY
        for relative_path in (
            "states/Game.srm",
            "savestates/Game.srm",
            "Game.rtc",
        ):
            assert policy.is_included("snes", relative_path) is True

    def test_complete_classic_namespace_preserves_existing_files(self):
        policy = DEFAULT_SAVE_SELECTION_POLICY
        assert policy.is_included("snes", "Game.state") is True
        assert policy.is_included("snes", "Game.state.auto") is True
        assert policy.is_included("snes", "Game.cfg") is True

    def test_unsupported_retroarch_namespace_remains_root_filtered(self):
        policy = DEFAULT_SAVE_SELECTION_POLICY
        assert policy.is_included("dreamcast", "Game.srm") is True
        assert policy.is_included("dreamcast", "nested/Game.srm") is False
        assert policy.is_included("dreamcast", "retroarch.log") is False


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

    def test_native_slot_files_share_the_game_conflict_unit(self):
        policy = DEFAULT_SAVE_SELECTION_POLICY

        assert policy.group_for_path(
            "n64/Game.srm"
        ) == policy.group_for_path("n64/Game.1.sav")


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

    def test_installed_games_are_never_eligible(self):
        policy = DEFAULT_SAVE_SELECTION_POLICY
        path = "rpcs3/dev_hdd0/game/NPUA12345/USRDIR/EBOOT.BIN"
        decision = policy.classify("ps3", path)
        assert decision.included is False
        assert decision.optional_group is None
        assert policy.is_included(
            "ps3",
            path,
            enabled_optional_groups=frozenset({"rpcs3_installed_games"}),
        ) is False

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

    def test_compatible_switch_emulators_share_one_lifecycle_layout(self):
        policy = DEFAULT_SAVE_SELECTION_POLICY
        expected = frozenset({"yuzu-account-title-save"})

        for emulator in ("eden", "citron", "yuzu"):
            assert policy.layout_ids_for_lifecycle(
                system="switch", emulator=emulator
            ) == expected


class TestModernNintendoTitleTrees:
    def test_azahar_includes_only_title_save_data_and_metadata(self):
        policy = DEFAULT_SAVE_SELECTION_POLICY
        base = (
            "azahar-emu/sdmc/Nintendo 3DS/"
            "0123456789ABCDEF0123456789ABCDEF/"
            "FEDCBA9876543210FEDCBA9876543210/"
            "title/00040000/001B5100"
        )
        for relative in (
            f"{base}/data/00000001/main",
            f"{base}/data/00000001.metadata",
        ):
            assert policy.is_included("3ds", relative) is True
        for relative in (
            f"{base}/content/00000000.app",
            "azahar-emu/nand/data/sysdata/account.dat",
            "azahar-emu/shaders/001B5100.bin",
            "azahar-emu/states/001B5100.state",
        ):
            assert policy.is_included("3ds", relative) is False

    def test_cemu_groups_every_file_for_one_title_without_other_mlc_data(self):
        policy = DEFAULT_SAVE_SELECTION_POLICY
        first = policy.group_for_path(
            "wiiu/usr/save/00050000/101C9400/user/80000001/game_data.sav"
        )
        sibling = policy.group_for_path(
            "wiiu/usr/save/00050000/101C9400/meta/saveinfo.xml"
        )
        other = policy.group_for_path(
            "wiiu/usr/save/00050000/10143600/user/common.dat"
        )

        assert first is not None and sibling is not None and other is not None
        assert first.group_id == sibling.group_id
        assert first.group_id != other.group_id
        assert policy.is_included(
            "wiiu", "usr/title/00050000/101C9400/content/code.rpx"
        ) is False
        assert policy.is_included("wiiu", "graphicPacks/cache.bin") is False


class TestVita3K:
    def test_title_savedata_is_grouped_and_other_ux0_content_is_excluded(self):
        policy = DEFAULT_SAVE_SELECTION_POLICY
        first = policy.group_for_path(
            "psvita/ux0/user/00/savedata/PCSE00762/SlotParam_0.bin"
        )
        sibling = policy.group_for_path(
            "psvita/ux0/user/00/savedata/PCSE00762/SystemData_0000.sav"
        )
        other = policy.group_for_path(
            "psvita/ux0/user/00/savedata/PCSG01234/save.bin"
        )

        assert first is not None and sibling is not None and other is not None
        assert first.group_id == sibling.group_id
        assert first.group_id != other.group_id
        assert policy.is_included("psvita", "ux0/app/PCSE00762/eboot.bin") is False
        assert policy.is_included("psvita", "shader/PCSE00762/cache.bin") is False


class TestFlycast:
    def test_only_audited_vmu_images_are_included_as_opaque_cards(self):
        policy = DEFAULT_SAVE_SELECTION_POLICY
        global_card = policy.group_for_path("dreamcast/flycast/vmu_save_A1.bin")
        per_game = policy.group_for_path(
            "dreamcast/flycast/MK-51052_vmu_save_A1.bin"
        )

        assert global_card is not None and per_game is not None
        assert global_card.shared is True and per_game.shared is True
        assert global_card.group_id != per_game.group_id
        assert policy.is_included("dreamcast", "flycast/emu.cfg") is False
        assert policy.is_included("dreamcast", "flycast/vmu_save_E1.bin") is False


class TestYmir:
    def test_only_audited_backup_memory_and_disc_state_files_are_included(self):
        policy = DEFAULT_SAVE_SELECTION_POLICY
        disc = "0123456789ABCDEF0123456789ABCDEF"
        state = policy.group_for_path(f"ymir/{disc}/0.savestate")
        state_meta = policy.group_for_path(f"ymir/{disc}/meta.txt")
        per_game = policy.group_for_path(
            "ymir/backup/games/bup-int-NiGHTS into Dreams [MK-81020].bin"
        )
        global_ram = policy.group_for_path("ymir/state/bup-int.bin")

        assert state is not None and state_meta is not None
        assert state.group_id == state_meta.group_id
        assert per_game is not None and per_game.shared is True
        assert global_ram is not None and global_ram.shared is True
        for relative in (
            "backup/exported/NIGHTS.BUP",
            "dumps/vdp2.bin",
            "state/smpc-us_eu.bin",
            "Ymir.toml",
            f"{disc}/screenshot.png",
        ):
            assert policy.is_included("ymir", relative) is False

    def test_ymir_lifecycle_uses_ymir_layouts_not_retroarch_saturn(self):
        assert DEFAULT_SAVE_SELECTION_POLICY.layout_ids_for_lifecycle(
            system="saturn", emulator="ymir", core="ymir"
        ) == frozenset(
            {
                "ymir-global-backup-memory",
                "ymir-per-game-backup-memory",
                "ymir-save-states",
            }
        )


class TestDolphin:
    def test_default_gamecube_memory_cards_and_gci_saves_are_included(self):
        policy = DEFAULT_SAVE_SELECTION_POLICY
        for path in (
            "GC/MemoryCardA.USA.raw",
            "GC/MemoryCardB.EUR.251.raw",
            "GC/USA/Card A/01-GAME-save.gci",
        ):
            assert policy.is_included("dolphin-emu", path) is True

    def test_gamecube_shared_and_per_file_groups_match_storage_semantics(self):
        policy = DEFAULT_SAVE_SELECTION_POLICY
        raw = policy.group_for_path("dolphin-emu/GC/MemoryCardA.USA.raw")
        gci_a = policy.group_for_path("dolphin-emu/GC/USA/Card A/01-GAME-save.gci")
        gci_b = policy.group_for_path("dolphin-emu/GC/USA/Card B/01-GAME-save.gci")

        assert raw is not None and raw.shared is True
        assert gci_a is not None and gci_a.shared is False
        assert gci_b is not None and gci_b.shared is False
        assert gci_a.group_id != gci_b.group_id

    def test_wii_game_and_channel_saves_are_grouped_per_title(self):
        policy = DEFAULT_SAVE_SELECTION_POLICY
        first = policy.group_for_path(
            "dolphin-emu/Wii/title/00010004/524d4345/data/rksys.dat"
        )
        sibling = policy.group_for_path(
            "dolphin-emu/Wii/title/00010004/524d4345/data/banner.bin"
        )
        other = policy.group_for_path(
            "dolphin-emu/Wii/title/00010004/52534245/data/save.dat"
        )

        assert first is not None and sibling is not None and other is not None
        assert first.group_id == sibling.group_id
        assert first.group_id != other.group_id
        assert first.shared is False

    def test_non_save_dolphin_content_and_invalid_ids_are_excluded(self):
        policy = DEFAULT_SAVE_SELECTION_POLICY
        for path in (
            "Config/Dolphin.ini",
            "Cache/Shaders/cache.bin",
            "Logs/dolphin.log",
            "ScreenShots/RMCE01.png",
            "Load/Textures/RMCE01/texture.png",
            "GameSettings/RMCE01.ini",
            "GC/USA/IPL.bin",
            "GC/SRAM.raw",
            "GC/USA/Card A/MC_SYSTEM_AREA",
            "Wii/shared2/sys/SYSCONF",
            "Wii/title/00000001/00000002/data/setting.txt",
            "Wii/title/00010002/48414341/data/system-channel.dat",
            "Wii/title/00010005/524d4345/data/dlc.bin",
            "Wii/title/00010004/not-hex/data/save.dat",
            "Wii/title/00010004/524d4345/content/title.tmd",
            "unknown/deep/save.gci",
        ):
            assert policy.is_included("dolphin-emu", path) is False

    def test_state_slots_and_recording_companions_group_per_game(self):
        policy = DEFAULT_SAVE_SELECTION_POLICY
        first = policy.group_for_path("dolphin-emu/StateSaves/RMCE01.s01")
        sibling = policy.group_for_path("dolphin-emu/StateSaves/RMCE01.s01.dtm")
        other_slot = policy.group_for_path("dolphin-emu/StateSaves/RMCE01.s02")
        other_game = policy.group_for_path("dolphin-emu/StateSaves/RSBE01.s01")

        assert first is not None and sibling is not None
        assert other_slot is not None and other_game is not None
        assert first.group_id == sibling.group_id == other_slot.group_id
        assert first.group_id != other_game.group_id
        assert policy.is_included("dolphin-emu", "StateSaves/lastState.sav") is False


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


class TestPositiveLayoutRegistry:
    def test_registry_is_auditable_and_layout_ids_are_unique(self):
        layouts = DEFAULT_SAVE_SELECTION_POLICY.layouts

        assert layouts
        assert len({layout.layout_id for layout in layouts}) == len(layouts)
        assert all(layout.system and layout.eligible_files for layout in layouts)
        assert not any(
            layout.system == "ps3" and "/game" in layout.root_pattern
            for layout in layouts
        )

    def test_direct_layout_requires_explicit_complete_matching_route_root(self):
        base = dict(
            layout_id="unsafe-direct",
            system="nes",
            root_pattern="",
            recursive=True,
            eligible_files=("*",),
            direct_save_capable=True,
        )
        with pytest.raises(ValueError, match="static, complete"):
            SaveSelectionPolicy(
                layouts=(SaveLayout(**base, direct_save_root="other-system"),)
            )
        with pytest.raises(ValueError, match="static, complete"):
            SaveSelectionPolicy(
                layouts=(
                    SaveLayout(
                        **{
                            **base,
                            "eligible_files": ("*.srm",),
                            "direct_save_root": "nes",
                        }
                    ),
                )
            )

    def test_lifecycle_identity_resolution_is_declarative_and_fail_closed(self):
        policy = DEFAULT_SAVE_SELECTION_POLICY

        assert policy.layout_ids_for_lifecycle(
            system="psx", emulator="duckstation", core="duckstation"
        ) == frozenset(
            {"duckstation-memory-cards", "duckstation-root-sav"}
        )
        assert policy.layout_ids_for_lifecycle(
            system="psx", emulator="libretro", core="pcsx-rearmed"
        ) == frozenset({"retroarch-root-psx"})
        assert policy.layout_ids_for_lifecycle(system="gamecube") == frozenset(
            {
                "dolphin-gc-memory-card-images",
                "dolphin-gc-gci-saves",
                "dolphin-save-states",
            }
        )
        assert policy.layout_ids_for_lifecycle(
            system="xbox", emulator="xemu"
        ) == frozenset()
        assert policy.layout_ids_for_lifecycle(
            system="unknown", emulator="unknown", core="unknown"
        ) == frozenset()

    def test_new_emulator_target_requires_only_layout_registry_data(self):
        layout = SaveLayout(
            layout_id="example-emulator-saves",
            system="example-storage",
            root_pattern="profiles",
            recursive=True,
            eligible_files=("*.sav",),
            lifecycle_systems=("example-console",),
            lifecycle_emulators=("example-emulator",),
            lifecycle_cores=("example-core",),
        )
        policy = SaveSelectionPolicy(layouts=(layout,))

        expected = frozenset({"example-emulator-saves"})
        assert policy.layout_ids_for_lifecycle(
            system="example-console", emulator="example-emulator"
        ) == expected
        assert policy.layout_ids_for_lifecycle(
            system="example-console", core="example-core"
        ) == expected
        assert policy.layout_ids_for_lifecycle(system="example-console") == frozenset()
        assert policy.layout_ids_for_lifecycle(
            system="other", emulator="example-emulator"
        ) == frozenset()
        assert policy.layout_ids_for_lifecycle(system="other") == frozenset()

    def test_stable_root_save_group_ignores_state_suffix(self):
        policy = DEFAULT_SAVE_SELECTION_POLICY

        save = policy.group_for_path("snes/Chrono Trigger.srm")
        state = policy.group_for_path("snes/Chrono Trigger.state.auto")

        assert save is not None and state is not None
        assert save.group_id == state.group_id
        assert save.shared is False

    def test_shared_layouts_are_explicit_and_use_safe_dataset_group(self):
        policy = DEFAULT_SAVE_SELECTION_POLICY

        first = policy.group_for_path("duckstation/memcards/card1.mcd")
        second = policy.group_for_path("duckstation/memcards/card2.mcd")
        xenia = policy.group_for_path("xbox360/0000000000000000/save/data.bin")

        assert first is not None and second is not None and xenia is not None
        assert first.shared is True
        assert first.group_id == second.group_id
        assert xenia.shared is True

    def test_dynamic_title_layouts_produce_stable_groups(self):
        policy = DEFAULT_SAVE_SELECTION_POLICY
        user = "0123456789ABCDEF0123456789ABCDEF"
        base = f"yuzu/0000000000000000/{user}/0100F2C0115B6000"

        first = policy.group_for_path(f"{base}/slot1/progress.dat")
        second = policy.group_for_path(f"{base}/slot2/options.dat")

        assert first is not None and second is not None
        assert first.group_id == second.group_id
        assert first.layout_id == "yuzu-account-title-save"

    def test_unknown_or_dangerous_path_has_no_group(self):
        policy = DEFAULT_SAVE_SELECTION_POLICY

        for path in (
            "unknown/deep/file.sav",
            "yuzu/keys/prod.keys",
            "ps3/rpcs3/dev_hdd0/game/BLUS12345/USRDIR/EBOOT.BIN",
        ):
            assert policy.is_canonical_path_supported(path) is False
            assert policy.group_for_path(path) is None
