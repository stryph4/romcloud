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

from romcloud.core.cancellation import TransferCancellationToken
from romcloud.core.exceptions import (
    ProviderAuthError,
    ProviderHostKeyMismatchError,
    ProviderHostKeyUnknownError,
    ProviderNotReachableError,
    TransferCancelledError,
)
from romcloud.core.models.game import Game, GameAsset
from romcloud.integrations.batocera.catalog import CatalogService
from romcloud.integrations.batocera.system_registry import EffectiveSystemRegistry
from romcloud.infrastructure.providers.sftp import (
    SFTPProvider,
    fingerprint_of,
    probe_host_key,
)
from romcloud.infrastructure.repositories.download import StagingRepository
from romcloud.services.transfer import TransferService

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
    def __init__(self, flags, operations, handle_id):
        super().__init__(flags)
        self._operations = operations
        self._handle_id = handle_id

    def stat(self):
        return paramiko.SFTPAttributes.from_stat(os.fstat(self.readfile.fileno()))

    def read(self, offset, length):
        self._operations.append(("read", self._handle_id, offset, length))
        return super().read(offset, length)

    def write(self, offset, data):
        self._operations.append(("write", self._handle_id, offset, len(data)))
        return super().write(offset, data)


class _StubSFTPServer(paramiko.SFTPServerInterface):
    """No virtual chroot: the client always sends full real paths (matching
    how :class:`SFTPProvider` itself works), so canonicalization is a no-op
    and only separator style needs reconciling with the host OS."""

    def __init__(self, server, recorder):
        super().__init__(server)
        self._recorder = recorder

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
        handle_id = self._recorder["next_handle"]
        self._recorder["next_handle"] += 1
        self._recorder["events"].append(("open", handle_id, path, flags))
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
        handle = _StubHandle(flags, self._recorder["events"], handle_id)
        f = os.fdopen(fd, fstr)
        handle.readfile = f
        handle.writefile = f
        return handle

    def remove(self, path):
        self._recorder["events"].append(("remove", path))
        try:
            os.remove(self._real(path))
        except OSError as exc:
            return paramiko.SFTPServer.convert_errno(exc.errno)
        return paramiko.SFTP_OK

    def mkdir(self, path, attr):
        self._recorder["events"].append(("mkdir", path))
        try:
            os.mkdir(self._real(path))
        except OSError as exc:
            return paramiko.SFTPServer.convert_errno(exc.errno)
        return paramiko.SFTP_OK

    def rename(self, oldpath, newpath):
        self._recorder["events"].append(("rename", oldpath, newpath))
        return paramiko.SFTP_OP_UNSUPPORTED


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
        self._recorder = {"events": [], "next_handle": 0}
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

        transport.set_subsystem_handler(
            "sftp", paramiko.SFTPServer, _StubSFTPServer, self._recorder
        )
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

    @property
    def operations(self):
        return self._recorder["events"]

    def clear_operations(self) -> None:
        self._recorder["events"].clear()


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
    def test_directory_listing_preserves_case_and_excludes_files(
        self, sftp_server
    ):
        server, root = sftp_server
        (root / "Roms").mkdir()
        (root / "PlayStation2").mkdir()
        (root / "README.txt").write_text("not a directory", encoding="utf-8")

        assert _provider(server).list_systems(root.as_posix()) == [
            "PlayStation2",
            "Roms",
        ]

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

    def test_transfer_service_caches_nested_directory_package(
        self, sftp_server, tmp_path, monkeypatch
    ):
        server, root = sftp_server
        source_root = root / "roms"
        package = source_root / "ps3" / "GAME"
        usrdir = package / "PS3_GAME" / "USRDIR"
        (usrdir / "DATA" / "LEVELS").mkdir(parents=True)
        (package / "PS3_GAME" / "EMPTY").mkdir()
        (usrdir / "EBOOT.BIN").write_bytes(b"eboot")
        (package / ".package-meta").write_bytes(b"meta")
        (usrdir / "DATA" / "config.dat").write_bytes(b"config")
        (usrdir / "DATA" / "LEVELS" / "level0.bin").write_bytes(b"level")
        game = Game.create(
            "ps3",
            "GAME",
            "sftp",
            source_root.as_posix(),
            [GameAsset("GAME", "ps3/GAME", size_bytes=None, is_primary=True)],
        )
        cache_root = tmp_path / "cache"
        provider = _provider(server)
        original_connect = provider._connect
        connection_count = 0

        def counted_connect():
            nonlocal connection_count
            connection_count += 1
            return original_connect()

        monkeypatch.setattr(provider, "_connect", counted_connect)

        final = Path(
            TransferService(
                provider=provider, cache_root=str(cache_root)
            ).transfer(game)
        )

        assert final == cache_root / "ps3" / "GAME"
        assert {
            path.relative_to(final).as_posix(): path.read_bytes()
            for path in final.rglob("*")
            if path.is_file()
        } == {
            ".package-meta": b"meta",
            "PS3_GAME/USRDIR/EBOOT.BIN": b"eboot",
            "PS3_GAME/USRDIR/DATA/config.dat": b"config",
            "PS3_GAME/USRDIR/DATA/LEVELS/level0.bin": b"level",
        }
        assert (final / "PS3_GAME" / "EMPTY").is_dir()
        assert not (cache_root / ".partial" / "ps3" / "GAME").exists()
        assert connection_count == 1

    def test_transfer_to_propagates_provider_neutral_cancellation(
        self, sftp_server, tmp_path
    ):
        server, root = sftp_server
        payload = b"x" * (512 * 1024)
        (root / "large.bin").write_bytes(payload)
        provider = _provider(server)
        dest = tmp_path / "partial.bin"
        cancellation = TransferCancellationToken()

        def cancel(done, total):
            cancellation.cancel()
            cancellation.raise_if_cancelled()

        with pytest.raises(TransferCancelledError):
            provider.transfer_to((root / "large.bin").as_posix(), str(dest), cancel)

        assert dest.exists()
        assert dest.stat().st_size < len(payload)

    def test_walk_enumerates_nested_files(self, sftp_server):
        server, root = sftp_server
        (root / "a" / "b").mkdir(parents=True)
        (root / "a" / "b" / "f.txt").write_text("x", encoding="utf-8")
        (root / "top.txt").write_text("y", encoding="utf-8")
        provider = _provider(server)
        entries = {e.relative_path: e for e in provider.walk(root.as_posix())}
        assert set(entries) == {"a", "a/b", "a/b/f.txt", "top.txt"}
        assert entries["a"].is_directory
        assert entries["a/b"].is_directory

    def test_open_binary_streams_content(self, sftp_server):
        server, root = sftp_server
        (root / "stream.bin").write_bytes(b"stream-me")
        provider = _provider(server)
        with provider.open_binary((root / "stream.bin").as_posix()) as fh:
            assert fh.read() == b"stream-me"

    def test_transfer_service_resumes_verified_offset_with_zero_remote_writes(
        self, sftp_server, tmp_path, db, monkeypatch
    ):
        server, root = sftp_server
        source_root = root / "roms"
        source = source_root / "ps2" / "Resume.iso"
        source.parent.mkdir(parents=True)
        payload = b"A" * (3 * 1024 * 1024)
        source.write_bytes(payload)
        game = Game.create(
            "ps2", "Resume", "sftp", source_root.as_posix(),
            [GameAsset("Resume.iso", "ps2/Resume.iso", len(payload), True)],
        )
        provider = _provider(server)
        closed_sessions: list[bool] = []
        original_connect = provider._connect

        def tracked_connect():
            client, sftp = original_connect()
            original_close = client.close

            def tracked_close():
                closed_sessions.append(True)
                original_close()

            monkeypatch.setattr(client, "close", tracked_close)
            return client, sftp

        monkeypatch.setattr(provider, "_connect", tracked_connect)
        staging = StagingRepository(db)
        service = TransferService(
            provider, str(tmp_path / "cache"), staging_repository=staging
        )
        all_events = []

        cancellation = TransferCancellationToken()
        with pytest.raises(TransferCancelledError):
            service.transfer(
                game,
                lambda done, _total: cancellation.cancel()
                if done >= 1024 * 1024 else None,
                cancellation,
            )
        part = tmp_path / "cache" / ".partial" / "ps2" / "Resume.iso.part"
        checkpoint = part.stat().st_size
        record = staging.get_file("ps2/Resume.iso", "")
        assert checkpoint == 1024 * 1024
        assert record is not None and record.checkpoint_bytes == checkpoint
        assert provider._transfer_session_state.get() is None
        all_events.extend(server.operations)

        server.clear_operations()
        final = Path(service.transfer(game))
        resume_events = list(server.operations)
        all_events.extend(resume_events)
        first_read_offset: dict[int, int] = {}
        for event in resume_events:
            if event[0] == "read":
                first_read_offset.setdefault(event[1], event[2])
        # One handle verifies from byte zero; the subsequent transfer handle
        # begins at the exact durable checkpoint.
        assert 0 in first_read_offset.values()
        assert checkpoint in first_read_offset.values()
        assert final.read_bytes() == payload

        # Build another retained prefix, then replace the source with different
        # same-size bytes. Verification may read the old offset, but the actual
        # transfer must restart from byte zero rather than append stale data.
        final.unlink()
        cancellation = TransferCancellationToken()
        server.clear_operations()
        with pytest.raises(TransferCancelledError):
            service.transfer(
                game,
                lambda done, _total: cancellation.cancel()
                if done >= checkpoint else None,
                cancellation,
            )
        all_events.extend(server.operations)
        replacement = b"B" * len(payload)
        source.write_bytes(replacement)
        server.clear_operations()
        assert Path(service.transfer(game)).read_bytes() == replacement
        mismatch_events = list(server.operations)
        all_events.extend(mismatch_events)
        mismatch_first_reads: dict[int, int] = {}
        for event in mismatch_events:
            if event[0] == "read":
                mismatch_first_reads.setdefault(event[1], event[2])
        assert mismatch_first_reads
        assert set(mismatch_first_reads.values()) == {0}

        write_flags = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND
        mutations = [
            event for event in all_events
            if event[0] in {"write", "remove", "mkdir", "rename"}
            or (event[0] == "open" and event[3] & write_flags)
        ]
        assert mutations == []
        assert len(closed_sessions) == 4
        assert provider._transfer_session_state.get() is None
        assert part.exists() is False

    def test_is_reachable_false_for_missing_path(self, sftp_server):
        server, root = sftp_server
        provider = _provider(server)
        assert provider.is_reachable((root / "does-not-exist").as_posix()) is False


class TestCatalogScanPerformance:
    """Round-trip budgets for representative remote ROM layouts."""

    @staticmethod
    def _refresh(
        server,
        root: Path,
        system: str,
        extensions: set[str],
        game_repo,
        proxy_repo,
        local_roms_dir: Path,
    ):
        provider = _provider(server)
        service = CatalogService(
            provider=provider,
            game_repo=game_repo,
            proxy_repo=proxy_repo,
            local_roms_root=str(local_roms_dir),
            source_root=root.as_posix(),
            system_registry=EffectiveSystemRegistry.from_extensions(
                {system: extensions}
            ),
        )
        result = service.refresh()
        assert result.errors == []
        metrics = provider.catalog_scan_metrics[system]
        assert metrics.connections == 1
        assert metrics.elapsed_seconds >= 0
        return result, metrics

    def test_scan_scope_caches_duplicate_listing_and_file_attributes(
        self, sftp_server
    ):
        server, root = sftp_server
        (root / "nes").mkdir()
        rom = root / "nes" / "Mario.nes"
        rom.write_bytes(b"rom")
        provider = _provider(server)

        with provider.catalog_system_scan("nes"):
            assert len(provider.list_entries(root.as_posix(), "nes")) == 1
            assert len(provider.list_entries(root.as_posix(), "nes")) == 1
            assert provider.get_size(rom.as_posix()) == 3

        metrics = provider.catalog_scan_metrics["nes"]
        assert metrics.connections == 1
        assert metrics.directory_listings == 1
        assert metrics.entries_examined == 1
        assert metrics.stat_calls == 0

    def test_single_file_system_uses_one_listing_and_no_metadata_calls(
        self, sftp_server, game_repo, proxy_repo, local_roms_dir
    ):
        server, root = sftp_server
        (root / "nes").mkdir()
        (root / "nes" / "Mario.nes").write_bytes(b"rom")

        result, metrics = self._refresh(
            server, root, "nes", {".nes"}, game_repo, proxy_repo, local_roms_dir
        )

        assert result.added == 1
        assert metrics.directory_listings == 1
        assert metrics.recursive_directory_visits == 1
        assert metrics.entries_examined == 1
        assert metrics.stat_calls == metrics.file_opens == metrics.content_reads == 0

    def test_directory_game_stops_at_launchable_package(
        self, sftp_server, game_repo, proxy_repo, local_roms_dir
    ):
        server, root = sftp_server
        payload = root / "ps3" / "Example.ps3" / "PS3_GAME" / "USRDIR"
        payload.mkdir(parents=True)
        (payload / "EBOOT.BIN").write_bytes(b"payload")

        result, metrics = self._refresh(
            server, root, "ps3", {".ps3"}, game_repo, proxy_repo, local_roms_dir
        )

        assert result.added == 1
        assert metrics.directory_listings == 1
        assert metrics.entries_examined == 1
        assert metrics.stat_calls == metrics.file_opens == metrics.content_reads == 0

    def test_cue_multitrack_reuses_listdir_metadata_instead_of_stat_per_track(
        self, sftp_server, game_repo, proxy_repo, local_roms_dir
    ):
        server, root = sftp_server
        system_root = root / "dreamcast"
        system_root.mkdir()
        (system_root / "Game.cue").write_text(
            'FILE "Track 01.bin" BINARY\n'
            'FILE "Track 02.bin" BINARY\n'
            'FILE "Track 03.bin" BINARY\n',
            encoding="utf-8",
        )
        for number in range(1, 4):
            (system_root / f"Track {number:02d}.bin").write_bytes(b"track")

        result, metrics = self._refresh(
            server,
            root,
            "dreamcast",
            {".cue", ".bin"},
            game_repo,
            proxy_repo,
            local_roms_dir,
        )

        assert result.added == 1
        assert metrics.directory_listings == 1
        assert metrics.entries_examined == 4
        # Before snapshot reuse this layout made four separate stat calls:
        # one for the cue and one for every referenced track.
        assert metrics.stat_calls == 0
        assert metrics.file_opens == metrics.content_reads == 1

    def test_m3u_multidisc_needs_only_name_and_size_metadata(
        self, sftp_server, game_repo, proxy_repo, local_roms_dir
    ):
        server, root = sftp_server
        system_root = root / "dreamcast"
        system_root.mkdir()
        (system_root / "Collection.m3u").write_text(
            "Disc 1.chd\nDisc 2.chd\n", encoding="utf-8"
        )
        (system_root / "Disc 1.chd").write_bytes(b"one")
        (system_root / "Disc 2.chd").write_bytes(b"two")

        result, metrics = self._refresh(
            server,
            root,
            "dreamcast",
            {".m3u", ".chd"},
            game_repo,
            proxy_repo,
            local_roms_dir,
        )

        # Preserve current catalog semantics: the playlist and its launchable
        # members remain independently discoverable.
        assert result.added == 3
        assert metrics.directory_listings == 1
        assert metrics.entries_examined == 3
        assert metrics.stat_calls == metrics.file_opens == metrics.content_reads == 0

    def test_nas_and_gamelist_metadata_directories_are_not_recursed(
        self, sftp_server, game_repo, proxy_repo, local_roms_dir
    ):
        server, root = sftp_server
        system_root = root / "neogeocd"
        (system_root / "@eaDir" / "thumbs").mkdir(parents=True)
        (system_root / "@eaDir" / "thumbs" / "index.jpg").write_bytes(b"thumb")
        (system_root / "images").mkdir()
        (system_root / "images" / "Game.png").write_bytes(b"image")
        (system_root / "Game.chd").write_bytes(b"game")
        (system_root / "gamelist.xml").write_text(
            "<gameList><game><image>./images/Game.png</image></game></gameList>",
            encoding="utf-8",
        )

        result, metrics = self._refresh(
            server,
            root,
            "neogeocd",
            {".chd"},
            game_repo,
            proxy_repo,
            local_roms_dir,
        )

        assert result.added == 1
        assert metrics.directory_listings == 1
        assert metrics.recursive_directory_visits == 1
        assert metrics.entries_examined == 4
        assert metrics.stat_calls == 0
        assert metrics.file_opens == metrics.content_reads == 1


class TestReadOnlyValidation:
    def test_read_only_probe_never_writes(self, sftp_server):
        server, root = sftp_server
        provider = _provider(server, probe_writable=False)
        result = provider.validate_access(root.as_posix())
        assert result.connected and result.read_verified
        assert result.write_verified is None
        assert result.cleanup_verified is None
        assert result.writable is False
        assert list(root.iterdir()) == []

    def test_legacy_writable_probe_flag_stays_read_only(self, sftp_server):
        server, root = sftp_server
        provider = _provider(server, probe_writable=True)
        result = provider.validate_access(root.as_posix())
        assert result.ok
        assert result.write_verified is None
        assert result.cleanup_verified is None
        assert result.writable is False
        assert "read-only" in result.detail.lower()
        assert list(root.iterdir()) == []

    def test_validation_never_touches_existing_files(self, sftp_server):
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
        assert provider.capabilities.can_resume_download is True
