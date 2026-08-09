"""Unit tests for ports_gfx.controller_config — per-controller custom
mapping persistence, derived from the romcloud_bin path (no romcloud
import), stored outside the reconciled ports-gfx/ tree."""

from __future__ import annotations

from pathlib import Path

from ports_gfx.controller_config import (
    load_all_mappings,
    load_mapping,
    make_loader,
    make_saver,
    mappings_path,
    save_mapping,
    state_dir,
)


class TestStateDir:
    def test_derived_from_romcloud_bin_parent_parent(self, tmp_path: Path):
        romcloud_bin = tmp_path / "romcloud" / "bin" / "romcloud"
        assert state_dir(str(romcloud_bin)) == tmp_path / "romcloud" / "ports-gfx-state"

    def test_kept_outside_the_reconciled_ports_gfx_directory(self, tmp_path: Path):
        romcloud_bin = tmp_path / "romcloud" / "bin" / "romcloud"
        result = state_dir(str(romcloud_bin))
        assert "ports-gfx-state" == result.name
        assert result != tmp_path / "romcloud" / "ports-gfx"


class TestLoadSaveRoundTrip:
    def test_missing_file_returns_empty(self, tmp_path: Path):
        romcloud_bin = tmp_path / "romcloud" / "bin" / "romcloud"
        assert load_all_mappings(str(romcloud_bin)) == {}
        assert load_mapping(str(romcloud_bin), "guid:abc") is None

    def test_save_then_load_round_trips(self, tmp_path: Path):
        romcloud_bin = tmp_path / "romcloud" / "bin" / "romcloud"
        save_mapping(str(romcloud_bin), "guid:abc", {"button": {"0": "confirm"}})
        assert load_mapping(str(romcloud_bin), "guid:abc") == {"button": {"0": "confirm"}}

    def test_saving_one_controller_preserves_another(self, tmp_path: Path):
        romcloud_bin = tmp_path / "romcloud" / "bin" / "romcloud"
        save_mapping(str(romcloud_bin), "guid:abc", {"button": {"0": "confirm"}})
        save_mapping(str(romcloud_bin), "guid:def", {"button": {"1": "back"}})

        assert load_mapping(str(romcloud_bin), "guid:abc") == {"button": {"0": "confirm"}}
        assert load_mapping(str(romcloud_bin), "guid:def") == {"button": {"1": "back"}}

    def test_corrupt_file_returns_empty_not_raise(self, tmp_path: Path):
        romcloud_bin = tmp_path / "romcloud" / "bin" / "romcloud"
        path = mappings_path(str(romcloud_bin))
        path.parent.mkdir(parents=True)
        path.write_text("not json{{{")
        assert load_all_mappings(str(romcloud_bin)) == {}

    def test_save_is_atomic_no_leftover_tmp_file(self, tmp_path: Path):
        romcloud_bin = tmp_path / "romcloud" / "bin" / "romcloud"
        save_mapping(str(romcloud_bin), "guid:abc", {"button": {}})
        leftover = list(state_dir(str(romcloud_bin)).glob("*.tmp"))
        assert leftover == []


class TestLoaderSaverClosures:
    def test_loader_reads_saved_mapping(self, tmp_path: Path):
        romcloud_bin = str(tmp_path / "romcloud" / "bin" / "romcloud")
        save_mapping(romcloud_bin, "guid:xyz", {"button": {"3": "up"}})
        loader = make_loader(romcloud_bin)
        assert loader("guid:xyz") == {"button": {"3": "up"}}
        assert loader("guid:missing") is None

    def test_saver_persists_via_the_same_path(self, tmp_path: Path):
        romcloud_bin = str(tmp_path / "romcloud" / "bin" / "romcloud")
        saver = make_saver(romcloud_bin)
        saver("guid:xyz", {"button": {"3": "up"}})
        assert load_mapping(romcloud_bin, "guid:xyz") == {"button": {"3": "up"}}
