from __future__ import annotations

from pathlib import Path

import pytest

from romcloud.core.exceptions import ConfigurationError
from romcloud.core.update_channels import UpdateChannel, parse_channel, resolve_channel
from romcloud.infrastructure.config import (
    AppConfig,
    CacheConfig,
    SourceConfig,
    load_config,
    write_config,
    write_update_channel,
)
from romcloud.lifecycle import update as upd


def _config(tmp_path: Path, channel: str = "stable") -> AppConfig:
    return AppConfig(
        source=SourceConfig(provider="none", rom_root=""),
        cache=CacheConfig(path=str(tmp_path / "cache")),
        local_roms_path=str(tmp_path / "roms"),
        data_path=str(tmp_path / "data"),
        update_channel=channel,
    )


def test_channel_resolver_is_a_strict_allowlist() -> None:
    assert resolve_channel().ref == "main"
    assert resolve_channel("stable").ref == "main"
    assert resolve_channel("develop").ref == "develop"
    for unsafe in ("main", "feature/foo", "../main", "https://example", "$(id)", "main; id"):
        with pytest.raises(ValueError):
            parse_channel(unsafe)


def test_existing_config_without_channel_defaults_to_stable(tmp_path: Path) -> None:
    path = tmp_path / "romcloud.toml"
    path.write_text('[source]\nprovider = "none"\n', encoding="utf-8")

    assert load_config(str(path), resolve_paths=False).update_channel == "stable"


@pytest.mark.parametrize("channel", ["stable", "develop"])
def test_channel_round_trip(tmp_path: Path, channel: str) -> None:
    path = tmp_path / "romcloud.toml"
    write_config(_config(tmp_path, channel), str(path))

    assert load_config(str(path), resolve_paths=False).update_channel == channel


def test_invalid_config_channel_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "romcloud.toml"
    path.write_text(
        'update_channel = "feature/foo"\n[source]\nprovider = "none"\n',
        encoding="utf-8",
    )

    with pytest.raises(ConfigurationError, match="invalid update channel"):
        load_config(str(path), resolve_paths=False)


def test_channel_patch_preserves_unrelated_configuration(tmp_path: Path) -> None:
    path = tmp_path / "romcloud.toml"
    original = '# user comment\n[source]\nprovider = "none"\n[custom]\nvalue = 42\n'
    path.write_text(original, encoding="utf-8")

    write_update_channel(UpdateChannel.DEVELOP, str(path))

    text = path.read_text(encoding="utf-8")
    assert text.endswith(original)
    assert load_config(str(path), resolve_paths=False).update_channel == "develop"


def test_develop_build_identity_includes_revision() -> None:
    build = upd.BuildInfo(
        version="0.9.29",
        commit="a" * 40,
        commit_short="a" * 12,
        build_date="x",
        source="github:stryph4/romcloud@develop",
        channel="develop",
    )

    assert build.display_identity == "ROMCloud 0.9.29 — Develop • " + "a" * 12


def test_stable_build_identity_is_uncluttered() -> None:
    build = upd.BuildInfo(
        version="0.9.29",
        commit="a" * 40,
        commit_short="a" * 12,
        build_date="x",
        source="github:stryph4/romcloud@main",
    )

    assert build.display_identity == "ROMCloud 0.9.29 — Stable"


@pytest.mark.parametrize("channel", ["stable", "develop"])
def test_repair_forwards_only_resolved_channel(monkeypatch, tmp_path: Path, channel: str) -> None:
    captured = []
    result = upd.UpdateResult(
        previous=None,
        new=upd.BuildInfo("1", None, None, "x", "s", channel=channel),
    )
    monkeypatch.setattr(
        upd,
        "perform_update",
        lambda *args, **kwargs: captured.append(kwargs["channel"]) or result,
    )

    assert upd.perform_repair(tmp_path, tmp_path / "python", channel=channel) == result
    assert captured == [UpdateChannel(channel)]
