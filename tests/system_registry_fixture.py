"""Shared explicit Batocera launch registry for catalog unit tests."""

from romcloud.integrations.batocera.system_registry import EffectiveSystemRegistry

TEST_SYSTEM_REGISTRY = EffectiveSystemRegistry.from_extensions(
    {
        "ps2": {".iso", ".chd"},
        "nes": {".nes", ".zip"},
        "snes": {".sfc", ".smc", ".zip"},
        "psx": {".cue", ".bin", ".iso", ".chd"},
        "saturn": {".cue", ".bin", ".iso", ".chd"},
        "ps3": {".ps3", ".psn"},
        "xbox": {".iso", ".xiso"},
        "xbox360": {".iso", ".xex", ".xbox360"},
        "gamecube": {".iso", ".rvz"},
    }
)
