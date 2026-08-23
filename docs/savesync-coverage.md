# SaveSync popular-system coverage audit

Audit date: 2026-08-22. Batocera source baseline:
`dee58350109b3898a612576f6e6b619029a13407`.

`UT` means the registry/service behavior is implemented and covered by unit
tests. It does not mean that an emulator loaded the result on Batocera
hardware. Every implemented row still requires the hardware load check in the
last column before it can be called fully validated.

| System | Emulator | Batocera save layout | Registry status | Discovery | Conflict granularity | Quick Sync | Materialization | Auto/game-stop | Container/shared status | Hardware validation required | Remaining limitations |
|---|---|---|---|---|---|---|---|---|---|---|---|
| NES | RetroArch cores | `/userdata/saves/nes/*.{srm,state*}` | Existing, confirmed | UT, root-only | Per title stem | UT, generic | UT to same root | UT, system lifecycle | Filesystem files | Yes: create/load/delete/conflict | Emulator load not exercised here |
| SNES | RetroArch cores | `/userdata/saves/snes/*.{srm,state*}` | Existing, confirmed | UT, root-only | Per title stem | UT, generic | UT to same root | UT, system lifecycle | Filesystem files | Yes | Emulator load not exercised here |
| Game Boy | RetroArch cores | `/userdata/saves/gb/*.{srm,state*}` | Existing, confirmed | UT, root-only | Per title stem | UT, generic | UT to same root | UT, system lifecycle | Filesystem files | Yes | Emulator load not exercised here |
| Game Boy Color | RetroArch cores | `/userdata/saves/gbc/*.{srm,state*}` | Existing, confirmed | UT, root-only | Per title stem | UT, generic | UT to same root | UT, system lifecycle | Filesystem files | Yes | Emulator load not exercised here |
| GBA | RetroArch cores | `/userdata/saves/gba/*.{srm,state*}` | Existing, confirmed | UT, root-only | Per title stem | UT, named repair regression | UT, missing-file repair | UT, named game-stop test | Filesystem files | Yes | Emulator load not exercised here |
| N64 / N64DD | RetroArch and standalone layout | `/userdata/saves/{n64,n64dd}` audited EEPROM/SRAM/Flash/controller-pak/disk/state extensions | Existing, confirmed | UT, exact extensions | Per title; numbered controller-pak siblings stay together | UT, generic | UT to same root | UT, system lifecycle | Multi-file filesystem group | Yes | New emulator extensions require a new audit |
| Nintendo DS | RetroArch / melonDS-compatible root | `/userdata/saves/nds/*.{srm,sav,state*,mln}` | Existing, confirmed | UT, root-only | Per title stem | UT, generic | UT to same root | UT, system lifecycle | Shared SD images excluded | Yes | Emulator load not exercised here |
| Nintendo 3DS | Azahar | `/userdata/saves/3ds/azahar-emu/sdmc/Nintendo 3DS/<id0>/<id1>/title/<high>/<low>/data/*` | **New, UT** | UT, validated hex levels and `data` only | Whole title data directory including metadata | UT through registry-scoped reconciliation | UT, exact primary-tree round trip | UT for `3ds` + Azahar | Filesystem title directory | Yes: installed/eShop titles and metadata | Installed content, NAND, shaders, config, and states are excluded |
| GameCube | Dolphin | `/userdata/saves/dolphin-emu/GC` raw cards or region/card GCI folders; `StateSaves/<game>.sNN[.dtm]` | Existing saves confirmed; **states added, UT** | UT, exact card/GCI/state patterns | Raw card per image; GCI per file; all slots/DTM companions per game | UT, generic | UT to exact Dolphin tree | UT for GameCube | Raw cards remain opaque; GCI/state files filesystem-native | Yes: raw-card and GCI modes | `lastState.sav` and non-save Dolphin data excluded |
| Wii | Dolphin | `/userdata/saves/dolphin-emu/Wii/title/{00010000,00010001,00010004}/<title>/data`; Dolphin states as above | Existing confirmed; **states added, UT** | UT, allowed title namespaces only | Per Wii title; states per game | UT, generic | UT to exact Dolphin tree | UT for Wii | Filesystem title directory | Yes: disc and channel saves | Other NAND namespaces and DLC/system content excluded |
| Wii U | Cemu | `/userdata/saves/wiiu/usr/save/<high>/<low>/**` | **New, UT** | UT, exact two-level title ID | Whole title save directory | UT through registry-scoped reconciliation | UT, exact round trip | UT for Wii U + Cemu | Filesystem title directory | Yes: create/change/delete/conflict and load | `usr/title`, updates, content, shader cache, config excluded |
| Switch | Eden | Physical `/userdata/system/configs/eden/nand/user/save/0000000000000000/<account32>/<title16>/**`; canonical remote `yuzu/...` | **New physical mapping, UT** | UT, exact account/title levels | One account + title directory | UT, named two-device Metroid Dread test | UT into Eden NAND, including missing repair and deletion | UT, named Eden game-stop test | Filesystem title domain; no NAND container merge | **Yes: Metroid Dread load on device B** | Eden is not in the audited Batocera master tree; mapping is based on Eden plus the Batocera-derived REG-Linux generator. If Eden and Citron roots are simultaneously ambiguous, one is selected deterministically and the other is untouched with a warning |
| Switch | Citron | Physical `/userdata/system/configs/citron/nand/user/save/0000000000000000/<account32>/<title16>/**`; canonical remote `yuzu/...` | **New physical mapping, UT** | UT, exact account/title levels | One account + title directory | UT through the same canonical layout | UT into Citron NAND | UT for Switch + Citron | Same logical layout as Eden/Yuzu | Yes: device-to-device load | Simultaneously ambiguous compatible roots are never merged |
| Switch | legacy Yuzu-compatible install | `/userdata/saves/yuzu/0000000000000000/<account32>/<title16>/**` | Existing, confirmed | UT, exact account/title levels | One account + title directory | UT, generic | UT to legacy primary tree when no mapped fork is active | UT for Switch/Yuzu | Filesystem title domain | Yes on retained installations | Keys, firmware/system NAND, cache, shaders, logs and config excluded |
| Switch | Ryujinx / other forks | No current generator found in audited Batocera tree | Unsupported | No | None | No | No | No | None | Re-audit if Batocera adds one | No guessed paths |
| PS1 | RetroArch cores | `/userdata/saves/psx/*.{srm,state*}` | Existing, confirmed | UT | Per title stem | UT, generic | UT to same root | UT, system lifecycle | Filesystem files | Yes | Emulator load not exercised here |
| PS1 | DuckStation | `/userdata/saves/duckstation/memcards/**` and root `*.sav` | Existing, confirmed | UT | Opaque card by default; experimental commercial-game domains inside valid 128 KiB raw cards | UT | UT | UT for DuckStation | Experimental `ps1-raw-memory-card`; opaque fallback | Yes, especially card adapter | Resume files excluded; adapter stays opt-in |
| PS2 | PCSX2 | `/userdata/saves/ps2/pcsx2/Mcd*.ps2`, folder cards, `sstates/**`; legacy `pcsx2` roots retained | Existing, confirmed | UT | Monolithic cards/states deliberately broad; marker-verified folder card can use one structural entry | UT | UT | UT for PS2/PCSX2 | Monolithic `.ps2` opaque; experimental folder adapter with opaque fallback | Yes | Multi-entry folder cards remain opaque |
| PSP | PPSSPP | `/userdata/saves/ppsspp/PSP/SAVEDATA/<title>/**`; `PPSSPP_STATE/**` | Existing, confirmed | UT | Savedata first descendant; state stem | UT, nested repair regression | UT | UT for PSP/PPSSPP | Filesystem directory | Yes | Other PSP data excluded |
| PS3 | RPCS3 | Canonical `ps3/rpcs3/dev_hdd0/home/<user>/savedata/<title>/**`; Batocera config-tree compatibility mapping retained; trophies, VMC and title savestates separately allowlisted | Existing, confirmed | UT | Savedata/trophy per title; savestate per title; VMC broad | UT, mapped-root regression | UT to configured physical tree | Registry-scoped; hardware check required | VMC remains opaque layout | Yes | Installed games, patches, firmware, caches, logs, and config excluded |
| Master System | RetroArch cores | `/userdata/saves/mastersystem/*.{srm,state*}` | Existing, confirmed | UT | Per title stem | UT | UT | UT | Filesystem files | Yes | Emulator load not exercised here |
| Genesis / Mega Drive | RetroArch cores | `/userdata/saves/megadrive/*.{srm,state*}` | Existing, confirmed | UT | Per title stem | UT | UT | UT | Filesystem files | Yes | Emulator load not exercised here |
| Sega CD / Mega CD | RetroArch cores | `/userdata/saves/megacd/*.{srm,state*}` | Existing, confirmed | UT | Per title stem | UT | UT | UT | Filesystem files | Yes | Emulator load not exercised here |
| 32X | RetroArch cores | `/userdata/saves/sega32x/*.{srm,state*}` | Existing, confirmed | UT | Per title stem | UT | UT | UT | Filesystem files | Yes | Emulator load not exercised here |
| Saturn | RetroArch cores | `/userdata/saves/saturn/*.{srm,state*}` | Existing, confirmed | UT | Per title stem | UT | UT | UT when RetroArch identity is used | Filesystem files | Yes | Does not claim Ymir data |
| Saturn | Ymir | `/userdata/saves/ymir/<disc-hash32>/{N,N-1}.savestate` + `meta.txt`; `/userdata/saves/ymir/backup/games/bup-{int,ext}-*.bin`; mapped `/userdata/system/configs/ymir/state/bup-int.bin` | **New, UT** | UT, exact state hash/patterns and backup filenames | States per disc; each backup RAM image independent/opaque | UT through generic mapped/primary views | UT including global RAM mapped root | UT for Saturn + Ymir | Backup RAM images opaque | Yes: global/per-game RAM modes and state load | Dumps, exports, config and `smpc-*.bin` excluded |
| Dreamcast | RetroArch cores | `/userdata/saves/dreamcast/*.{srm,state*}` | Existing, confirmed | UT | Per title stem | UT | UT | UT by system fallback | Filesystem files | Yes | Emulator load not exercised here |
| Dreamcast | Flycast standalone | `/userdata/saves/dreamcast/flycast/{vmu_save_,<game>_vmu_save_}{A-D}{1-2}.bin` | **New, UT** | UT, exact VMU names/slots | One opaque physical VMU image | UT | UT, exact round trip | UT for Dreamcast + Flycast | Correctly opaque per card | Yes: global and per-game VMU modes | Config and unrecognized card names excluded |
| MAME | MAME / libretro | Root `*.srm`, `*.state*`; `nvram/<machine>/**`; `state/<machine>/**` | Existing, confirmed | UT | Per root title or first machine descendant | UT | UT | UT | Filesystem machine directory | Yes | cfg, input, diff, artwork and unrelated runtime data excluded |
| FBNeo | RetroArch core | `/userdata/saves/fbneo/*.{srm,state*}` | Existing, confirmed | UT | Per title stem | UT | UT | UT | Filesystem files | Yes | Emulator load not exercised here |
| Neo Geo | RetroArch core | `/userdata/saves/neogeo/*.{srm,state*}` | Existing, confirmed | UT | Per title stem | UT | UT | UT | Filesystem files | Yes | Emulator load not exercised here |
| Neo Geo CD | RetroArch core | `/userdata/saves/neogeocd/*.{srm,state*}` | Existing, confirmed | UT | Per title stem | UT | UT | UT | Filesystem files | Yes | Emulator load not exercised here |
| PC Engine / TurboGrafx-16 | RetroArch core | `/userdata/saves/pcengine/*.{srm,state*}` | Existing, confirmed | UT | Per title stem | UT | UT | UT | Filesystem files | Yes | Emulator load not exercised here |
| PC Engine CD / TurboGrafx-CD | RetroArch core | `/userdata/saves/pcenginecd/*.{srm,state*}` | Existing, confirmed | UT | Per title stem | UT | UT | UT | Filesystem files | Yes | Emulator load not exercised here |
| Original Xbox | xemu | `/userdata/saves/xbox/xbox_hdd.qcow2` | Existing, intentionally disabled by default | Opt-in manual only | Whole HDD | Not automatic | Force path only when opted in | Disabled | Correctly opaque whole disk | Yes before enabling | No safe filesystem decomposition was proven |
| Xbox 360 | Xenia / Xenia Edge | `/userdata/saves/xbox360/**` | Existing, retained | UT, broad positive root | Whole content tree | UT, generic when enabled by registry | UT to same root | Registry lifecycle by system; hardware check required | Correctly opaque pending stronger proof | Yes | Xenia's XUID/title data and related profile/header material make filesystem-only per-title decomposition unsafe |
| Vita | Vita3K | `/userdata/saves/psvita/ux0/user/00/savedata/<title-id>/**` | **New, UT** | UT, title-ID level only | Whole title savedata directory | UT through registry-scoped reconciliation | UT, exact round trip | UT for Vita/PSVita + Vita3K | Filesystem title directory | Yes: encrypted/content variants and load | Applications, licenses, shaders and other `ux0` content excluded |

## Shared and opaque review

- DuckStation raw cards and PCSX2 Folder Memory Cards are the only currently
  implemented logical-container decompositions, both experimental and with
  opaque fallback.
- RPCS3 trophies are safely grouped by their first title descendant. Dolphin
  raw cards, Flycast VMUs, and Ymir backup RAM are safely split only at the
  physical card/image boundary; none is decomposed internally.
- PCSX2 monolithic cards, Dolphin raw cards, Flycast VMUs, Ymir backup RAM,
  RPCS3 virtual memory cards, and xemu's HDD remain opaque because they are
  shared physical media and no complete safe logical namespace was proven.
- Xenia's broad tree remains one conflict domain. Current source uses
  user/title paths, but profile migration and related header/profile material
  are also part of the content root; splitting only the obvious title folder
  could produce an incomplete generation.
- PCSX2 savestate and legacy trees retain their existing broad grouping to
  avoid silently composing independently written generations.
- No existing broad/shared layout had enough evidence to classify it as
  obsolete or safe to remove in this audit.

## Hardware qualification plan

Use disposable test saves plus an independent backup. Do not manufacture
emulator paths or move live saves to make a case pass.

1. Record the Batocera version, architecture, selected emulator/core, ROMCloud
   version, and the emulator's generated configuration. For mapped roots,
   record the exact configured NAND/profile path before running SaveSync.
2. Establish Device A and Device B with the same writable remote-data target.
   Run one Full Sync on each, then retain the SaveSync state, remote journal,
   lifecycle log, and hashes of the specific save group.
3. On Device A, launch Metroid Dread in Eden, reach a recognizable new in-game
   checkpoint, save normally, and exit through Batocera. Confirm game-stop
   completion and remote materialization at
   `yuzu/0000000000000000/<account32>/010093801237C000/**`.
4. On unchanged Device B, run ordinary Quick Sync. Confirm there was no
   Download All action, confirm the exact configured Eden physical path now
   contains the same hashes, then launch Metroid Dread and verify the new
   checkpoint loads. This emulator load is the required stage-7 gate.
5. Repeat the Eden case for an in-game modification and an emulator-generated
   deletion. Verify Device B Quick Sync updates/removes only that title group.
   Make independent changes to two title IDs on the two devices and verify they
   merge; then modify the same title on both and verify ROMCloud preserves an
   explicit conflict without overwriting either generation.
6. Repeat steps 2-5 with Citron if present. If both compatible roots are
   installed, exercise the ambiguity warning and confirm the unselected root is
   byte-for-byte untouched.
7. For Azahar, test a cartridge and an installed title. Verify both
   `data/00000001/**` and `data/00000001.metadata` move as one title group,
   while installed content, extdata, NAND, and shaders do not enter the plan.
8. For Cemu, test two Wii U titles with distinct `<high>/<low>` IDs. Exercise
   new save, modification, deletion, independent-title merge, same-title
   conflict, missing Device B materialization, Full Sync, Quick Sync, force
   Upload, and force Download. Launch both titles after downloads.
9. For Vita3K, repeat that sequence with two title IDs under
   `ux0/user/00/savedata`; confirm `ux0/app`, licenses, and shaders are absent
   from every preview and remote generation.
10. For Flycast, test both global and per-game VMU modes. Confirm each VMU
    image is its own opaque conflict domain and that a synchronized card is
    recognized in the emulator's VMU manager/game.
11. For Ymir, test global internal backup RAM, per-game internal backup RAM,
    an external backup RAM cartridge, and two save-state slots. Confirm
    `bup-int.bin` materializes under the configured Ymir persistent-state
    directory, state slots load, and `smpc-*.bin`, dumps, and exports remain
    untouched.
12. For Dolphin, test raw GameCube cards, GCI folder cards, Wii title saves,
    and a state slot with an optional `.dtm` companion. Confirm `lastState.sav`
    remains excluded and each synchronized result loads.
13. Interrupt one upload and one download during staging for a new layout.
    Confirm rollback, changed-path-only backup behavior, the narrow
    `.savesync-previous` generation, and a successful later Quick/Full repair.
14. Archive logs, before/after manifests, hashes, and emulator screenshots for
    each pass. Mark only rows whose emulator load check succeeded as
    hardware-validated; a filesystem-only pass remains implemented/UT.

## Primary layout evidence

- [Batocera Azahar generator](https://github.com/batocera-linux/batocera.linux/blob/dee58350109b3898a612576f6e6b619029a13407/package/batocera/core/batocera-configgen/configgen/configgen/generators/azahar/azaharGenerator.py)
- [Azahar SD save archive paths](https://github.com/azahar-emu/azahar/blob/a9707fdd883a01aca4fa75108f7cb81933aec0e9/src/core/file_sys/archive_source_sd_savedata.cpp)
- [Batocera Cemu paths](https://github.com/batocera-linux/batocera.linux/blob/dee58350109b3898a612576f6e6b619029a13407/package/batocera/core/batocera-configgen/configgen/configgen/generators/cemu/cemuPaths.py)
- [Batocera Citron generator](https://github.com/batocera-linux/batocera.linux/blob/dee58350109b3898a612576f6e6b619029a13407/package/batocera/core/batocera-configgen/configgen/configgen/generators/citron/citronGenerator.py)
- [Eden account/title sync guide](https://github.com/eden-emulator/mirror/blob/master/docs/user/SyncthingGuide.md)
- [REG-Linux Eden config](https://github.com/REG-Linux/REG-Linux/blob/3ec6c9cd2b8bb2bbeebe85cfe1f26a2e1991f2fb/package/system/reglinux-configgen/configgen/configgen/generators/eden/edenConfig.py)
- [Batocera Vita3K generator](https://github.com/batocera-linux/batocera.linux/blob/dee58350109b3898a612576f6e6b619029a13407/package/batocera/core/batocera-configgen/configgen/configgen/generators/vita3k/vita3kGenerator.py)
- [Batocera Flycast paths](https://github.com/batocera-linux/batocera.linux/blob/dee58350109b3898a612576f6e6b619029a13407/package/batocera/core/batocera-configgen/configgen/configgen/generators/flycast/flycastPaths.py)
- [Batocera Dolphin paths](https://github.com/batocera-linux/batocera.linux/blob/dee58350109b3898a612576f6e6b619029a13407/package/batocera/core/batocera-configgen/configgen/configgen/generators/dolphin/dolphinPaths.py)
- [Batocera Xenia generator](https://github.com/batocera-linux/batocera.linux/blob/dee58350109b3898a612576f6e6b619029a13407/package/batocera/core/batocera-configgen/configgen/configgen/generators/xenia/xeniaGenerator.py)
- [Batocera Ymir generator](https://github.com/batocera-linux/batocera.linux/blob/dee58350109b3898a612576f6e6b619029a13407/package/batocera/core/batocera-configgen/configgen/configgen/generators/ymir/ymirGenerator.py)
- [Ymir save-state service](https://github.com/StrikerX3/Ymir/blob/d98d4d249fd6c00d7f213fee6e1a0b6bbf460869/apps/ymir-sdl3/src/app/services/save_state_service.cpp)
- [Ymir backup-RAM path source](https://github.com/StrikerX3/Ymir/blob/d98d4d249fd6c00d7f213fee6e1a0b6bbf460869/apps/ymir-sdl3/src/app/shared_context.cpp)
