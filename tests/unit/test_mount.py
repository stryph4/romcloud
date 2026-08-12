"""Unit tests for romcloud.infrastructure.mount.

Covers: mount-state detection via /proc/mounts content (no `mountpoint`
command dependency), credential-safe argv building, reachability checks
with injected resolver/connector/clock (no real network or sleeping),
and mount/unmount orchestration with a fake subprocess runner.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
import subprocess
import threading
import time

import pytest

from romcloud.core.exceptions import MountError, ProviderAuthError, ProviderNotReachableError
from romcloud.infrastructure import mount

_SAMPLE_PROC_MOUNTS = (
    "sysfs /sys sysfs rw 0 0\n"
    "proc /proc proc rw 0 0\n"
    "//192.168.1.50/ROMs /userdata/romcloud/source cifs ro,relatime 0 0\n"
    "tmpfs /tmp\\040with\\040space tmpfs rw 0 0\n"
)


class TestIsMounted:
    def test_detects_mounted_target(self):
        assert mount.is_mounted("/userdata/romcloud/source", _SAMPLE_PROC_MOUNTS) is True

    def test_unmounted_target_returns_false(self):
        assert mount.is_mounted("/userdata/somewhere-else", _SAMPLE_PROC_MOUNTS) is False

    def test_handles_octal_escaped_spaces(self):
        assert mount.is_mounted("/tmp with space", _SAMPLE_PROC_MOUNTS) is True

    def test_trailing_slash_normalised(self):
        assert mount.is_mounted("/userdata/romcloud/source/", _SAMPLE_PROC_MOUNTS) is True

    def test_is_target_mounted_reads_real_file(self, tmp_path):
        fake_proc_mounts = tmp_path / "mounts"
        fake_proc_mounts.write_text(_SAMPLE_PROC_MOUNTS)
        assert mount.is_target_mounted(
            "/userdata/romcloud/source", proc_mounts_path=str(fake_proc_mounts)
        ) is True
        assert mount.is_target_mounted(
            "/nope", proc_mounts_path=str(fake_proc_mounts)
        ) is False

    def test_is_target_mounted_missing_proc_mounts_raises_clear_error(self, tmp_path):
        with pytest.raises(MountError):
            mount.is_target_mounted("/x", proc_mounts_path=str(tmp_path / "does-not-exist"))

    def test_writable_check_distinguishes_ro_and_rw_mounts(self):
        mounts = _SAMPLE_PROC_MOUNTS + (
            "//192.168.1.50/ROMCloud /userdata/romcloud/remote cifs rw,relatime 0 0\n"
        )

        assert mount.is_mounted_writable("/userdata/romcloud/source", mounts) is False
        assert mount.is_mounted_writable("/userdata/romcloud/remote", mounts) is True
        assert mount.is_mounted_writable("/userdata/not-mounted", mounts) is False

    def test_read_only_check_distinguishes_ro_and_rw_mounts(self, tmp_path):
        mounts = tmp_path / "mounts"
        mounts.write_text(
            "//nas/ROMs /mnt/roms cifs ro 0 0\n"
            "//nas/ROMs /mnt/saves cifs rw 0 0\n"
        )

        assert mount.is_target_mounted_read_only(
            "/mnt/roms", proc_mounts_path=str(mounts)
        ) is True
        assert mount.is_target_mounted_read_only(
            "/mnt/saves", proc_mounts_path=str(mounts)
        ) is False

    @pytest.mark.parametrize(
        ("server", "share", "read_only", "expected"),
        [
            ("192.168.1.50", "ROMCloud", False, True),
            ("other-nas", "ROMCloud", False, False),
            ("192.168.1.50", "Other", False, False),
            ("192.168.1.50", "ROMCloud", True, False),
            (None, "ROMCloud", False, True),
        ],
    )
    def test_cifs_identity_requires_expected_source_and_mode(
        self, server, share, read_only, expected
    ):
        mounts = _SAMPLE_PROC_MOUNTS + (
            "//192.168.1.50/ROMCloud /userdata/romcloud/remote cifs rw,relatime 0 0\n"
        )

        assert mount.is_mounted_cifs_target(
            "/userdata/romcloud/remote",
            mounts,
            server=server,
            share=share,
            read_only=read_only,
        ) is expected

    def test_cifs_identity_rejects_non_cifs_mount(self):
        mounts = "/dev/sda1 /userdata/romcloud/remote ext4 rw 0 0\n"

        assert mount.is_mounted_cifs_target(
            "/userdata/romcloud/remote",
            mounts,
            server="nas",
            share="ROMCloud",
            read_only=False,
        ) is False


class TestBuildArgv:
    def test_mount_argv_never_contains_password(self):
        argv = mount.build_mount_argv(
            "nas.local", "ROMs", "/mnt/roms", Path("/creds/file"), read_only=True
        )
        assert not any("hunter2" in a for a in argv)
        assert "mount" in argv
        assert "-t" in argv and "cifs" in argv

    def test_mount_argv_uses_credentials_file_option(self):
        argv = mount.build_mount_argv(
            "nas.local", "ROMs", "/mnt/roms", Path("/creds/file")
        )
        options = argv[argv.index("-o") + 1]
        assert "credentials=/creds/file" in options

    def test_mount_argv_read_only_by_default(self):
        argv = mount.build_mount_argv("nas.local", "ROMs", "/mnt/roms", Path("/creds/file"))
        options = argv[argv.index("-o") + 1]
        assert ",ro" in options or options.endswith("ro")

    def test_mount_argv_read_write_when_requested(self):
        argv = mount.build_mount_argv(
            "nas.local", "ROMs", "/mnt/roms", Path("/creds/file"), read_only=False
        )
        options = argv[argv.index("-o") + 1]
        assert "rw" in options.split(",")

    def test_mount_argv_share_path_correct(self):
        argv = mount.build_mount_argv("nas.local", "ROMs", "/mnt/roms", Path("/creds/file"))
        assert "//nas.local/ROMs" in argv
        assert "/mnt/roms" in argv

    def test_mount_argv_uses_selected_share_relative_directory(self):
        argv = mount.build_mount_argv(
            "nas.local",
            "ROMs",
            "/mnt/roms",
            Path("/creds/file"),
            remote_path="Libraries/Roms",
        )
        options = argv[argv.index("-o") + 1]
        assert "prefixpath=Libraries/Roms" in options.split(",")

    def test_unmount_argv(self):
        assert mount.build_unmount_argv("/mnt/roms") == ["umount", "/mnt/roms"]


class TestCheckReachable:
    def test_success(self):
        result = mount.check_reachable(
            "nas.local",
            445,
            resolver=lambda host, port: [("fake",)],
            connector=lambda addr, timeout: SimpleNamespace(close=lambda: None),
        )
        assert result.ok is True
        assert result.stage == "ok"

    def test_dns_failure(self):
        def failing_resolver(host, port):
            raise OSError("Name or service not known")

        result = mount.check_reachable("bad.invalid", 445, resolver=failing_resolver)
        assert result.ok is False
        assert result.stage == "dns"

    def test_tcp_failure(self):
        result = mount.check_reachable(
            "nas.local",
            445,
            resolver=lambda host, port: [("fake",)],
            connector=lambda addr, timeout: (_ for _ in ()).throw(OSError("Connection refused")),
        )
        assert result.ok is False
        assert result.stage == "tcp"

    def test_blocked_injected_dns_is_abandoned_at_deadline(self):
        release = threading.Event()

        def blocked_resolver(_host, _port):
            release.wait(5.0)
            return []

        started = time.monotonic()
        try:
            result = mount.check_reachable(
                "nas.local", timeout=0.02, resolver=blocked_resolver
            )
        finally:
            release.set()

        assert time.monotonic() - started < 1.0
        assert result.ok is False
        assert result.stage == "dns"
        assert "timed out" in result.detail


class TestWaitUntilReachable:
    def test_returns_immediately_on_first_success(self):
        calls = []

        def check(host, port):
            calls.append(1)
            return mount.ReachabilityResult(True, "ok", "")

        result = mount.wait_until_reachable(
            "nas.local", check=check, sleep=lambda s: None, clock=_fake_clock([0.0])
        )
        assert result.ok is True
        assert len(calls) == 1

    def test_retries_until_success(self):
        outcomes = iter(
            [
                mount.ReachabilityResult(False, "tcp", "not yet"),
                mount.ReachabilityResult(False, "tcp", "not yet"),
                mount.ReachabilityResult(True, "ok", ""),
            ]
        )
        sleeps = []

        result = mount.wait_until_reachable(
            "nas.local",
            check=lambda host, port: next(outcomes),
            sleep=lambda s: sleeps.append(s),
            clock=_fake_clock([0.0, 1.0, 2.0, 3.0, 4.0, 5.0]),
            timeout_total=100.0,
        )
        assert result.ok is True
        assert len(sleeps) == 2

    def test_gives_up_after_timeout(self):
        result = mount.wait_until_reachable(
            "nas.local",
            check=lambda host, port: mount.ReachabilityResult(False, "tcp", "never"),
            sleep=lambda s: None,
            clock=_fake_clock([0.0, 5.0, 10.0, 999.0]),
            timeout_total=10.0,
        )
        assert result.ok is False

    def test_pre_set_cancellation_skips_probe_and_sleep(self):
        cancelled = threading.Event()
        cancelled.set()

        result = mount.wait_until_reachable(
            "nas.local",
            check=lambda *_args: (_ for _ in ()).throw(
                AssertionError("cancelled wait must not probe")
            ),
            sleep=lambda _seconds: (_ for _ in ()).throw(
                AssertionError("cancelled wait must not sleep")
            ),
            cancel_event=cancelled,
        )

        assert result.ok is False
        assert result.stage == "cancelled"


def _fake_clock(values: list[float]):
    it = iter(values)

    def clock():
        try:
            return next(it)
        except StopIteration:
            return values[-1]

    return clock


class _FakeCompletedProcess:
    def __init__(self, returncode: int, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class TestMountCifsSource:
    def test_already_mounted_is_noop(self, tmp_path):
        proc_mounts = tmp_path / "mounts"
        proc_mounts.write_text("//nas/ROMs /mnt/roms cifs ro 0 0\n")

        called = []
        outcome = mount.mount_cifs_source(
            "nas",
            "ROMs",
            "/mnt/roms",
            Path("/creds"),
            runner=lambda *a, **k: called.append(a) or _FakeCompletedProcess(0),
            proc_mounts_path=str(proc_mounts),
        )
        assert outcome.already_mounted is True
        assert called == []  # never even tried to run mount

    def test_already_mounted_with_wrong_mode_fails_clearly(self, tmp_path):
        proc_mounts = tmp_path / "mounts"
        proc_mounts.write_text("//nas/ROMs /mnt/saves cifs ro 0 0\n")

        with pytest.raises(MountError, match="wrong mode"):
            mount.mount_cifs_source(
                "nas",
                "ROMs",
                "/mnt/saves",
                Path("/creds"),
                read_only=False,
                proc_mounts_path=str(proc_mounts),
            )

    def test_already_mounted_rw_wrong_share_fails_clearly(self, tmp_path):
        proc_mounts = tmp_path / "mounts"
        proc_mounts.write_text("//nas/Other /mnt/remote cifs rw 0 0\n")

        with pytest.raises(MountError, match="wrong mode or SMB source"):
            mount.mount_cifs_source(
                "nas",
                "ROMCloud",
                "/mnt/remote",
                Path("/creds"),
                read_only=False,
                proc_mounts_path=str(proc_mounts),
            )

    def test_unreachable_raises_before_mounting(self, tmp_path):
        proc_mounts = tmp_path / "mounts"
        proc_mounts.write_text("")

        called = []
        with pytest.raises(ProviderNotReachableError):
            mount.mount_cifs_source(
                "nas",
                "ROMs",
                "/mnt/roms",
                Path("/creds"),
                runner=lambda *a, **k: called.append(a) or _FakeCompletedProcess(0),
                proc_mounts_path=str(proc_mounts),
                wait_timeout=0.01,
                wait_interval=0.01,
            )
        assert called == []

    def test_successful_mount_creates_dir_and_runs_mount(self, tmp_path, monkeypatch):
        proc_mounts = tmp_path / "mounts"
        proc_mounts.write_text("")
        mount_point = tmp_path / "roms"

        monkeypatch.setattr(mount, "wait_until_reachable", lambda *a, **k: mount.ReachabilityResult(True, "ok", ""))

        captured = {}

        def fake_runner(argv, **kwargs):
            captured["argv"] = argv
            return _FakeCompletedProcess(0)

        outcome = mount.mount_cifs_source(
            "nas",
            "ROMs",
            str(mount_point),
            Path("/creds"),
            runner=fake_runner,
            proc_mounts_path=str(proc_mounts),
        )
        assert outcome.mounted is True
        assert outcome.already_mounted is False
        assert mount_point.is_dir()
        assert captured["argv"][0] == "mount"

    def test_auth_failure_classified(self, tmp_path, monkeypatch):
        proc_mounts = tmp_path / "mounts"
        proc_mounts.write_text("")

        monkeypatch.setattr(mount, "wait_until_reachable", lambda *a, **k: mount.ReachabilityResult(True, "ok", ""))

        def fake_runner(argv, **kwargs):
            return _FakeCompletedProcess(32, stderr="mount error(13): Permission denied")

        with pytest.raises(ProviderAuthError):
            mount.mount_cifs_source(
                "nas",
                "ROMs",
                str(tmp_path / "roms"),
                Path("/creds"),
                runner=fake_runner,
                proc_mounts_path=str(proc_mounts),
            )

    def test_network_failure_classified(self, tmp_path, monkeypatch):
        proc_mounts = tmp_path / "mounts"
        proc_mounts.write_text("")

        monkeypatch.setattr(mount, "wait_until_reachable", lambda *a, **k: mount.ReachabilityResult(True, "ok", ""))

        def fake_runner(argv, **kwargs):
            return _FakeCompletedProcess(32, stderr="mount error(101): Network is unreachable")

        with pytest.raises(ProviderNotReachableError):
            mount.mount_cifs_source(
                "nas",
                "ROMs",
                str(tmp_path / "roms"),
                Path("/creds"),
                runner=fake_runner,
                proc_mounts_path=str(proc_mounts),
            )

    def test_generic_failure_classified_as_mount_error(self, tmp_path, monkeypatch):
        proc_mounts = tmp_path / "mounts"
        proc_mounts.write_text("")

        monkeypatch.setattr(mount, "wait_until_reachable", lambda *a, **k: mount.ReachabilityResult(True, "ok", ""))

        def fake_runner(argv, **kwargs):
            return _FakeCompletedProcess(32, stderr="mount error(22): Invalid argument")

        with pytest.raises(MountError):
            mount.mount_cifs_source(
                "nas",
                "ROMs",
                str(tmp_path / "roms"),
                Path("/creds"),
                runner=fake_runner,
                proc_mounts_path=str(proc_mounts),
            )

    def test_error_message_never_contains_password(self, tmp_path, monkeypatch):
        proc_mounts = tmp_path / "mounts"
        proc_mounts.write_text("")
        monkeypatch.setattr(mount, "wait_until_reachable", lambda *a, **k: mount.ReachabilityResult(True, "ok", ""))

        def fake_runner(argv, **kwargs):
            return _FakeCompletedProcess(32, stderr="mount error(13): Permission denied")

        with pytest.raises(ProviderAuthError) as excinfo:
            mount.mount_cifs_source(
                "nas",
                "ROMs",
                str(tmp_path / "roms"),
                Path("/creds"),
                runner=fake_runner,
                proc_mounts_path=str(proc_mounts),
            )
        assert "hunter2" not in str(excinfo.value)

    def test_mount_command_timeout_is_actionable(self, tmp_path, monkeypatch):
        proc_mounts = tmp_path / "mounts"
        proc_mounts.write_text("")
        monkeypatch.setattr(
            mount,
            "wait_until_reachable",
            lambda *a, **k: mount.ReachabilityResult(True, "ok", ""),
        )

        def timed_out(argv, **kwargs):
            raise subprocess.TimeoutExpired(argv, kwargs["timeout"])

        with pytest.raises(ProviderNotReachableError, match="mount command timed out"):
            mount.mount_cifs_source(
                "nas",
                "ROMs",
                str(tmp_path / "roms"),
                Path("/creds"),
                runner=timed_out,
                proc_mounts_path=str(proc_mounts),
                command_timeout=0.01,
            )


class TestUnmountCifsSource:
    def test_not_mounted_is_noop(self, tmp_path):
        proc_mounts = tmp_path / "mounts"
        proc_mounts.write_text("")

        called = []
        result = mount.unmount_cifs_source(
            "/mnt/roms",
            runner=lambda *a, **k: called.append(a) or _FakeCompletedProcess(0),
            proc_mounts_path=str(proc_mounts),
        )
        assert result is False
        assert called == []

    def test_mounted_unmounts_successfully(self, tmp_path):
        proc_mounts = tmp_path / "mounts"
        proc_mounts.write_text("//nas/ROMs /mnt/roms cifs ro 0 0\n")

        captured = {}

        def fake_runner(argv, **kwargs):
            captured["argv"] = argv
            return _FakeCompletedProcess(0)

        result = mount.unmount_cifs_source(
            "/mnt/roms", runner=fake_runner, proc_mounts_path=str(proc_mounts)
        )
        assert result is True
        assert captured["argv"] == ["umount", "/mnt/roms"]

    def test_unmount_failure_raises_mount_error(self, tmp_path):
        proc_mounts = tmp_path / "mounts"
        proc_mounts.write_text("//nas/ROMs /mnt/roms cifs ro 0 0\n")

        def fake_runner(argv, **kwargs):
            return _FakeCompletedProcess(1, stderr="target is busy")

        with pytest.raises(MountError):
            mount.unmount_cifs_source(
                "/mnt/roms", runner=fake_runner, proc_mounts_path=str(proc_mounts)
            )

    def test_shutdown_lazy_unmount_is_bounded(self, monkeypatch):
        monkeypatch.setattr(mount, "is_target_mounted", lambda *a, **k: True)
        captured = {}

        def timed_out(argv, **kwargs):
            captured["argv"] = argv
            raise subprocess.TimeoutExpired(argv, kwargs["timeout"])

        with pytest.raises(MountError, match="Timed out"):
            mount.unmount_cifs_source(
                "/mnt/roms", runner=timed_out, command_timeout=0.01, lazy=True
            )

        assert captured["argv"] == ["umount", "-l", "/mnt/roms"]
