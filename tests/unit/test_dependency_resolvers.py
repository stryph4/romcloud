from __future__ import annotations

from pathlib import Path

import pytest

from romcloud.core.exceptions import DependencyResolutionError
from romcloud.core.models.game import Game, GameAsset
from romcloud.infrastructure.providers.local import LocalFilesystemProvider
from romcloud.services.dependencies import DependencyResolverRegistry


def _game(root: Path, system: str, relative_path: str) -> Game:
    path = root / relative_path
    return Game.create(
        system,
        path.stem,
        "local",
        str(root),
        [
            GameAsset(
                path.name,
                relative_path,
                size_bytes=path.stat().st_size,
                is_primary=True,
            )
        ],
    )


def _resolve(root: Path, system: str, relative_path: str, **limits) -> Game:
    return DependencyResolverRegistry(
        LocalFilesystemProvider(), source_root=str(root), **limits
    ).resolve(_game(root, system, relative_path))


def _paths(game: Game) -> list[str]:
    return [asset.relative_path for asset in game.assets]


def test_m3u_resolves_multiple_chd_files_in_playlist_order(tmp_path: Path) -> None:
    system = tmp_path / "psx"
    system.mkdir()
    (system / "Game.m3u").write_text("# multi-disc\nDisc 1.chd\nDisc 2.chd\n")
    (system / "Disc 1.chd").write_bytes(b"one")
    (system / "Disc 2.chd").write_bytes(b"two")

    resolved = _resolve(tmp_path, "psx", "psx/Game.m3u")

    assert _paths(resolved) == [
        "psx/Game.m3u",
        "psx/Disc 1.chd",
        "psx/Disc 2.chd",
    ]
    assert resolved.primary_asset.relative_path == "psx/Game.m3u"


def test_m3u_recursively_resolves_cue_and_bin_tracks(tmp_path: Path) -> None:
    system = tmp_path / "psx"
    system.mkdir()
    (system / "Game.m3u").write_text("Disc 1.cue\n")
    (system / "Disc 1.cue").write_text(
        'FILE "Disc 1 (Track 1).bin" BINARY\n'
        'FILE "Disc 1 (Track 2).bin" BINARY\n'
    )
    (system / "Disc 1 (Track 1).bin").write_bytes(b"one")
    (system / "Disc 1 (Track 2).bin").write_bytes(b"two")

    assert _paths(_resolve(tmp_path, "psx", "psx/Game.m3u")) == [
        "psx/Game.m3u",
        "psx/Disc 1.cue",
        "psx/Disc 1 (Track 1).bin",
        "psx/Disc 1 (Track 2).bin",
    ]


@pytest.mark.parametrize(
    ("body", "message"),
    [
        ("Missing.chd\n", "missing"),
        ("../../outside.chd\n", "escapes"),
        ("/absolute/disc.chd\n", "Absolute"),
        ("C:\\absolute\\disc.chd\n", "Absolute"),
    ],
)
def test_m3u_rejects_missing_and_unsafe_references(
    tmp_path: Path, body: str, message: str
) -> None:
    system = tmp_path / "psx"
    system.mkdir()
    (system / "Game.m3u").write_text(body)

    with pytest.raises(DependencyResolutionError, match=message):
        _resolve(tmp_path, "psx", "psx/Game.m3u")


def test_dependency_cycle_is_rejected(tmp_path: Path) -> None:
    system = tmp_path / "psx"
    system.mkdir()
    (system / "A.m3u").write_text("B.m3u\n")
    (system / "B.m3u").write_text("A.m3u\n")

    with pytest.raises(DependencyResolutionError, match="cycle"):
        _resolve(tmp_path, "psx", "psx/A.m3u")


def test_symlink_escape_is_rejected(tmp_path: Path) -> None:
    system = tmp_path / "psx"
    outside = tmp_path / "outside"
    system.mkdir()
    outside.mkdir()
    (outside / "Disc.chd").write_bytes(b"outside")
    (system / "Game.m3u").write_text("escaped/Disc.chd\n")
    try:
        (system / "escaped").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("symlink creation is unavailable on this host")

    with pytest.raises(DependencyResolutionError, match="Cannot inspect"):
        _resolve(tmp_path, "psx", "psx/Game.m3u")


def test_recursion_and_dependency_count_bounds(tmp_path: Path) -> None:
    system = tmp_path / "psx"
    system.mkdir()
    (system / "A.m3u").write_text("B.m3u\n")
    (system / "B.m3u").write_text("Disc.chd\n")
    (system / "Disc.chd").write_bytes(b"disc")

    with pytest.raises(DependencyResolutionError, match="recursion"):
        _resolve(tmp_path, "psx", "psx/A.m3u", max_depth=1)
    with pytest.raises(DependencyResolutionError, match="count"):
        _resolve(tmp_path, "psx", "psx/A.m3u", max_dependencies=1)


def test_xbox360_matches_batocera_one_line_marker_format(tmp_path: Path) -> None:
    xbla = tmp_path / "xbox360" / "xbla"
    xbla.mkdir(parents=True)
    marker = xbla / "Castle Crashers.xbox360"
    marker.write_text("Castle Crashers\r\nignored second line")
    payload = xbla / "Castle Crashers"
    payload.write_bytes(b"xbox-live-arcade-payload")

    resolved = _resolve(
        tmp_path, "xbox360", "xbox360/xbla/Castle Crashers.xbox360"
    )

    assert _paths(resolved) == [
        "xbox360/xbla/Castle Crashers.xbox360",
        "xbox360/xbla/Castle Crashers",
    ]


def test_ccd_resolves_required_img_and_optional_sub(tmp_path: Path) -> None:
    system = tmp_path / "psx"
    system.mkdir()
    (system / "Game.ccd").write_text("[CloneCD]\nVersion=3\n")
    (system / "Game.img").write_bytes(b"image")
    (system / "Game.sub").write_bytes(b"subchannels")

    assert _paths(_resolve(tmp_path, "psx", "psx/Game.ccd")) == [
        "psx/Game.ccd",
        "psx/Game.img",
        "psx/Game.sub",
    ]

    (system / "Game.sub").unlink()
    assert _paths(_resolve(tmp_path, "psx", "psx/Game.ccd")) == [
        "psx/Game.ccd",
        "psx/Game.img",
    ]


def test_gdi_resolves_quoted_and_unquoted_track_files(tmp_path: Path) -> None:
    system = tmp_path / "dreamcast"
    system.mkdir()
    (system / "Game.gdi").write_text(
        '2\n1 0 4 2352 track01.bin 0\n2 45000 0 2352 "track 02.raw" 0\n'
    )
    (system / "track01.bin").write_bytes(b"one")
    (system / "track 02.raw").write_bytes(b"two")

    assert _paths(_resolve(tmp_path, "dreamcast", "dreamcast/Game.gdi")) == [
        "dreamcast/Game.gdi",
        "dreamcast/track01.bin",
        "dreamcast/track 02.raw",
    ]
