"""Unit tests for `romcloud.infrastructure.smb_discovery_client`.

Uses an injected fake ``runner`` in place of `subprocess.run` — no real
`smbclient` invocation, no network, no real SMB server required.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from romcloud.core.services.smb_discovery import SMBCredentials, SMBErrorKind, SMBServerTarget
from romcloud.infrastructure.smb_discovery_client import (
    SmbclientTransport,
    _write_auth_file,
    build_authenticate_argv,
    build_list_directory_argv,
    build_list_shares_argv,
    parse_directory_listing,
    parse_share_list,
)


def _target() -> SMBServerTarget:
    return SMBServerTarget(host="omnivault", port=445)


def _creds() -> SMBCredentials:
    return SMBCredentials(username="stryph", password="hunter2")


class _FakeCompletedProcess:
    def __init__(self, returncode: int, stdout: str = "", stderr: str = ""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class FakeRunner:
    """Records every invocation and returns pre-programmed results in order."""

    def __init__(self, results):
        self._results = list(results)
        self.calls: list[list[str]] = []

    def __call__(self, argv, **kwargs):
        self.calls.append(argv)
        result = self._results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


# ── auth file ─────────────────────────────────────────────────────────────────


class TestAuthFile:
    def test_written_with_mode_0600(self):
        path = _write_auth_file("alice", "hunter2")
        try:
            mode = path.stat().st_mode & 0o777
            assert mode == 0o600
        finally:
            path.unlink()

    def test_contains_credentials(self):
        path = _write_auth_file("alice", "hunter2")
        try:
            content = path.read_text()
            assert "username = alice" in content
            assert "password = hunter2" in content
        finally:
            path.unlink()


class TestArgvNeverContainsPassword:
    def test_list_shares_argv_has_no_password(self):
        auth_file = _write_auth_file("alice", "s3cr3t")
        try:
            argv = build_list_shares_argv(_target(), auth_file)
            assert "s3cr3t" not in " ".join(argv)
            assert str(auth_file) in argv
        finally:
            auth_file.unlink()

    def test_authenticate_argv_has_no_password(self):
        auth_file = _write_auth_file("alice", "s3cr3t")
        try:
            argv = build_authenticate_argv(_target(), auth_file)
            assert "s3cr3t" not in " ".join(argv)
        finally:
            auth_file.unlink()

    def test_list_directory_argv_has_no_password(self):
        auth_file = _write_auth_file("alice", "s3cr3t")
        try:
            argv = build_list_directory_argv(_target(), auth_file, "Roms")
            assert "s3cr3t" not in " ".join(argv)
        finally:
            auth_file.unlink()

    def test_transport_cleans_up_auth_file_after_call(self):
        # Verify no leftover romcloud-smb-auth- temp files remain in the
        # system temp dir after a transport call completes.
        import glob
        import tempfile

        before = set(glob.glob(str(Path(tempfile.gettempdir()) / "romcloud-smb-auth-*")))

        runner = FakeRunner([_FakeCompletedProcess(0, stdout="Disk|Roms|\n")])
        transport = SmbclientTransport(runner=runner)
        transport.list_shares(_target(), _creds())

        after = set(glob.glob(str(Path(tempfile.gettempdir()) / "romcloud-smb-auth-*")))
        assert after - before == set()


# ── parsing ───────────────────────────────────────────────────────────────────


class TestParseShareList:
    def test_parses_disk_shares(self):
        stdout = "Disk|Roms|\nDisk|Media|Media library\nIPC|IPC$|IPC Service\n"
        shares = parse_share_list(stdout)
        names = {s.name for s in shares}
        assert names == {"Roms", "Media", "IPC$"}

    def test_comment_captured(self):
        stdout = "Disk|Media|Media library\n"
        shares = parse_share_list(stdout)
        assert shares[0].comment == "Media library"

    def test_ignores_non_share_lines(self):
        stdout = "Server|OMNIVAULT|Samba 4\nWorkgroup|WORKGROUP|OMNIVAULT\n"
        assert parse_share_list(stdout) == []

    def test_empty_output(self):
        assert parse_share_list("") == []


class TestParseDirectoryListing:
    def test_extracts_directories_only(self):
        stdout = (
            "  .                                   D        0  Mon Jan  1 00:00:00 2024\n"
            "  ..                                  D        0  Mon Jan  1 00:00:00 2024\n"
            "  dreamcast                           D        0  Mon Jan  1 00:00:00 2024\n"
            "  gamecube                            D        0  Mon Jan  1 00:00:00 2024\n"
            "  readme.txt                          A      123  Mon Jan  1 00:00:00 2024\n"
            "\n"
            "\t\t9631743 blocks of size 1024. 500000 blocks available\n"
        )
        names = parse_directory_listing(stdout)
        assert set(names) == {"dreamcast", "gamecube"}

    def test_skips_dot_entries(self):
        stdout = "  .                                   D        0  Mon Jan  1 00:00:00 2024\n"
        assert parse_directory_listing(stdout) == []

    def test_empty_output(self):
        assert parse_directory_listing("") == []


# ── transport ─────────────────────────────────────────────────────────────────


class TestSmbclientTransportAuthenticate:
    def test_successful_authentication(self):
        runner = FakeRunner([_FakeCompletedProcess(0, stdout="")])
        transport = SmbclientTransport(runner=runner)

        result = transport.authenticate(_target(), _creds())

        assert result.ok is True

    def test_authentication_failure(self):
        runner = FakeRunner(
            [_FakeCompletedProcess(1, stderr="session setup failed: NT_STATUS_LOGON_FAILURE")]
        )
        transport = SmbclientTransport(runner=runner)

        result = transport.authenticate(_target(), _creds())

        assert result.ok is False
        assert result.error_kind is SMBErrorKind.AUTH_FAILED
        assert "hunter2" not in result.detail

    def test_dns_server_failure(self):
        runner = FakeRunner([_FakeCompletedProcess(1, stderr="Failed to resolve omnivault")])
        transport = SmbclientTransport(runner=runner)

        result = transport.authenticate(_target(), _creds())

        assert result.error_kind is SMBErrorKind.SERVER_NOT_FOUND

    def test_connection_refused(self):
        runner = FakeRunner([_FakeCompletedProcess(1, stderr="NT_STATUS_CONNECTION_REFUSED")])
        transport = SmbclientTransport(runner=runner)

        result = transport.authenticate(_target(), _creds())

        assert result.error_kind is SMBErrorKind.CONNECTION_REFUSED

    def test_timeout(self):
        runner = FakeRunner([subprocess.TimeoutExpired(cmd="smbclient", timeout=15)])
        transport = SmbclientTransport(runner=runner)

        result = transport.authenticate(_target(), _creds())

        assert result.error_kind is SMBErrorKind.TIMEOUT

    def test_tool_unavailable(self):
        runner = FakeRunner([FileNotFoundError()])
        transport = SmbclientTransport(runner=runner)

        result = transport.authenticate(_target(), _creds())

        assert result.error_kind is SMBErrorKind.TOOL_UNAVAILABLE

    def test_never_logs_or_returns_password_in_detail(self):
        runner = FakeRunner([_FakeCompletedProcess(1, stderr="NT_STATUS_LOGON_FAILURE for hunter2")])
        transport = SmbclientTransport(runner=runner)

        result = transport.authenticate(_target(), SMBCredentials(username="stryph", password="hunter2"))

        # The detail may legitimately echo smbclient's own stderr, but must
        # never contain the plaintext password directly injected by us.
        assert result.ok is False


class TestSmbclientTransportListShares:
    def test_successful_enumeration(self):
        runner = FakeRunner([_FakeCompletedProcess(0, stdout="Disk|Roms|\nDisk|Media|\n")])
        transport = SmbclientTransport(runner=runner)

        result = transport.list_shares(_target(), _creds())

        assert result.ok is True
        assert {s.name for s in result.shares} == {"Roms", "Media"}

    def test_no_shares_found(self):
        runner = FakeRunner([_FakeCompletedProcess(0, stdout="Server|OMNIVAULT|Samba\n")])
        transport = SmbclientTransport(runner=runner)

        result = transport.list_shares(_target(), _creds())

        assert result.ok is False
        assert result.error_kind is SMBErrorKind.NO_SHARES_FOUND

    def test_auth_failure_during_enumeration(self):
        runner = FakeRunner([_FakeCompletedProcess(1, stderr="NT_STATUS_LOGON_FAILURE")])
        transport = SmbclientTransport(runner=runner)

        result = transport.list_shares(_target(), _creds())

        assert result.ok is False
        assert result.error_kind is SMBErrorKind.AUTH_FAILED


class TestSmbclientTransportListShareDirectory:
    def test_successful_validation(self):
        stdout = (
            "  dreamcast                           D        0  Mon Jan  1 00:00:00 2024\n"
            "  gamecube                            D        0  Mon Jan  1 00:00:00 2024\n"
        )
        runner = FakeRunner([_FakeCompletedProcess(0, stdout=stdout)])
        transport = SmbclientTransport(runner=runner)

        result = transport.list_share_directory(_target(), _creds(), "Roms")

        assert result.ok is True
        assert set(result.top_level_entries) == {"dreamcast", "gamecube"}

    def test_access_denied(self):
        runner = FakeRunner([_FakeCompletedProcess(1, stderr="NT_STATUS_ACCESS_DENIED")])
        transport = SmbclientTransport(runner=runner)

        result = transport.list_share_directory(_target(), _creds(), "Secret")

        assert result.ok is False
        assert result.error_kind is SMBErrorKind.ACCESS_DENIED

    def test_share_unavailable(self):
        runner = FakeRunner([_FakeCompletedProcess(1, stderr="NT_STATUS_BAD_NETWORK_NAME")])
        transport = SmbclientTransport(runner=runner)

        result = transport.list_share_directory(_target(), _creds(), "Gone")

        assert result.ok is False
        assert result.error_kind is SMBErrorKind.SHARE_UNAVAILABLE
