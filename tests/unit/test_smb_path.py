from __future__ import annotations

import pytest

from romcloud.infrastructure.smb_path import normalize_smb_remote_path


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("", ""),
        ("/", ""),
        ("ROMS", "ROMS"),
        ("/ROMS/", "ROMS"),
        (r"Libraries\ROMs", "Libraries/ROMs"),
    ],
)
def test_normalizes_safe_share_relative_paths(value, expected):
    assert normalize_smb_remote_path(value) == expected


@pytest.mark.parametrize(
    "value",
    [
        "../ROMS",
        "./ROMS",
        "Libraries/../ROMS",
        "Libraries/./ROMS",
        "Libraries//ROMS",
        "Libraries/ROMS//",
        "//server/share/ROMS",
        r"C:\ROMS",
        'Libraries/"ROMS',
        "Libraries/ROMS\nSAVES",
        "Libraries/ROMS\x00SAVES",
        "Libraries,ROMS",
    ],
)
def test_rejects_unsafe_share_relative_paths(value):
    with pytest.raises(ValueError):
        normalize_smb_remote_path(value)
