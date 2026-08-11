"""Unit tests for romcloud.infrastructure.hardware_identity.

Covers placeholder filtering, ordered probing, and live material
recomputation — all exercised against a fake DMI directory, never the
real ``/sys/class/dmi/id`` on the test machine.
"""

from __future__ import annotations

from pathlib import Path

from romcloud.infrastructure import hardware_identity as hw


def _write_dmi(base: Path, **fields: str) -> Path:
    base.mkdir(parents=True, exist_ok=True)
    for name, value in fields.items():
        (base / name).write_text(value + "\n", encoding="utf-8")
    return base


class TestIsUsableIdentifier:
    def test_rejects_empty_and_none(self):
        assert hw.is_usable_identifier("") is False
        assert hw.is_usable_identifier(None) is False

    def test_rejects_known_placeholders_case_and_space_insensitive(self):
        for value in (
            "Default string",
            "  DEFAULT STRING  ",
            "To Be Filled By O.E.M.",
            "None",
            "N/A",
            "System Serial Number",
            "00000000-0000-0000-0000-000000000000",
        ):
            assert hw.is_usable_identifier(value) is False

    def test_rejects_all_same_character_values(self):
        assert hw.is_usable_identifier("00000000") is False
        assert hw.is_usable_identifier("ffffffff-ffff-ffff-ffff-ffffffffffff") is False

    def test_accepts_a_real_looking_value(self):
        assert hw.is_usable_identifier("PF3ABCDE") is True
        assert hw.is_usable_identifier("4c4c4544-0044-3210-8035-b9c04f435931") is True


class TestReadDmiIdentifier:
    def test_missing_file_returns_none(self, tmp_path: Path):
        assert hw.read_dmi_identifier("product_uuid", base=tmp_path) is None

    def test_placeholder_file_returns_none(self, tmp_path: Path):
        _write_dmi(tmp_path, board_serial="Default string")
        assert hw.read_dmi_identifier("board_serial", base=tmp_path) is None

    def test_usable_value_is_returned_stripped(self, tmp_path: Path):
        _write_dmi(tmp_path, board_serial="  REALSERIAL123  \n")
        assert hw.read_dmi_identifier("board_serial", base=tmp_path) == "REALSERIAL123"

    def test_unknown_identifier_name_returns_none(self, tmp_path: Path):
        _write_dmi(tmp_path, board_serial="REALSERIAL123")
        assert hw.read_dmi_identifier("not_a_real_field", base=tmp_path) is None


class TestProbeBindingTypes:
    def test_empty_when_nothing_usable(self, tmp_path: Path):
        assert hw.probe_binding_types(base=tmp_path) == ()

    def test_orders_by_priority_and_filters_placeholders(self, tmp_path: Path):
        _write_dmi(
            tmp_path,
            product_uuid="",  # e.g. Steam Deck: absent
            board_serial="REALBOARD123",
            product_serial="REALSYSTEM456",
        )
        assert hw.probe_binding_types(base=tmp_path) == ("board_serial", "product_serial")

    def test_placeholder_product_uuid_is_skipped_but_others_used(self, tmp_path: Path):
        _write_dmi(
            tmp_path,
            product_uuid="00000000-0000-0000-0000-000000000000",
            board_serial="REALBOARD123",
        )
        assert hw.probe_binding_types(base=tmp_path) == ("board_serial",)

    def test_all_three_usable_preserves_priority_order(self, tmp_path: Path):
        _write_dmi(
            tmp_path,
            product_uuid="4c4c4544-0044-3210-8035-b9c04f435931",
            board_serial="REALBOARD123",
            product_serial="REALSYSTEM456",
        )
        assert hw.probe_binding_types(base=tmp_path) == (
            "product_uuid",
            "board_serial",
            "product_serial",
        )


class TestGatherBindingMaterial:
    def test_empty_types_yields_empty_material(self, tmp_path: Path):
        assert hw.gather_binding_material((), base=tmp_path) == ""

    def test_recomputes_live_from_disk(self, tmp_path: Path):
        _write_dmi(tmp_path, board_serial="REALBOARD123")
        material = hw.gather_binding_material(("board_serial",), base=tmp_path)
        assert "REALBOARD123" in material

    def test_material_changes_if_identifier_becomes_unavailable(self, tmp_path: Path):
        _write_dmi(tmp_path, board_serial="REALBOARD123")
        before = hw.gather_binding_material(("board_serial",), base=tmp_path)

        (tmp_path / "board_serial").write_text("Default string\n", encoding="utf-8")
        after = hw.gather_binding_material(("board_serial",), base=tmp_path)

        assert before != after
        assert after == ""  # the identifier is no longer usable at all

    def test_material_changes_if_identifier_value_changes(self, tmp_path: Path):
        _write_dmi(tmp_path, board_serial="REALBOARD123")
        before = hw.gather_binding_material(("board_serial",), base=tmp_path)

        (tmp_path / "board_serial").write_text("DIFFERENTBOARD456\n", encoding="utf-8")
        after = hw.gather_binding_material(("board_serial",), base=tmp_path)

        assert before != after


class TestReadMachineId:
    def test_missing_file_returns_none(self, tmp_path: Path):
        assert hw.read_machine_id(path=tmp_path / "machine-id") is None

    def test_reads_stripped_value(self, tmp_path: Path):
        path = tmp_path / "machine-id"
        path.write_text("abc123def456\n", encoding="utf-8")
        assert hw.read_machine_id(path=path) == "abc123def456"

    def test_empty_file_returns_none(self, tmp_path: Path):
        path = tmp_path / "machine-id"
        path.write_text("\n", encoding="utf-8")
        assert hw.read_machine_id(path=path) is None
