"""Real local SFTP server + :class:`SFTPProvider` contract tests.

A minimal in-process SFTP server (paramiko acting as both client and
server, bound to ``127.0.0.1`` on an ephemeral port, rooted at a pytest
``tmp_path``) exercises the provider against the real protocol instead of
only mocks, per the project's SFTP testing requirements. No network access
or external infrastructure is required.
"""

from __future__ import annotations

import os
import socket
import threading
from pathlib import Path

import paramiko
import pytest

from romcloud.core.exceptions import (
    ProviderAuthError,
    ProviderHostKeyMismatchError,
    ProviderHostKeyUnknownError,
    ProviderNotReachableError,
)
from romcloud.infrastructure.providers.sftp import (
    SFTPProvider,
    fingerprint_of,
    probe_host_key,
)

# A client that disconnects right after a rejected auth/host-key handshake
# leaves the server-side thread's blocking read to observe a reset socket —
# expected in these adversarial tests, not a real failure.
pytestmark = pytest.mark.filterwarnings(
    "ignore::pytest.PytestUnhandledThreadExceptionWarning"
)

TEST_USERNAME = "romcloud-test"
TEST_PASSWORD = "correct-horse-battery-staple"


class _StubServer(paramiko.ServerInterface):
    def __init__(self) -> None:
        self.event = threading.Event()

    def check_channel_request(self, kind, chanid):
        return paramiko.OPEN_SUCCEEDED

    def check_auth_password(self, username, password):
        if username == TEST_USERNAME and password == TEST_PASSWORD:
            return paramiko.AUTH_SUCCESSFUL
        return paramiko.AUTH_FAILED

    def get_allowed_auths(self, username):
        return "password"


class _StubHandle(paramiko.SFTPHandle):
    def stat(self):
        return paramiko.SFTPAttributes.from_stat(os.fstat(self.readfile.fileno()))


class _StubSFTPServer(paramiko.SFTPServerInterface):
    """No virtual chroot: the client always sends full real paths (matching
    how :class:`SFTPProvider` itself works), so canonicalization is a no-op
    and only separator style needs reconciling with the host OS."""

    def canonicalize(self, path):
        return path

    def _real(self, path: str) -> str:
        return path.replace("/", os.sep) if os.sep != "/" else path

    def list_folder(self, path):
        real = self._real(path)
        try:
            names = os.listdir(real)
        except OSError as exc:
            return paramiko.SFTPServer.convert_errno(exc.errno)
        out = []
        for name in names:
            attr = paramiko.SFTPAttributes.from_stat(os.stat(os.path.join(real, name)))
            attr.filename = name
            out.append(attr)
        return out

    def stat(self, path):
        try:
            return paramiko.SFTPAttributes.from_stat(os.stat(self._real(path)))
        except OSError as exc:
            return paramiko.SFTPServer.convert_errno(exc.errno)

    def lstat(self, path):
        try:
            return paramiko.SFTPAttributes.from_stat(os.lstat(self._real(path)))
        except OSError as exc:
            return paramiko.SFTPServer.convert_errno(exc.errno)

    def open(self, path, flags, attr):
        real = self._real(path)
        try:
            mode = getattr(attr, "st_mode", None) or 0o666
            fd = os.open(real, flags, mode)
        except OSError as exc:
            return paramiko.SFTPServer.convert_errno(exc.errno)
        if flags & os.O_WRONLY:
            fstr = "ab" if flags & os.O_APPEND else "wb"
        elif flags & os.O_RDWR:
            fstr = "a+b" if flags & os.O_APPEND else "r+b"
        else:
            fstr = "rb"
        handle = _StubHandle(flags)
        f = os.fdopen(fd, fstr)
        handle.readfile = f
        handle.writefile = f
        return handle

    def remove(self, path):
        try:
            os.remove(self._real(path))
        except OSError as exc:
            return paramiko.SFTPServer.convert_errno(exc.errno)
        return paramiko.SFTP_OK

    def mkdir(self, path, attr):
        try:
            os.mkdir(self._real(path))
        except OSError as exc:
            return paramiko.SFTPServer.convert_errno(exc.errno)
        return paramiko.SFTP_OK


class _SftpTestServer:
    def __init__(self, root: Path) -> None:
        self.host_key = paramiko.RSAKey.generate(2048)
        self.fingerprint = fingerprint_of(self.host_key)
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(5)
        self.port = self._sock.getsockname()[1]
        self._root = str(root)
        self._stop = False
        self._thread = threading.Thread(target=self._serve_forever, daemon=True)
        self._thread.start()

    def _serve_forever(self) -> None:
        self._sock.settimeout(0.5)
        while not self._stop:
            try:
                client_sock, _ = self._sock.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            threading.Thread(
                target=self._handle_one, args=(client_sock,), daemon=True
            ).start()

    def _handle_one(self, client_sock: socket.socket) -> None:
        transport = paramiko.Transport(client_sock)
        transport.add_server_key(self.host_key)

        transport.set_subsystem_handler("sftp", paramiko.SFTPServer, _StubSFTPServer)
        server = _StubServer()
        try:
            transport.start_server(server=server)
        except paramiko.SSHException:
            return
        channel = transport.accept(5)
        if channel is None:
            return
        server.event.wait(2)

    def close(self) -> None:
        self._stop = True
        try:
            self._sock.close()
        except OSError:
            pass


@pytest.fixture
def sftp_server(tmp_path: Path):
    root = tmp_path / "sftp-root"
    root.mkdir()
    server = _SftpTestServer(root)
    yield server, root
    server.close()


_UNSET = object()


def _provider(server, *, password=TEST_PASSWORD, fingerprint=_UNSET, probe_writable=False):
    return SFTPProvider(
        host="127.0.0.1",
        username=TEST_USERNAME,
        port=server.port,
        password=password,
        trusted_host_key_fingerprint=(
            server.fingerprint if fingerprint is _UNSET else fingerprint
        ),
        probe_writable=probe_writable,
        connect_timeout=5.0,
        operation_timeout=5.0,
    )


class TestHostKeyTrust:
    def test_probe_host_key_matches_server_fingerprint(self, sftp_server):
        server, _root = sftp_server
        key_type, fingerprint = probe_host_key("127.0.0.1", server.port, timeout=5.0)
        assert fingerprint == server.fingerprint
        assert key_type

    def test_unknown_host_key_is_rejected(self, sftp_server):
        server, root = sftp_server
        provider = _provider(server, fingerprint=None)
        with pytest.raises(ProviderHostKeyUnknownError):
            provider.is_reachable(root.as_posix())

    def test_mismatched_host_key_is_rejected(self, sftp_server):
        server, root = sftp_server
        provider = SFTPProvider(
            host="127.0.0.1",
            username=TEST_USERNAME,
            port=server.port,
            password=TEST_PASSWORD,
            trusted_host_key_fingerprint="SHA256:" + "A" * 43,
            connect_timeout=5.0,
            operation_timeout=5.0,
        )
        with pytest.raises(ProviderHostKeyMismatchError):
            provider.list_systems(root.as_posix())


class TestAuthentication:
    def test_wrong_password_raises_auth_error(self, sftp_server):
        server, root = sftp_server
        provider = _provider(server, password="wrong-password")
        with pytest.raises(ProviderAuthError):
            provider.list_systems(root.as_posix())

    def test_unreachable_host_raises_not_reachable(self, sftp_server):
        server, root = sftp_server
        provider = SFTPProvider(
            host="127.0.0.1",
            username=TEST_USERNAME,
            port=1,  # nothing listens on port 1
            password=TEST_PASSWORD,
            trusted_host_key_fingerprint=server.fingerprint,
            connect_timeout=1.0,
            operation_timeout=1.0,
        )
        with pytest.raises(ProviderNotReachableError):
            provider.list_systems(str(root))


class TestReadOperations:
    def test_list_systems_and_entries(self, sftp_server):
        server, root = sftp_server
        (root / "psx").mkdir()
        (root / "psx" / "Game.bin").write_bytes(b"abc123")
        (root / "psx" / "Game.cue").write_bytes(b"cue")
        provider = _provider(server)
        assert provider.list_systems(root.as_posix()) == ["psx"]
        entries = {e.name: e for e in provider.list_entries(root.as_posix(), "psx")}
        assert entries["Game.bin"].size_bytes == 6
        assert not entries["Game.bin"].is_directory

    def test_get_size_and_read_text(self, sftp_server):
        server, root = sftp_server
        (root / "meta.txt").write_text("hello world", encoding="utf-8")
        provider = _provider(server)
        assert provider.get_size((root / "meta.txt").as_posix()) == 11
        assert provider.read_text((root / "meta.txt").as_posix()) == "hello world"

    def test_transfer_to_downloads_file(self, sftp_server, tmp_path):
        server, root = sftp_server
        (root / "rom.bin").write_bytes(b"payload-bytes")
        provider = _provider(server)
        dest = tmp_path / "downloaded.bin"
        provider.transfer_to((root / "rom.bin").as_posix(), str(dest))
        assert dest.read_bytes() == b"payload-bytes"

    def test_walk_enumerates_nested_files(self, sftp_server):
        server, root = sftp_server
        (root / "a" / "b").mkdir(parents=True)
        (root / "a" / "b" / "f.txt").write_text("x", encoding="utf-8")
        (root / "top.txt").write_text("y", encoding="utf-8")
        provider = _provider(server)
        entries = {e.relative_path: e for e in provider.walk(root.as_posix())}
        assert set(entries) == {"a/b/f.txt", "top.txt"}

    def test_open_binary_streams_content(self, sftp_server):
        server, root = sftp_server
        (root / "stream.bin").write_bytes(b"stream-me")
        provider = _provider(server)
        with provider.open_binary((root / "stream.bin").as_posix()) as fh:
            assert fh.read() == b"stream-me"

    def test_is_reachable_false_for_missing_path(self, sftp_server):
        server, root = sftp_server
        provider = _provider(server)
        assert provider.is_reachable((root / "does-not-exist").as_posix()) is False


class TestWriteValidation:
    def test_read_only_probe_never_writes(self, sftp_server):
        server, root = sftp_server
        provider = _provider(server, probe_writable=False)
        result = provider.validate_access(root.as_posix())
        assert result.connected and result.read_verified
        assert result.write_verified is None
        assert list(root.iterdir()) == []

    def test_writable_probe_creates_verifies_and_cleans_up(self, sftp_server):
        server, root = sftp_server
        provider = _provider(server, probe_writable=True)
        result = provider.validate_access(root.as_posix())
        assert result.ok
        assert result.write_verified is True
        assert result.cleanup_verified is True
        assert list(root.iterdir()) == []

    def test_probe_never_touches_existing_files(self, sftp_server):
        server, root = sftp_server
        (root / "existing.sav").write_bytes(b"do-not-touch")
        provider = _provider(server, probe_writable=True)
        provider.validate_access(root.as_posix())
        assert (root / "existing.sav").read_bytes() == b"do-not-touch"


class TestCapabilities:
    def test_sftp_has_no_filesystem_semantics_or_durable_transactions(self, sftp_server):
        server, _root = sftp_server
        provider = _provider(server)
        assert provider.capabilities.has_filesystem_semantics is False
        assert provider.capabilities.supports_durable_transactions is False
        assert provider.capabilities.can_resume_download is False
