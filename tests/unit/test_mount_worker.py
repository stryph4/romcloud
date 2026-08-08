"""Unit tests for romcloud.infrastructure.mount_worker.

Covers the boot-safety hardening: single-instance lock (with stale-lock
recovery), the worker loop's failure isolation (never raises, always
records a clear status), stop/cleanup, spawning a detached background
process, and the combined diagnostics used by `mount status`/`healthcheck`.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from romcloud.core.exceptions import MountError, ProviderAuthError, ProviderNotReachableError
from romcloud.infrastructure import mount_worker as mw


def _fake_config(
    *,
    rom_root: str = "/mnt/roms",
    smb=None,
    credentials_path: Path | None = None,
):
    return SimpleNamespace(
        source=SimpleNamespace(rom_root=rom_root),
        smb=smb,
        credentials_path=credentials_path or Path("/tmp/does-not-matter/credentials.toml"),
        data_path=str(Path(rom_root).parent / "data"),
    )


def _fake_smb(server="nas.local", share="ROMs", username="alice", port=445):
    return SimpleNamespace(server=server, share=share, username=username, port=port)


class TestRomcloudHomeFromConfig:
    def test_derives_parent_of_data_path(self):
        config = SimpleNamespace(data_path="/userdata/system/romcloud/data")
        assert mw.romcloud_home_from_config(config) == Path("/userdata/system/romcloud")


class TestWorkerStatusPersistence:
    def test_round_trip(self, tmp_path):
        mw._write_worker_status(tmp_path, "success", "mounted ok")
        status = mw.read_worker_status(tmp_path)
        assert status is not None
        assert status.state == "success"
        assert status.detail == "mounted ok"

    def test_missing_file_returns_none(self, tmp_path):
        assert mw.read_worker_status(tmp_path) is None

    def test_corrupt_file_returns_none(self, tmp_path):
        mw.status_path(tmp_path).parent.mkdir(parents=True)
        mw.status_path(tmp_path).write_text("not json{{{")
        assert mw.read_worker_status(tmp_path) is None


class TestIsWorkerRunning:
    def test_no_lock_file_returns_none(self, tmp_path):
        assert mw.is_worker_running(tmp_path) is None

    def test_live_pid_is_detected(self, tmp_path):
        mw.lock_path(tmp_path).parent.mkdir(parents=True)
        mw.lock_path(tmp_path).write_text(str(os.getpid()))
        assert mw.is_worker_running(tmp_path) == os.getpid()

    def test_stale_lock_is_recovered_and_removed(self, tmp_path):
        # Spawn and wait for a subprocess so its PID is guaranteed dead.
        proc = subprocess.Popen([sys.executable, "-c", "pass"])
        dead_pid = proc.pid
        proc.wait()

        mw.lock_path(tmp_path).parent.mkdir(parents=True)
        mw.lock_path(tmp_path).write_text(str(dead_pid))

        assert mw.is_worker_running(tmp_path) is None
        assert not mw.lock_path(tmp_path).exists()

    def test_corrupt_lock_file_is_removed(self, tmp_path):
        mw.lock_path(tmp_path).parent.mkdir(parents=True)
        mw.lock_path(tmp_path).write_text("not-a-pid")
        assert mw.is_worker_running(tmp_path) is None
        assert not mw.lock_path(tmp_path).exists()


class TestWorkerLock:
    def test_acquire_and_release(self, tmp_path):
        with mw._WorkerLock(tmp_path):
            assert mw.lock_path(tmp_path).exists()
            assert mw.lock_path(tmp_path).read_text() == str(os.getpid())
        assert not mw.lock_path(tmp_path).exists()

    def test_second_acquire_while_held_raises(self, tmp_path):
        with mw._WorkerLock(tmp_path):
            with pytest.raises(mw.WorkerAlreadyRunning):
                with mw._WorkerLock(tmp_path):
                    pass

    def test_lock_released_even_if_body_raises(self, tmp_path):
        with pytest.raises(ValueError):
            with mw._WorkerLock(tmp_path):
                raise ValueError("boom")
        assert not mw.lock_path(tmp_path).exists()


class TestOnlyOneWorkerRunsAtOnce:
    def test_run_worker_skips_when_lock_already_held_by_live_process(self, tmp_path, monkeypatch):
        mw.lock_path(tmp_path).parent.mkdir(parents=True)
        mw.lock_path(tmp_path).write_text(str(os.getpid()))  # "our" pid — definitely alive

        called = []
        monkeypatch.setattr(mw.mountlib, "is_target_mounted", lambda *a, **k: called.append(1) or False)

        config = _fake_config(smb=_fake_smb())
        code = mw.run_worker(tmp_path, config)

        assert code == 0
        assert called == []  # never even checked — exited immediately on the lock
        # The pre-existing lock (not ours) must be left alone.
        assert mw.lock_path(tmp_path).read_text() == str(os.getpid())


class TestRunWorker:
    def test_already_mounted_records_success_and_skips_mount(self, tmp_path, monkeypatch):
        monkeypatch.setattr(mw.mountlib, "is_target_mounted", lambda *a, **k: True)

        def _boom(*a, **k):
            raise AssertionError("must not attempt to mount when already mounted")

        monkeypatch.setattr(mw.mountlib, "mount_cifs_source", _boom)

        config = _fake_config(smb=_fake_smb())
        code = mw.run_worker(tmp_path, config)

        assert code == 0
        status = mw.read_worker_status(tmp_path)
        assert status.state == "success"
        assert not mw.lock_path(tmp_path).exists()

    def test_no_smb_section_records_failure_cleanly(self, tmp_path, monkeypatch):
        monkeypatch.setattr(mw.mountlib, "is_target_mounted", lambda *a, **k: False)

        config = _fake_config(smb=None)
        code = mw.run_worker(tmp_path, config)

        assert code == 0
        status = mw.read_worker_status(tmp_path)
        assert status.state == "failed"
        assert "smb" in status.detail.lower()

    def test_no_password_records_failure_cleanly(self, tmp_path, monkeypatch):
        monkeypatch.setattr(mw.mountlib, "is_target_mounted", lambda *a, **k: False)
        monkeypatch.setattr(mw, "load_smb_password", lambda *a, **k: None)

        config = _fake_config(smb=_fake_smb())
        code = mw.run_worker(tmp_path, config)

        assert code == 0
        status = mw.read_worker_status(tmp_path)
        assert status.state == "failed"
        assert "password" in status.detail.lower()

    def test_successful_mount_records_success(self, tmp_path, monkeypatch):
        monkeypatch.setattr(mw.mountlib, "is_target_mounted", lambda *a, **k: False)
        monkeypatch.setattr(mw, "load_smb_password", lambda *a, **k: "hunter2")
        monkeypatch.setattr(mw, "write_cifs_credentials_file", lambda *a, **k: None)
        monkeypatch.setattr(
            mw.mountlib,
            "mount_cifs_source",
            lambda **k: mw.mountlib.MountOutcome(mounted=True, already_mounted=False, detail="mounted"),
        )

        config = _fake_config(smb=_fake_smb())
        code = mw.run_worker(tmp_path, config)

        assert code == 0
        status = mw.read_worker_status(tmp_path)
        assert status.state == "success"
        assert not mw.lock_path(tmp_path).exists()

    def test_auth_failure_never_raises_and_is_logged_without_password(self, tmp_path, monkeypatch):
        monkeypatch.setattr(mw.mountlib, "is_target_mounted", lambda *a, **k: False)
        monkeypatch.setattr(mw, "load_smb_password", lambda *a, **k: "hunter2")
        monkeypatch.setattr(mw, "write_cifs_credentials_file", lambda *a, **k: None)

        def _raise_auth(**k):
            raise ProviderAuthError("SMB authentication failed: mount error(13): Permission denied")

        monkeypatch.setattr(mw.mountlib, "mount_cifs_source", _raise_auth)

        config = _fake_config(smb=_fake_smb())
        code = mw.run_worker(tmp_path, config)  # must not raise

        assert code == 0
        status = mw.read_worker_status(tmp_path)
        assert status.state == "failed"
        assert "hunter2" not in status.detail
        assert not mw.lock_path(tmp_path).exists()

    def test_network_failure_never_raises(self, tmp_path, monkeypatch):
        monkeypatch.setattr(mw.mountlib, "is_target_mounted", lambda *a, **k: False)
        monkeypatch.setattr(mw, "load_smb_password", lambda *a, **k: "hunter2")
        monkeypatch.setattr(mw, "write_cifs_credentials_file", lambda *a, **k: None)

        def _raise_net(**k):
            raise ProviderNotReachableError("SMB source unreachable")

        monkeypatch.setattr(mw.mountlib, "mount_cifs_source", _raise_net)

        config = _fake_config(smb=_fake_smb())
        code = mw.run_worker(tmp_path, config)

        assert code == 0
        assert mw.read_worker_status(tmp_path).state == "failed"

    def test_unexpected_exception_never_crashes_worker(self, tmp_path, monkeypatch):
        monkeypatch.setattr(mw.mountlib, "is_target_mounted", lambda *a, **k: False)
        monkeypatch.setattr(mw, "load_smb_password", lambda *a, **k: "hunter2")
        monkeypatch.setattr(mw, "write_cifs_credentials_file", lambda *a, **k: None)

        def _raise_weird(**k):
            raise RuntimeError("totally unexpected")

        monkeypatch.setattr(mw.mountlib, "mount_cifs_source", _raise_weird)

        config = _fake_config(smb=_fake_smb())
        code = mw.run_worker(tmp_path, config)  # must not propagate

        assert code == 0
        status = mw.read_worker_status(tmp_path)
        assert status.state == "failed"
        assert not mw.lock_path(tmp_path).exists()


class TestStopWorker:
    def test_no_lock_returns_false(self, tmp_path):
        assert mw.stop_worker(tmp_path) is False

    def test_stale_lock_returns_false_and_is_cleaned_up(self, tmp_path):
        proc = subprocess.Popen([sys.executable, "-c", "pass"])
        proc.wait()
        mw.lock_path(tmp_path).parent.mkdir(parents=True)
        mw.lock_path(tmp_path).write_text(str(proc.pid))

        assert mw.stop_worker(tmp_path) is False
        assert not mw.lock_path(tmp_path).exists()

    def test_terminates_live_worker_process(self, tmp_path):
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        try:
            mw.lock_path(tmp_path).parent.mkdir(parents=True)
            mw.lock_path(tmp_path).write_text(str(child.pid))

            stopped = mw.stop_worker(tmp_path, grace_period=5.0, poll_interval=0.05)

            assert stopped is True
            assert not mw.lock_path(tmp_path).exists()
            child.wait(timeout=5)
            assert child.returncode is not None
        finally:
            if child.poll() is None:
                child.kill()
                child.wait()


class TestCleanupRuntimeState:
    def test_removes_lock_and_status_but_not_log(self, tmp_path):
        mw.lock_path(tmp_path).parent.mkdir(parents=True)
        mw.lock_path(tmp_path).write_text("123")
        mw._write_worker_status(tmp_path, "success", "ok")
        mw.worker_log_path(tmp_path).parent.mkdir(parents=True)
        mw.worker_log_path(tmp_path).write_text("log contents")

        mw.cleanup_runtime_state(tmp_path)

        assert not mw.lock_path(tmp_path).exists()
        assert not mw.status_path(tmp_path).exists()
        assert mw.worker_log_path(tmp_path).exists()
        assert mw.worker_log_path(tmp_path).read_text() == "log contents"

    def test_noop_when_nothing_exists(self, tmp_path):
        mw.cleanup_runtime_state(tmp_path)  # must not raise


class TestSpawnWorker:
    def test_spawns_with_correct_argv_and_detached_options(self, tmp_path):
        captured = {}

        def fake_popen(argv, **kwargs):
            captured["argv"] = argv
            captured["kwargs"] = kwargs
            return SimpleNamespace(pid=4242)

        pid = mw.spawn_worker(tmp_path, python_executable="/venv/bin/python", popen=fake_popen)

        assert pid == 4242
        assert captured["argv"] == ["/venv/bin/python", "-m", "romcloud.cli.main", "mount", "worker"]
        assert captured["kwargs"]["start_new_session"] is True
        assert captured["kwargs"]["stdin"] == subprocess.DEVNULL

    def test_creates_log_file_and_run_dir(self, tmp_path):
        mw.spawn_worker(tmp_path, python_executable=sys.executable, popen=lambda *a, **k: SimpleNamespace(pid=1))
        assert mw.run_dir(tmp_path).is_dir()
        assert mw.worker_log_path(tmp_path).parent.is_dir()

    def test_real_spawn_returns_immediately_without_waiting(self, tmp_path):
        """The key boot-safety property: spawning must never block on the
        child, however long the child runs."""
        start = time.monotonic()
        pid = mw.spawn_worker(
            tmp_path,
            python_executable=sys.executable,
            popen=lambda argv, **kwargs: subprocess.Popen(
                [sys.executable, "-c", "import time; time.sleep(30)"], **{
                    k: v for k, v in kwargs.items() if k in ("stdin", "stdout", "stderr", "start_new_session")
                }
            ),
        )
        elapsed = time.monotonic() - start
        try:
            assert elapsed < 5.0
        finally:
            try:
                os.kill(pid, 9)
            except ProcessLookupError:
                pass


class TestGetDiagnostics:
    def test_not_configured(self, tmp_path):
        config = _fake_config(smb=None)
        diag = mw.get_diagnostics(tmp_path, config)
        assert diag.configured is False
        assert diag.label == "not configured"

    def test_mounted(self, tmp_path, monkeypatch):
        monkeypatch.setattr(mw.mountlib, "is_target_mounted", lambda *a, **k: True)
        config = _fake_config(smb=_fake_smb())
        diag = mw.get_diagnostics(tmp_path, config)
        assert diag.mounted is True
        assert diag.label == "mounted"

    def test_worker_running_shows_waiting(self, tmp_path, monkeypatch):
        monkeypatch.setattr(mw.mountlib, "is_target_mounted", lambda *a, **k: False)
        mw.lock_path(tmp_path).parent.mkdir(parents=True)
        mw.lock_path(tmp_path).write_text(str(os.getpid()))

        config = _fake_config(smb=_fake_smb())
        diag = mw.get_diagnostics(tmp_path, config)

        assert diag.worker_pid == os.getpid()
        assert "waiting" in diag.label

    def test_last_failure_shown_when_no_worker_and_not_mounted(self, tmp_path, monkeypatch):
        monkeypatch.setattr(mw.mountlib, "is_target_mounted", lambda *a, **k: False)
        mw._write_worker_status(tmp_path, "failed", "SMB authentication failed")

        config = _fake_config(smb=_fake_smb())
        diag = mw.get_diagnostics(tmp_path, config)

        assert diag.worker_pid is None
        assert "last attempt failed" in diag.label
        assert diag.last_detail == "SMB authentication failed"

    def test_default_not_mounted_label(self, tmp_path, monkeypatch):
        monkeypatch.setattr(mw.mountlib, "is_target_mounted", lambda *a, **k: False)
        config = _fake_config(smb=_fake_smb())
        diag = mw.get_diagnostics(tmp_path, config)
        assert diag.label == "not mounted"
