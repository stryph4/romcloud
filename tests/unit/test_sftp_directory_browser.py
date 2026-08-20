from __future__ import annotations

import pytest

from romcloud.services.directory_browser import (
    normalize_sftp_directory,
    sftp_parent,
)


def test_sftp_root_and_case_sensitive_nested_paths_are_preserved():
    assert normalize_sftp_directory("/") == "/"
    assert normalize_sftp_directory("/Roms/PlayStation2") == "/Roms/PlayStation2"
    assert normalize_sftp_directory(r"\Roms\PS3") == "/Roms/PS3"


def test_sftp_parent_navigation_stops_at_provider_visible_root():
    assert sftp_parent("/Roms/PS2") == "/Roms"
    assert sftp_parent("/Roms") == "/"
    assert sftp_parent("/") == "/"


def test_sftp_parent_respects_a_configured_boundary():
    assert sftp_parent("/home/account/Roms", boundary="/home/account") == "/home/account"
    assert sftp_parent("/home/account", boundary="/home/account") == "/home/account"
    with pytest.raises(ValueError, match="outside"):
        sftp_parent("/other", boundary="/home/account")


@pytest.mark.parametrize(
    "path",
    [
        "sftp://host/Roms",
        "ssh://host/Roms",
        "user@host:/Roms",
        "//host/Roms",
        "/sftp://host/Roms",
    ],
)
def test_protocol_or_host_prefixed_sftp_paths_are_rejected(path):
    with pytest.raises(ValueError, match="without sftp:// or a server name"):
        normalize_sftp_directory(path)


def test_sftp_traversal_is_rejected():
    with pytest.raises(ValueError, match="server-visible root"):
        normalize_sftp_directory("/Roms/../private")
