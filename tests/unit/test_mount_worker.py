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
    saves_mount: str | None = None,
):
    return SimpleNamespace(
        source=SimpleNamespace(rom_root=rom_root),
        smb=smb,
        credentials_path=credentials_path or Path("/tmp/does-not-matter/credentials.toml"),
        data_path=str(Path(rom_root).parent / "data"),
        saves=(
            SimpleNamespace(remote_mount_path=saves_mount)
            if saves_mount is not None
            else None
        ),
    )


def _fake_smb(server="nas.local", share="ROMs", username="alice", port=445):
    return SimpleNamespace(server=server, share=share, username=username, port=port)


def _fake_clock(values: list[float]):
    it = iter(values)

    def clock():
        try:
            return next(it)
        except StopIteration:
            return values[-1]

    return clock


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
        mw._worker_cmdline_matches = lambda pid, *, proc_root: pid == os.getpid()
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
    def test_acquire_and_release(self, tmp_path, monkeypatch):
        monkeypatch.setattr(mw, "_worker_cmdline_matches", lambda pid, *, proc_root: pid == os.getpid())
        with mw._WorkerLock(tmp_path):
            assert mw.lock_path(tmp_path).exists()
            assert mw.lock_path(tmp_path).read_text() == str(os.getpid())
        assert not mw.lock_path(tmp_path).exists()

    def test_second_acquire_while_held_raises(self, tmp_path, monkeypatch):
        monkeypatch.setattr(mw, "_worker_cmdline_matches", lambda pid, *, proc_root: pid == os.getpid())
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
        monkeypatch.setattr(mw, "_worker_cmdline_matches", lambda pid, *, proc_root: pid == os.getpid())

        called = []
        monkeypatch.setattr(mw.mountlib, "is_target_mounted", lambda *a, **k: called.append(1) or False)

        config = _fake_config(smb=_fake_smb())
        code = mw.run_worker(tmp_path, config)

        assert code == 0
        assert called == []  # never even checked — exited immediately on the lock
        # The pre-existing lock (not ours) must be left alone.
        assert mw.lock_path(tmp_path).read_text() == str(os.getpid())


class TestRunWorker:
    def test_mounts_catalog_read_only_and_savesync_separately_read_write(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setattr(mw.mountlib, "is_target_mounted", lambda *a, **k: False)
        monkeypatch.setattr(mw, "load_smb_password", lambda *a, **k: "hunter2")
        monkeypatch.setattr(mw, "write_cifs_credentials_file", lambda *a, **k: None)
        monkeypatch.setattr(mw.mount_endpoint_cache, "resolve_endpoint", lambda *a, **k: None)
        attempts = []
        monkeypatch.setattr(
            mw.mountlib,
            "mount_cifs_source",
            lambda **kwargs: attempts.append(kwargs) or mw.mountlib.MountOutcome(
                mounted=True, already_mounted=False, detail="mounted"
            ),
        )

        config = _fake_config(smb=_fake_smb(), saves_mount="/mnt/saves-rw")
        code = mw.run_worker(tmp_path, config)

        assert code == 0
        assert [item["mount_point"] for item in attempts] == ["/mnt/roms", "/mnt/saves-rw"]
        assert attempts[0].get("read_only", True) is True
        assert attempts[1]["read_only"] is False

    def test_already_mounted_records_success_and_skips_mount(self, tmp_path, monkeypatch):
        monkeypatch.setattr(mw.mountlib, "is_target_mounted", lambda *a, **k: True)
        monkeypatch.setattr(mw.mountlib, "is_target_mounted_read_only", lambda *a, **k: True)

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
        sleeps = []
        attempts = []

        monkeypatch.setattr(
            mw.mountlib,
            "mount_cifs_source",
            lambda **k: attempts.append(k) or mw.mountlib.MountOutcome(
                mounted=True, already_mounted=False, detail="mounted"
            ),
        )

        config = _fake_config(smb=_fake_smb())
        code = mw.run_worker(
            tmp_path,
            config,
            sleep=lambda seconds: sleeps.append(seconds),
            clock=_fake_clock([0.0, 0.5]),
        )

        assert code == 0
        assert attempts == [
            {
                "server": "nas.local",
                "share": "ROMs",
                "mount_point": "/mnt/roms",
                "credentials_path": mw.cifs_credentials_path(config.credentials_path),
                "port": 445,
                "wait_timeout": mw.DEFAULT_ATTEMPT_TIMEOUT,
                "wait_interval": mw.DEFAULT_ATTEMPT_INTERVAL,
            }
        ]
        # The single mount attempt must never be handed the full overall
        # retry budget — that was the root cause of the boot-time bug.
        assert attempts[0]["wait_timeout"] < mw.DEFAULT_RETRY_TIMEOUT
        assert sleeps == []
        status = mw.read_worker_status(tmp_path)
        assert status.state == "success"
        assert not mw.lock_path(tmp_path).exists()

    def test_default_attempt_timeout_is_short_not_full_retry_budget(self):
        """Regression for the boot-time bug: a single attempt's timeout must
        stay short (~5-10s) and strictly less than the overall retry budget."""
        assert 5.0 <= mw.DEFAULT_ATTEMPT_TIMEOUT <= 10.0
        assert mw.DEFAULT_ATTEMPT_TIMEOUT < mw.DEFAULT_RETRY_TIMEOUT

    def test_retry_budget_exhaustion_records_failed_status(self, tmp_path, monkeypatch):
        monkeypatch.setattr(mw.mountlib, "is_target_mounted", lambda *a, **k: False)
        monkeypatch.setattr(mw, "load_smb_password", lambda *a, **k: "hunter2")
        monkeypatch.setattr(mw, "write_cifs_credentials_file", lambda *a, **k: None)

        attempts = []

        def fake_mount_cifs_source(**kwargs):
            attempts.append(kwargs)
            raise ProviderNotReachableError("SMB source unreachable")

        monkeypatch.setattr(mw.mountlib, "mount_cifs_source", fake_mount_cifs_source)

        sleeps = []
        config = _fake_config(smb=_fake_smb())
        code = mw.run_worker(
            tmp_path,
            config,
            retry_timeout=1.0,
            retry_initial_delay=0.5,
            retry_max_delay=1.0,
            sleep=lambda seconds: sleeps.append(seconds),
            clock=_fake_clock([0.0, 0.3, 0.5, 0.6, 1.1]),
        )

        assert code == 0
        assert len(attempts) == 2
        assert sleeps == [0.5]
        status = mw.read_worker_status(tmp_path)
        assert status.state == "failed"
        assert status.detail.startswith("Timed out after 2 attempt(s)")
        assert "SMB source" in status.detail

    def test_retries_provider_not_reachable_until_success(self, tmp_path, monkeypatch):
        monkeypatch.setattr(mw.mountlib, "is_target_mounted", lambda *a, **k: False)
        monkeypatch.setattr(mw, "load_smb_password", lambda *a, **k: "hunter2")
        monkeypatch.setattr(mw, "write_cifs_credentials_file", lambda *a, **k: None)

        attempts = []

        def fake_mount_cifs_source(**kwargs):
            attempts.append(kwargs)
            if len(attempts) < 3:
                raise ProviderNotReachableError("SMB source unreachable")
            return mw.mountlib.MountOutcome(mounted=True, already_mounted=False, detail="mounted")

        monkeypatch.setattr(mw.mountlib, "mount_cifs_source", fake_mount_cifs_source)

        sleeps = []
        config = _fake_config(smb=_fake_smb())
        code = mw.run_worker(
            tmp_path,
            config,
            retry_timeout=30.0,
            retry_initial_delay=1.0,
            retry_max_delay=2.0,
            sleep=lambda seconds: sleeps.append(seconds),
            clock=_fake_clock([0.0, 0.5, 1.0, 2.0, 3.0, 4.0]),
        )

        assert code == 0
        assert len(attempts) == 3
        assert sleeps == [1.0, 2.0]
        assert mw.read_worker_status(tmp_path).state == "success"

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
        start = time.monotonic()
        # A small, explicit retry budget keeps this deterministic and fast —
        # the real-time behaviour (never raising, bounded exhaustion) is what
        # is under test here, not the production 300s window.
        code = mw.run_worker(
            tmp_path,
            config,
            retry_timeout=0.3,
            retry_initial_delay=0.05,
            retry_max_delay=0.1,
        )
        elapsed = time.monotonic() - start

        assert code == 0
        assert elapsed < 5.0  # bounded — must never retry indefinitely
        assert mw.read_worker_status(tmp_path).state == "failed"

    def test_multiple_real_attempts_occur_within_bounded_retry_window(self, tmp_path, monkeypatch):
        """With real wall-clock timing (no fake clock), the retry loop must
        actually retry more than once within a bounded overall window, and no
        single attempt may be handed the full overall retry budget."""
        monkeypatch.setattr(mw.mountlib, "is_target_mounted", lambda *a, **k: False)
        monkeypatch.setattr(mw, "load_smb_password", lambda *a, **k: "hunter2")
        monkeypatch.setattr(mw, "write_cifs_credentials_file", lambda *a, **k: None)

        attempts = []

        def fake_mount_cifs_source(**kwargs):
            attempts.append(kwargs)
            raise ProviderNotReachableError("SMB source unreachable")

        monkeypatch.setattr(mw.mountlib, "mount_cifs_source", fake_mount_cifs_source)

        config = _fake_config(smb=_fake_smb())
        retry_timeout = 0.3
        start = time.monotonic()
        code = mw.run_worker(
            tmp_path,
            config,
            attempt_timeout=0.05,
            attempt_interval=0.01,
            retry_timeout=retry_timeout,
            retry_initial_delay=0.02,
            retry_max_delay=0.05,
        )
        elapsed = time.monotonic() - start

        assert code == 0
        assert elapsed < 5.0
        assert len(attempts) >= 2, "the retry loop must actually retry within the overall budget"
        assert all(a["wait_timeout"] <= retry_timeout for a in attempts)
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


class TestCachedEndpointFallback:
    """Boot-time UX: a cached resolved endpoint is tried first (fast path),
    falling back to the existing bounded hostname retry loop untouched."""

    def test_uses_cached_endpoint_first_and_succeeds_without_hostname_attempt(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setattr(mw.mountlib, "is_target_mounted", lambda *a, **k: False)
        monkeypatch.setattr(mw, "load_smb_password", lambda *a, **k: "hunter2")
        monkeypatch.setattr(mw, "write_cifs_credentials_file", lambda *a, **k: None)
        mw.mount_endpoint_cache.write_endpoint_cache(tmp_path, "omnivault", "192.0.2.10")

        attempts = []

        def fake_mount_cifs_source(**kwargs):
            attempts.append(kwargs["server"])
            if kwargs["server"] == "omnivault":
                raise AssertionError("must not attempt the hostname when the cache succeeds")
            return mw.mountlib.MountOutcome(mounted=True, already_mounted=False, detail="mounted")

        monkeypatch.setattr(mw.mountlib, "mount_cifs_source", fake_mount_cifs_source)

        config = _fake_config(smb=_fake_smb(server="omnivault"))
        code = mw.run_worker(tmp_path, config)

        assert code == 0
        assert attempts == ["192.0.2.10"]
        assert mw.read_worker_status(tmp_path).state == "success"

    def test_falls_back_to_hostname_when_cached_endpoint_fails_without_losing_budget(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setattr(mw.mountlib, "is_target_mounted", lambda *a, **k: False)
        monkeypatch.setattr(mw, "load_smb_password", lambda *a, **k: "hunter2")
        monkeypatch.setattr(mw, "write_cifs_credentials_file", lambda *a, **k: None)
        mw.mount_endpoint_cache.write_endpoint_cache(tmp_path, "omnivault", "192.0.2.99")

        attempts = []

        def fake_mount_cifs_source(**kwargs):
            attempts.append(kwargs["server"])
            if kwargs["server"] == "192.0.2.99":
                raise ProviderNotReachableError("stale endpoint unreachable")
            return mw.mountlib.MountOutcome(mounted=True, already_mounted=False, detail="mounted")

        monkeypatch.setattr(mw.mountlib, "mount_cifs_source", fake_mount_cifs_source)
        monkeypatch.setattr(mw.mount_endpoint_cache, "resolve_endpoint", lambda *a, **k: "192.0.2.10")

        config = _fake_config(smb=_fake_smb(server="omnivault"))
        code = mw.run_worker(
            tmp_path,
            config,
            retry_timeout=30.0,  # the hostname attempt must still get the FULL budget
        )

        assert code == 0
        assert attempts == ["192.0.2.99", "omnivault"]
        assert mw.read_worker_status(tmp_path).state == "success"

    def test_ignores_cache_entry_for_a_different_configured_server(self, tmp_path, monkeypatch):
        monkeypatch.setattr(mw.mountlib, "is_target_mounted", lambda *a, **k: False)
        monkeypatch.setattr(mw, "load_smb_password", lambda *a, **k: "hunter2")
        monkeypatch.setattr(mw, "write_cifs_credentials_file", lambda *a, **k: None)
        mw.mount_endpoint_cache.write_endpoint_cache(tmp_path, "old-nas", "192.0.2.50")

        attempts = []

        def fake_mount_cifs_source(**kwargs):
            attempts.append(kwargs["server"])
            return mw.mountlib.MountOutcome(mounted=True, already_mounted=False, detail="mounted")

        monkeypatch.setattr(mw.mountlib, "mount_cifs_source", fake_mount_cifs_source)
        monkeypatch.setattr(mw.mount_endpoint_cache, "resolve_endpoint", lambda *a, **k: None)

        config = _fake_config(smb=_fake_smb(server="omnivault"))
        code = mw.run_worker(tmp_path, config)

        assert code == 0
        assert attempts == ["omnivault"]  # the foreign cached IP was never tried

    def test_successful_hostname_mount_refreshes_cache(self, tmp_path, monkeypatch):
        monkeypatch.setattr(mw.mountlib, "is_target_mounted", lambda *a, **k: False)
        monkeypatch.setattr(mw, "load_smb_password", lambda *a, **k: "hunter2")
        monkeypatch.setattr(mw, "write_cifs_credentials_file", lambda *a, **k: None)
        monkeypatch.setattr(
            mw.mountlib, "mount_cifs_source",
            lambda **k: mw.mountlib.MountOutcome(mounted=True, already_mounted=False, detail="mounted"),
        )
        monkeypatch.setattr(mw.mount_endpoint_cache, "resolve_endpoint", lambda *a, **k: "192.0.2.10")
        monkeypatch.setattr(
            mw.mountlib, "check_reachable",
            lambda *a, **k: mw.mountlib.ReachabilityResult(True, "ok", ""),
        )

        config = _fake_config(smb=_fake_smb(server="omnivault"))
        code = mw.run_worker(tmp_path, config)

        assert code == 0
        entry = mw.mount_endpoint_cache.read_endpoint_cache(tmp_path)
        assert entry is not None
        assert entry.server == "omnivault"
        assert entry.endpoint == "192.0.2.10"

    def test_unreachable_resolved_candidate_is_never_cached(self, tmp_path, monkeypatch):
        monkeypatch.setattr(mw.mountlib, "is_target_mounted", lambda *a, **k: False)
        monkeypatch.setattr(mw, "load_smb_password", lambda *a, **k: "hunter2")
        monkeypatch.setattr(mw, "write_cifs_credentials_file", lambda *a, **k: None)
        monkeypatch.setattr(
            mw.mountlib, "mount_cifs_source",
            lambda **k: mw.mountlib.MountOutcome(mounted=True, already_mounted=False, detail="mounted"),
        )
        # A fresh resolution can legitimately return a *different* address
        # than the one the successful mount itself used (round-robin DNS,
        # rotating resolvers) — an unverified candidate must never be cached.
        monkeypatch.setattr(mw.mount_endpoint_cache, "resolve_endpoint", lambda *a, **k: "192.0.2.99")
        monkeypatch.setattr(
            mw.mountlib, "check_reachable",
            lambda *a, **k: mw.mountlib.ReachabilityResult(False, "tcp", "unreachable"),
        )

        config = _fake_config(smb=_fake_smb(server="omnivault"))
        code = mw.run_worker(tmp_path, config)

        assert code == 0
        assert mw.mount_endpoint_cache.read_endpoint_cache(tmp_path) is None

    def test_direct_ip_config_never_triggers_a_redundant_cached_probe(self, tmp_path, monkeypatch):
        monkeypatch.setattr(mw.mountlib, "is_target_mounted", lambda *a, **k: False)
        monkeypatch.setattr(mw, "load_smb_password", lambda *a, **k: "hunter2")
        monkeypatch.setattr(mw, "write_cifs_credentials_file", lambda *a, **k: None)
        # A previous run already cached the same IP the user configured directly.
        mw.mount_endpoint_cache.write_endpoint_cache(tmp_path, "192.0.2.10", "192.0.2.10")

        attempts = []

        def fake_mount_cifs_source(**kwargs):
            attempts.append(kwargs["server"])
            return mw.mountlib.MountOutcome(mounted=True, already_mounted=False, detail="mounted")

        monkeypatch.setattr(mw.mountlib, "mount_cifs_source", fake_mount_cifs_source)
        monkeypatch.setattr(mw.mount_endpoint_cache, "resolve_endpoint", lambda *a, **k: "192.0.2.10")

        config = _fake_config(smb=_fake_smb(server="192.0.2.10"))
        code = mw.run_worker(tmp_path, config)

        assert code == 0
        assert attempts == ["192.0.2.10"]  # exactly one attempt, no wasted duplicate

    def test_missing_cache_does_not_change_existing_hostname_behavior(self, tmp_path, monkeypatch):
        monkeypatch.setattr(mw.mountlib, "is_target_mounted", lambda *a, **k: False)
        monkeypatch.setattr(mw, "load_smb_password", lambda *a, **k: "hunter2")
        monkeypatch.setattr(mw, "write_cifs_credentials_file", lambda *a, **k: None)

        attempts = []

        def fake_mount_cifs_source(**kwargs):
            attempts.append(kwargs["server"])
            return mw.mountlib.MountOutcome(mounted=True, already_mounted=False, detail="mounted")

        monkeypatch.setattr(mw.mountlib, "mount_cifs_source", fake_mount_cifs_source)
        monkeypatch.setattr(mw.mount_endpoint_cache, "resolve_endpoint", lambda *a, **k: None)

        config = _fake_config(smb=_fake_smb(server="omnivault"))
        code = mw.run_worker(tmp_path, config)

        assert code == 0
        assert attempts == ["omnivault"]


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

    def test_terminates_live_worker_process(self, tmp_path, monkeypatch):
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        try:
            mw.lock_path(tmp_path).parent.mkdir(parents=True)
            mw.lock_path(tmp_path).write_text(str(child.pid))
            monkeypatch.setattr(mw, "_worker_cmdline_matches", lambda pid, *, proc_root: pid == child.pid)

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

    def test_removes_cached_endpoint(self, tmp_path):
        mw.mount_endpoint_cache.write_endpoint_cache(tmp_path, "omnivault", "192.0.2.10")

        mw.cleanup_runtime_state(tmp_path)

        assert mw.mount_endpoint_cache.read_endpoint_cache(tmp_path) is None

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
        monkeypatch.setattr(mw.mountlib, "is_target_mounted_read_only", lambda *a, **k: True)
        config = _fake_config(smb=_fake_smb())
        diag = mw.get_diagnostics(tmp_path, config)
        assert diag.mounted is True
        assert diag.label == "mounted"

    def test_worker_running_shows_waiting(self, tmp_path, monkeypatch):
        monkeypatch.setattr(mw.mountlib, "is_target_mounted", lambda *a, **k: False)
        mw.lock_path(tmp_path).parent.mkdir(parents=True)
        mw.lock_path(tmp_path).write_text(str(os.getpid()))
        monkeypatch.setattr(mw, "_worker_cmdline_matches", lambda pid, *, proc_root: pid == os.getpid())

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

    def test_cached_endpoint_shown_only_when_it_matches_configured_server(self, tmp_path, monkeypatch):
        monkeypatch.setattr(mw.mountlib, "is_target_mounted", lambda *a, **k: False)
        mw.mount_endpoint_cache.write_endpoint_cache(tmp_path, "omnivault", "192.0.2.10")

        config = _fake_config(smb=_fake_smb(server="omnivault"))
        diag = mw.get_diagnostics(tmp_path, config)
        assert diag.cached_endpoint == "192.0.2.10"

        other_config = _fake_config(smb=_fake_smb(server="different-host"))
        diag_other = mw.get_diagnostics(tmp_path, other_config)
        assert diag_other.cached_endpoint is None
