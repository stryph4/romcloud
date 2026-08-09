"""Unit tests for romcloud.infrastructure.update — the self-updater.

All network and subprocess I/O is faked (injected opener/runner) so these
tests run fully offline and never touch a real venv or GitHub. See
tests/unit/test_update_cmd.py for the CLI wiring tests.
"""

from __future__ import annotations

import io
import json
import subprocess
import zipfile
from pathlib import Path

import pytest

from romcloud.core.exceptions import (
    UpdateArchiveError,
    UpdateDownloadError,
    UpdateInstallError,
)
from romcloud.infrastructure import update as upd


# ── test helpers ──────────────────────────────────────────────────────────────


class _FakeHTTPResponse:
    def __init__(self, data: bytes) -> None:
        self._buf = io.BytesIO(data)

    def read(self, n: int = -1) -> bytes:
        return self._buf.read(n)

    def __enter__(self) -> "_FakeHTTPResponse":
        return self

    def __exit__(self, *exc) -> bool:
        return False


def _make_opener(payloads: dict, calls: list | None = None):
    def opener(request, timeout=None):
        url = request.full_url
        if calls is not None:
            calls.append(url)
        if url not in payloads:
            raise AssertionError(f"Unexpected URL requested: {url}")
        payload = payloads[url]
        if isinstance(payload, BaseException):
            raise payload
        return _FakeHTTPResponse(payload)

    return opener


def _commit_json(sha: str = "abc123def456" + "0" * 28, message: str = "Fix stuff") -> bytes:
    return json.dumps(
        {
            "sha": sha,
            "commit": {
                "committer": {"date": "2026-08-08T00:00:00Z"},
                "message": message,
            },
        }
    ).encode()


def _make_archive_bytes(
    sha: str = "abc123def456" + "0" * 28,
    version: str = "9.9.9",
    extra_files: dict | None = None,
    executable_files: set | None = None,
    top_dir: str | None = None,
) -> bytes:
    top = top_dir if top_dir is not None else f"romcloud-{sha}"
    files = {
        f"{top}/pyproject.toml": f'[project]\nname = "romcloud"\nversion = "{version}"\n'.encode(),
        f"{top}/src/romcloud/__init__.py": f'__version__ = "{version}"\n'.encode(),
    }
    if extra_files:
        for name, content in extra_files.items():
            files[f"{top}/{name}"] = content

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, content in files.items():
            info = zipfile.ZipInfo(name)
            mode = 0o755 if executable_files and name in executable_files else 0o644
            info.external_attr = (mode & 0o777) << 16
            zf.writestr(info, content)
    return buf.getvalue()


def _fake_runner_success(argv, **kwargs):
    return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")


def _fake_runner_failure(stderr: str = "pip failed"):
    def runner(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 1, stdout="", stderr=stderr)

    return runner


_SHA = "abc123def456" + "0" * 28


def _full_payloads(archive_bytes: bytes | None = None, sha: str = _SHA) -> dict:
    payloads = {upd.commit_api_url(upd.DEFAULT_REPO, upd.DEFAULT_BRANCH): _commit_json(sha=sha)}
    if archive_bytes is not None:
        payloads[upd.archive_download_url(upd.DEFAULT_REPO, sha)] = archive_bytes
    return payloads


# ── get_latest_commit ─────────────────────────────────────────────────────────


class TestGetLatestCommit:
    def test_parses_typical_response(self):
        opener = _make_opener({upd.commit_api_url("o/r", "main"): _commit_json(sha="deadbeef" * 5)})
        info = upd.get_latest_commit("o/r", "main", opener=opener)
        assert info.sha == "deadbeef" * 5
        assert info.short_sha == ("deadbeef" * 5)[:12]

    def test_http_error_raises_download_error(self):
        import urllib.error

        opener = _make_opener(
            {upd.commit_api_url("o/r", "main"): urllib.error.HTTPError("u", 404, "Not Found", {}, None)}
        )
        with pytest.raises(UpdateDownloadError):
            upd.get_latest_commit("o/r", "main", opener=opener)

    def test_malformed_json_raises_download_error(self):
        opener = _make_opener({upd.commit_api_url("o/r", "main"): b"not json"})
        with pytest.raises(UpdateDownloadError):
            upd.get_latest_commit("o/r", "main", opener=opener)

    def test_missing_expected_keys_raises_download_error(self):
        opener = _make_opener({upd.commit_api_url("o/r", "main"): b'{"unexpected": true}'})
        with pytest.raises(UpdateDownloadError):
            upd.get_latest_commit("o/r", "main", opener=opener)


# ── download_file ─────────────────────────────────────────────────────────────


class TestDownloadFile:
    def test_writes_bytes_to_dest(self, tmp_path):
        dest = tmp_path / "out.zip"
        opener = _make_opener({"http://x/f.zip": b"hello world"})
        upd.download_file("http://x/f.zip", dest, opener=opener)
        assert dest.read_bytes() == b"hello world"

    def test_http_error_raises_download_error(self):
        import urllib.error

        opener = _make_opener({"http://x/f.zip": urllib.error.HTTPError("u", 500, "Server Error", {}, None)})
        with pytest.raises(UpdateDownloadError):
            upd.download_file("http://x/f.zip", Path("/tmp/whatever.zip"), opener=opener)

    def test_url_error_raises_download_error(self):
        import urllib.error

        opener = _make_opener({"http://x/f.zip": urllib.error.URLError("no route to host")})
        with pytest.raises(UpdateDownloadError):
            upd.download_file("http://x/f.zip", Path("/tmp/whatever.zip"), opener=opener)


# ── safe_extract_zip ──────────────────────────────────────────────────────────


class TestSafeExtractZip:
    def test_extracts_files_correctly(self, tmp_path):
        archive = tmp_path / "a.zip"
        archive.write_bytes(_make_archive_bytes(top_dir="proj"))
        dest = tmp_path / "out"

        upd.safe_extract_zip(archive, dest)

        assert (dest / "proj" / "pyproject.toml").exists()
        assert "9.9.9" in (dest / "proj" / "pyproject.toml").read_text()

    def test_preserves_executable_permission(self, tmp_path):
        archive = tmp_path / "a.zip"
        archive.write_bytes(
            _make_archive_bytes(
                top_dir="proj",
                extra_files={"scripts/install.sh": b"#!/bin/bash\necho hi\n"},
                executable_files={"proj/scripts/install.sh"},
            )
        )
        dest = tmp_path / "out"
        upd.safe_extract_zip(archive, dest)

        mode = (dest / "proj" / "scripts" / "install.sh").stat().st_mode
        assert mode & 0o111  # at least one executable bit set

    def test_malformed_archive_raises_archive_error(self, tmp_path):
        archive = tmp_path / "not_a_zip.zip"
        archive.write_bytes(b"this is not a zip file at all")
        with pytest.raises(UpdateArchiveError):
            upd.safe_extract_zip(archive, tmp_path / "out")

    def test_path_traversal_is_rejected(self, tmp_path):
        archive = tmp_path / "evil.zip"
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("proj/pyproject.toml", "[project]\nversion='1'\n")
            zf.writestr("../../evil.txt", "pwned")
        archive.write_bytes(buf.getvalue())

        dest = tmp_path / "out"
        with pytest.raises(UpdateArchiveError):
            upd.safe_extract_zip(archive, dest)

        assert not (tmp_path.parent.parent / "evil.txt").exists()

    def test_absolute_path_is_rejected(self, tmp_path):
        archive = tmp_path / "evil2.zip"
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("/etc/evil.txt", "pwned")
        archive.write_bytes(buf.getvalue())

        with pytest.raises(UpdateArchiveError):
            upd.safe_extract_zip(archive, tmp_path / "out")


# ── find_extracted_project_root ──────────────────────────────────────────────


class TestFindExtractedProjectRoot:
    def test_single_top_level_dir(self, tmp_path):
        (tmp_path / "romcloud-abc123").mkdir()
        (tmp_path / "romcloud-abc123" / "pyproject.toml").write_text("x")
        assert upd.find_extracted_project_root(tmp_path) == tmp_path / "romcloud-abc123"

    def test_pyproject_directly_in_extract_dir(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text("x")
        assert upd.find_extracted_project_root(tmp_path) == tmp_path

    def test_unexpected_layout_raises(self, tmp_path):
        (tmp_path / "a").mkdir()
        (tmp_path / "b").mkdir()
        with pytest.raises(UpdateArchiveError):
            upd.find_extracted_project_root(tmp_path)


# ── build info persistence ───────────────────────────────────────────────────


class TestBuildInfoPersistence:
    def test_round_trip(self, tmp_path):
        info = upd.BuildInfo(
            version="1.2.3", commit="a" * 40, commit_short="a" * 12,
            build_date="2026-08-08T00:00:00+00:00", source="github:o/r@main",
        )
        upd.write_build_info(tmp_path, info)
        loaded = upd.read_build_info(tmp_path)
        assert loaded == info

    def test_missing_file_returns_none(self, tmp_path):
        assert upd.read_build_info(tmp_path) is None

    def test_corrupt_file_returns_none(self, tmp_path):
        (tmp_path / "version.json").write_text("not json{{{")
        assert upd.read_build_info(tmp_path) is None


class TestReadProjectVersion:
    def test_reads_version(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text('[project]\nname = "x"\nversion = "5.6.7"\n')
        assert upd.read_project_version(tmp_path) == "5.6.7"

    def test_missing_file_returns_unknown(self, tmp_path):
        assert upd.read_project_version(tmp_path) == "unknown"

    def test_malformed_file_returns_unknown(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text("not valid toml [[[")
        assert upd.read_project_version(tmp_path) == "unknown"


# ── check_for_update ──────────────────────────────────────────────────────────


class TestCheckForUpdate:
    def test_no_prior_build_info_reports_update_available(self, tmp_path):
        opener = _make_opener(_full_payloads())
        result = upd.check_for_update(tmp_path, opener=opener)
        assert result.current is None
        assert result.update_available is True
        assert result.latest_commit.sha == _SHA

    def test_matching_commit_reports_up_to_date(self, tmp_path):
        upd.write_build_info(
            tmp_path,
            upd.BuildInfo(version="0.1.0", commit=_SHA, commit_short=_SHA[:12], build_date="x", source="s"),
        )
        opener = _make_opener(_full_payloads())
        result = upd.check_for_update(tmp_path, opener=opener)
        assert result.update_available is False

    def test_different_commit_reports_update_available(self, tmp_path):
        upd.write_build_info(
            tmp_path,
            upd.BuildInfo(version="0.1.0", commit="old" * 13, commit_short="old", build_date="x", source="s"),
        )
        opener = _make_opener(_full_payloads())
        result = upd.check_for_update(tmp_path, opener=opener)
        assert result.update_available is True

    def test_unknown_commit_matching_version_reports_up_to_date(self, tmp_path):
        upd.write_build_info(
            tmp_path,
            upd.BuildInfo(version="9.9.9", commit=None, commit_short=None, build_date="x", source="s"),
        )
        archive = _make_archive_bytes(sha=_SHA, version="9.9.9")
        opener = _make_opener(_full_payloads(archive_bytes=archive))

        result = upd.check_for_update(tmp_path, opener=opener)

        assert result.update_available is False
        assert not any(tmp_path.glob("romcloud-update-*"))

    def test_unknown_commit_mismatched_version_reports_update_available(self, tmp_path):
        upd.write_build_info(
            tmp_path,
            upd.BuildInfo(version="1.0.0", commit=None, commit_short=None, build_date="x", source="s"),
        )
        archive = _make_archive_bytes(sha=_SHA, version="2.0.0")
        opener = _make_opener(_full_payloads(archive_bytes=archive))

        result = upd.check_for_update(tmp_path, opener=opener)

        assert result.update_available is True
        assert not any(tmp_path.glob("romcloud-update-*"))

    def test_check_only_mode_never_downloads_or_writes(self, tmp_path):
        """--check must be read-only: no archive download, no version.json write."""
        calls: list = []
        # Only the commit API URL is registered — any attempt to download the
        # archive would raise AssertionError from the fake opener.
        opener = _make_opener(_full_payloads(), calls=calls)

        upd.check_for_update(tmp_path, opener=opener)

        assert not (tmp_path / "version.json").exists()
        assert not any(tmp_path.glob("romcloud-update-*"))
        assert calls == [upd.commit_api_url(upd.DEFAULT_REPO, upd.DEFAULT_BRANCH)]


# ── perform_update ────────────────────────────────────────────────────────────


class TestPerformUpdateSuccess:
    def test_full_successful_update(self, tmp_path):
        home = tmp_path / "romcloud"
        venv_python = home / "venv" / "bin" / "python"
        archive = _make_archive_bytes(sha=_SHA, version="2.0.0")
        opener = _make_opener(_full_payloads(archive_bytes=archive))

        result = upd.perform_update(home, venv_python, opener=opener, runner=_fake_runner_success)

        assert result.previous is None
        assert result.new.commit == _SHA
        assert result.new.version == "2.0.0"

        on_disk = upd.read_build_info(home)
        assert on_disk == result.new

    def test_temp_directory_cleaned_up_on_success(self, tmp_path):
        home = tmp_path / "romcloud"
        archive = _make_archive_bytes(sha=_SHA)
        opener = _make_opener(_full_payloads(archive_bytes=archive))

        upd.perform_update(home, home / "venv" / "bin" / "python", opener=opener, runner=_fake_runner_success)

        assert not any(home.glob("romcloud-update-*"))

    def test_pip_invoked_with_venv_python_and_extracted_project(self, tmp_path):
        home = tmp_path / "romcloud"
        venv_python = home / "venv" / "bin" / "python"
        archive = _make_archive_bytes(sha=_SHA)
        opener = _make_opener(_full_payloads(archive_bytes=archive))

        captured = {}

        def runner(argv, **kwargs):
            captured["argv"] = argv
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

        upd.perform_update(home, venv_python, opener=opener, runner=runner)

        assert captured["argv"][0] == str(venv_python)
        assert captured["argv"][1:4] == ["-m", "pip", "install"]
        assert "--upgrade" in captured["argv"]


class TestPerformUpdateFailedDownload:
    def test_raises_update_download_error(self, tmp_path):
        import urllib.error

        home = tmp_path / "romcloud"
        payloads = _full_payloads()
        payloads[upd.archive_download_url(upd.DEFAULT_REPO, _SHA)] = urllib.error.URLError("network down")
        opener = _make_opener(payloads)

        with pytest.raises(UpdateDownloadError):
            upd.perform_update(home, home / "venv" / "bin" / "python", opener=opener, runner=_fake_runner_success)

    def test_does_not_create_version_json(self, tmp_path):
        import urllib.error

        home = tmp_path / "romcloud"
        payloads = _full_payloads()
        payloads[upd.archive_download_url(upd.DEFAULT_REPO, _SHA)] = urllib.error.URLError("network down")
        opener = _make_opener(payloads)

        with pytest.raises(UpdateDownloadError):
            upd.perform_update(home, home / "venv" / "bin" / "python", opener=opener, runner=_fake_runner_success)

        assert not (home / "version.json").exists()

    def test_cleans_up_temp_dir(self, tmp_path):
        import urllib.error

        home = tmp_path / "romcloud"
        home.mkdir(parents=True)
        payloads = _full_payloads()
        payloads[upd.archive_download_url(upd.DEFAULT_REPO, _SHA)] = urllib.error.URLError("network down")
        opener = _make_opener(payloads)

        with pytest.raises(UpdateDownloadError):
            upd.perform_update(home, home / "venv" / "bin" / "python", opener=opener, runner=_fake_runner_success)

        assert not any(home.glob("romcloud-update-*"))


class TestPerformUpdateMalformedArchive:
    def test_raises_update_archive_error(self, tmp_path):
        home = tmp_path / "romcloud"
        payloads = _full_payloads(archive_bytes=b"garbage, not a zip")
        opener = _make_opener(payloads)

        with pytest.raises(UpdateArchiveError):
            upd.perform_update(home, home / "venv" / "bin" / "python", opener=opener, runner=_fake_runner_success)

        assert not (home / "version.json").exists()
        assert not any(home.glob("romcloud-update-*"))


class TestPerformUpdateFailedInstall:
    def test_raises_update_install_error_with_pip_stderr(self, tmp_path):
        home = tmp_path / "romcloud"
        archive = _make_archive_bytes(sha=_SHA)
        opener = _make_opener(_full_payloads(archive_bytes=archive))

        with pytest.raises(UpdateInstallError, match="disk full"):
            upd.perform_update(
                home, home / "venv" / "bin" / "python", opener=opener,
                runner=_fake_runner_failure("ERROR: disk full"),
            )

    def test_no_partially_upgraded_install_recorded(self, tmp_path):
        """A failed pip install must never overwrite the previous version.json."""
        home = tmp_path / "romcloud"
        previous = upd.BuildInfo(
            version="1.0.0", commit="old" * 13, commit_short="oldold", build_date="x", source="s"
        )
        upd.write_build_info(home, previous)

        archive = _make_archive_bytes(sha=_SHA)
        opener = _make_opener(_full_payloads(archive_bytes=archive))

        with pytest.raises(UpdateInstallError):
            upd.perform_update(
                home, home / "venv" / "bin" / "python", opener=opener,
                runner=_fake_runner_failure("boom"),
            )

        assert upd.read_build_info(home) == previous

    def test_cleans_up_temp_dir(self, tmp_path):
        home = tmp_path / "romcloud"
        archive = _make_archive_bytes(sha=_SHA)
        opener = _make_opener(_full_payloads(archive_bytes=archive))

        with pytest.raises(UpdateInstallError):
            upd.perform_update(
                home, home / "venv" / "bin" / "python", opener=opener,
                runner=_fake_runner_failure("boom"),
            )

        assert not any(home.glob("romcloud-update-*"))


class TestConfigDataCacheUntouched:
    def test_sibling_directories_untouched_by_successful_update(self, tmp_path):
        home = tmp_path / "romcloud"
        config_dir = home / "config"
        data_dir = home / "data"
        logs_dir = home / "logs"
        for d in (config_dir, data_dir, logs_dir):
            d.mkdir(parents=True)
        (config_dir / "romcloud.toml").write_text("sentinel-config")
        (config_dir / "credentials.toml").write_text("sentinel-creds")
        (data_dir / "catalog.db").write_bytes(b"sentinel-db")
        (logs_dir / "romcloud.log").write_text("sentinel-log")

        cache_root = tmp_path / "romcloud-cache"
        cache_root.mkdir()
        (cache_root / "ps2").mkdir()
        (cache_root / "ps2" / "Game.iso").write_bytes(b"sentinel-rom-bytes")

        local_roms = tmp_path / "roms"
        local_roms.mkdir()
        (local_roms / "ps2").mkdir()
        (local_roms / "ps2" / "Game.romcloud").write_text("sentinel-proxy")

        archive = _make_archive_bytes(sha=_SHA)
        opener = _make_opener(_full_payloads(archive_bytes=archive))

        upd.perform_update(home, home / "venv" / "bin" / "python", opener=opener, runner=_fake_runner_success)

        assert (config_dir / "romcloud.toml").read_text() == "sentinel-config"
        assert (config_dir / "credentials.toml").read_text() == "sentinel-creds"
        assert (data_dir / "catalog.db").read_bytes() == b"sentinel-db"
        assert (logs_dir / "romcloud.log").read_text() == "sentinel-log"
        assert (cache_root / "ps2" / "Game.iso").read_bytes() == b"sentinel-rom-bytes"
        assert (local_roms / "ps2" / "Game.romcloud").read_text() == "sentinel-proxy"


class TestNoGitAvailable:
    def test_perform_update_succeeds_without_git_on_path(self, tmp_path, monkeypatch):
        """Reproduces the real Batocera 42 constraint: no git binary at all."""
        empty_bin = tmp_path / "empty-bin"
        empty_bin.mkdir()
        monkeypatch.setenv("PATH", str(empty_bin))

        home = tmp_path / "romcloud"
        archive = _make_archive_bytes(sha=_SHA)
        opener = _make_opener(_full_payloads(archive_bytes=archive))

        result = upd.perform_update(home, home / "venv" / "bin" / "python", opener=opener, runner=_fake_runner_success)
        assert result.new.commit == _SHA

    def test_real_pip_install_of_extracted_project_needs_no_git(self, tmp_path, monkeypatch):
        """Heavier integration check: a real venv + real pip install of the
        extracted (network-faked) project succeeds with git absent from PATH."""
        import shutil
        import subprocess as real_subprocess
        import sys

        empty_bin = tmp_path / "empty-bin"
        empty_bin.mkdir()
        # Keep python3 usable (symlink it in) but exclude git entirely.
        python3 = shutil.which("python3")
        assert python3 is not None
        (empty_bin / "python3").symlink_to(python3)
        monkeypatch.setenv("PATH", str(empty_bin))
        assert shutil.which("git") is None

        home = tmp_path / "romcloud"
        venv_dir = home / "venv"
        create = real_subprocess.run([sys.executable, "-m", "venv", str(venv_dir)], capture_output=True, text=True)
        assert create.returncode == 0, create.stderr

        archive = _make_archive_bytes(sha=_SHA, version="3.3.3")
        opener = _make_opener(_full_payloads(archive_bytes=archive))

        result = upd.perform_update(
            home, venv_dir / "bin" / "python", opener=opener, runner=real_subprocess.run
        )
        assert result.new.version == "3.3.3"


class TestIdempotentRepeatUpdate:
    def test_running_update_twice_is_safe_and_consistent(self, tmp_path):
        home = tmp_path / "romcloud"
        archive = _make_archive_bytes(sha=_SHA, version="4.4.4")
        opener = _make_opener(_full_payloads(archive_bytes=archive))

        first = upd.perform_update(home, home / "venv" / "bin" / "python", opener=opener, runner=_fake_runner_success)
        second = upd.perform_update(home, home / "venv" / "bin" / "python", opener=opener, runner=_fake_runner_success)

        assert first.new.commit == second.new.commit == _SHA
        assert second.previous == first.new
        assert upd.read_build_info(home) == second.new
        assert not any(home.glob("romcloud-update-*"))
