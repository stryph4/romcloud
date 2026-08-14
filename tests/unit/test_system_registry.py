from __future__ import annotations

import json
from pathlib import Path

import pytest

from romcloud.integrations.batocera.system_registry import (
    EffectiveSystemRegistry,
    SystemRegistryError,
    load_effective_system_registry,
)


def _config(*systems: tuple[str, str]) -> str:
    body = "".join(
        f"<system><name>{name}</name><extension>{extensions}</extension>"
        f"<command>launch</command></system>"
        for name, extensions in systems
    )
    return f"<?xml version='1.0'?><systemList>{body}</systemList>"


def _load(tmp_path: Path) -> EffectiveSystemRegistry:
    return load_effective_system_registry(
        cache_path=tmp_path / "data" / "registry.json",
        user_config_dir=tmp_path / "user",
        system_config_dir=tmp_path / "share",
        legacy_config_dir=tmp_path / "etc",
    )


def test_base_precedence_matches_batocera(tmp_path: Path) -> None:
    for directory in (tmp_path / "user", tmp_path / "share", tmp_path / "etc"):
        directory.mkdir()
    (tmp_path / "etc" / "es_systems.cfg").write_text(_config(("nes", ".etc")))
    (tmp_path / "share" / "es_systems.cfg").write_text(_config(("nes", ".stock")))
    (tmp_path / "user" / "es_systems.cfg").write_text(_config(("nes", ".user")))
    (tmp_path / "user" / "es_systems_custom.cfg").write_text(
        _config(("nes", ".custom"))
    )

    registry = _load(tmp_path)

    assert registry.get("nes").extensions == frozenset({".custom"})


def test_overlays_patch_tags_add_systems_and_ignore_romcloud_overlay(
    tmp_path: Path,
) -> None:
    user = tmp_path / "user"
    share = tmp_path / "share"
    user.mkdir()
    share.mkdir()
    (share / "es_systems.cfg").write_text(
        _config(("nes", ".nes .zip"), ("psx", ".cue .bin"))
    )
    (share / "es_systems_a.cfg").write_text(_config(("nes", ".nes .7z")))
    (user / "es_systems_z.cfg").write_text(_config(("custom", ".foo")))
    (user / "es_systems_romcloud.cfg").write_text(
        _config(("nes", ".nes .romcloud"), ("psx", ".romcloud"))
    )

    registry = _load(tmp_path)

    assert registry.get("nes").extensions == frozenset({".nes", ".7z"})
    assert registry.get("psx").extensions == frozenset({".cue", ".bin"})
    assert registry.get("custom").extensions == frozenset({".foo"})


def test_empty_overlay_extension_removes_launch_eligibility(tmp_path: Path) -> None:
    user = tmp_path / "user"
    share = tmp_path / "share"
    user.mkdir()
    share.mkdir()
    (share / "es_systems.cfg").write_text(_config(("nes", ".nes")))
    (user / "es_systems_disable.cfg").write_text(
        "<systemList><system><name>nes</name><extension/></system></systemList>"
    )

    registry = _load(tmp_path)

    assert registry.get("nes").extensions == frozenset()


def test_empty_effective_command_does_not_authorize_launch(tmp_path: Path) -> None:
    share = tmp_path / "share"
    share.mkdir()
    (share / "es_systems.cfg").write_text(_config(("nes", ".nes")))
    user = tmp_path / "user"
    user.mkdir()
    (user / "es_systems_disable.cfg").write_text(
        "<systemList><system><name>nes</name><command/></system></systemList>"
    )

    registry = _load(tmp_path)

    assert registry.get("nes").accepts("Mario.nes") is False


def test_live_success_persists_and_malformed_config_uses_last_known_good(
    tmp_path: Path,
) -> None:
    share = tmp_path / "share"
    share.mkdir()
    base = share / "es_systems.cfg"
    base.write_text(_config(("nes", ".nes")))
    first = _load(tmp_path)
    assert first.from_last_known_good is False
    base.write_text("<broken")

    fallback = _load(tmp_path)

    assert fallback.from_last_known_good is True
    assert fallback.get("nes").accepts("Mario.NES")


def test_unavailable_without_last_known_good_fails_closed(tmp_path: Path) -> None:
    with pytest.raises(SystemRegistryError, match="skipped safely"):
        _load(tmp_path)


def test_valid_live_registry_survives_lkg_write_failure(
    tmp_path: Path, monkeypatch
) -> None:
    share = tmp_path / "share"
    share.mkdir()
    (share / "es_systems.cfg").write_text(_config(("nes", ".nes")))
    monkeypatch.setattr(
        "romcloud.integrations.batocera.system_registry.atomic_write_text",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(PermissionError("read-only")),
    )

    registry = _load(tmp_path)

    assert registry.get("nes").accepts("Mario.nes")
    assert registry.from_last_known_good is False


def test_corrupt_last_known_good_is_not_trusted(tmp_path: Path) -> None:
    cache = tmp_path / "data" / "registry.json"
    cache.parent.mkdir()
    cache.write_text(json.dumps({"version": 2, "systems": {"nes": "not-a-spec"}}))

    with pytest.raises(SystemRegistryError):
        _load(tmp_path)
