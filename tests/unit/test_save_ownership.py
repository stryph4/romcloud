"""Pure tests for catalog-backed automatic save attribution."""

from romcloud.core.models.game import Game, GameAsset
from romcloud.core.save_ownership import ManagedSaveOwnershipPolicy


def _game(system: str, filename: str) -> Game:
    return Game.create(
        system,
        filename.rsplit(".", 1)[0],
        "local",
        "/roms",
        [GameAsset(filename, f"{system}/{filename}", is_primary=True)],
    )


def test_catalog_game_identity_attributes_root_named_save_and_state() -> None:
    policy = ManagedSaveOwnershipPolicy([_game("snes", "Chrono Trigger.sfc")])

    assert policy.is_managed_path("snes/Chrono Trigger.srm") is True
    assert policy.is_managed_path("snes/Chrono Trigger.state.auto") is True


def test_ordinary_local_game_is_not_managed_by_catalog_identity() -> None:
    policy = ManagedSaveOwnershipPolicy([_game("snes", "Chrono Trigger.sfc")])

    assert policy.is_managed_path("snes/Local Game.srm") is False


def test_same_system_does_not_grant_ownership_to_unrelated_save() -> None:
    policy = ManagedSaveOwnershipPolicy([_game("n64", "Managed Game.z64")])

    assert policy.is_managed_path("n64/Managed Game.eep") is True
    assert policy.is_managed_path("n64/Local Game.eep") is False


def test_local_rom_with_same_stem_makes_attribution_ambiguous() -> None:
    policy = ManagedSaveOwnershipPolicy(
        [_game("snes", "Same Name.sfc")],
        ambiguous_local_stems={"snes": frozenset({"same name"})},
    )

    assert policy.is_managed_path("snes/Same Name.srm") is False


def test_rpcs3_title_id_paths_can_be_attributed_but_shared_trees_cannot() -> None:
    policy = ManagedSaveOwnershipPolicy([_game("ps3", "Demon's Souls [BLUS30443].ps3")])

    assert policy.is_managed_path(
        "ps3/rpcs3/dev_hdd0/home/00000001/savedata/BLUS30443-SAVE/SAVE.DAT"
    ) is True
    assert policy.is_managed_path(
        "ps3/rpcs3/dev_hdd0/game/BLUS30443/USRDIR/EBOOT.BIN"
    ) is True
    assert policy.is_managed_path(
        "ps3/rpcs3/dev_hdd0/savedata/vmc/MemoryCard.VM1"
    ) is False
    assert policy.is_managed_path(
        "ps3/rpcs3/dev_hdd0/home/00000001/trophy/NPWR00001_00/TROPUSR.DAT"
    ) is False


def test_shared_memory_card_tree_is_conservatively_not_attributed() -> None:
    policy = ManagedSaveOwnershipPolicy([_game("ps2", "Managed Game.iso")])

    assert policy.is_managed_path("ps2/pcsx2/Mcd001.ps2") is False
    assert policy.is_managed_path("ps2/pcsx2/sstates/12345678.p2s") is False
