"""Adapter registry selected declaratively by the save-layout registry."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from pathlib import Path

from romcloud.core.save_containers import (
    OpaqueContainerError,
    OpaqueReason,
    ProbeResult,
    SaveContainerAdapter,
)
from romcloud.infrastructure.pcsx2_folder_card import (
    ConservativePs2FolderGroupingPolicy,
    Pcsx2FolderMemoryCardAdapter,
    Ps2FolderGroupingPolicy,
)
from romcloud.infrastructure.ps1_memory_card import Ps1RawMemoryCardAdapter


@dataclass(frozen=True)
class SaveContainerRegistry:
    adapters: Mapping[str, SaveContainerAdapter]

    def adapter(self, adapter_id: str) -> SaveContainerAdapter:
        return self.adapters[adapter_id]

    def get(self, adapter_id: str) -> SaveContainerAdapter | None:
        return self.adapters.get(adapter_id)


class _OpaquePcsx2FileCardAdapter:
    """Declarative reason carrier; monolithic PS2 cards are never parsed."""

    adapter_id = "pcsx2-monolithic-file-card"
    schema_version = 1

    def probe(self, source: Path) -> ProbeResult:
        return ProbeResult(
            False, opaque_reason=OpaqueReason.UNSUPPORTED_PS2_FILE_CARD
        )

    def snapshot(self, source: Path, *, container_id: str):
        raise OpaqueContainerError(OpaqueReason.UNSUPPORTED_PS2_FILE_CARD)

    def enumerate_entries(self, snapshot):
        raise OpaqueContainerError(OpaqueReason.UNSUPPORTED_PS2_FILE_CARD)

    def identify_entry(self, entry):
        raise OpaqueContainerError(OpaqueReason.UNSUPPORTED_PS2_FILE_CARD)

    def hash_entry(self, entry):
        raise OpaqueContainerError(OpaqueReason.UNSUPPORTED_PS2_FILE_CARD)

    def extract_entry(self, source, snapshot, identity):
        raise OpaqueContainerError(OpaqueReason.UNSUPPORTED_PS2_FILE_CARD)

    def replace_entry(self, candidate, replacement):
        raise OpaqueContainerError(OpaqueReason.UNSUPPORTED_PS2_FILE_CARD)

    def remove_entry(self, candidate, identity):
        raise OpaqueContainerError(OpaqueReason.UNSUPPORTED_PS2_FILE_CARD)

    def rebuild(self, destination, plan, candidate):
        raise OpaqueContainerError(OpaqueReason.UNSUPPORTED_PS2_FILE_CARD)

    def validate(self, candidate, expected):
        raise OpaqueContainerError(OpaqueReason.UNSUPPORTED_PS2_FILE_CARD)


def build_save_container_registry(
    *, ps2_grouping_policy: Ps2FolderGroupingPolicy | None = None
) -> SaveContainerRegistry:
    ps1 = Ps1RawMemoryCardAdapter()
    ps2 = Pcsx2FolderMemoryCardAdapter(
        ps2_grouping_policy or ConservativePs2FolderGroupingPolicy()
    )
    opaque_ps2 = _OpaquePcsx2FileCardAdapter()
    return SaveContainerRegistry(
        adapters={
            ps1.adapter_id: ps1,
            ps2.adapter_id: ps2,
            opaque_ps2.adapter_id: opaque_ps2,
        }
    )


DEFAULT_SAVE_CONTAINER_REGISTRY = build_save_container_registry()
