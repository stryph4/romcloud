"""Tests for the thin, git-free Batocera bootstrap script."""

from __future__ import annotations

import io
import os
import subprocess
import tarfile
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
BOOTSTRAP = REPO_ROOT / "scripts" / "bootstrap.sh"


def _archive(installer: str = "#!/usr/bin/env bash\nexit 0\n") -> bytes:
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w:gz") as archive:
        data = installer.encode()
        info = tarfile.TarInfo("romcloud-test/scripts/install.sh")
        info.size = len(data)
        info.mode = 0o755
        archive.addfile(info, io.BytesIO(data))
    return output.getvalue()


def _fake_commands(tmp_path: Path, archive_bytes: bytes) -> tuple[Path, Path]:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    curl_log = tmp_path / "curl-args"
    fake_curl = fake_bin / "curl"
    fake_curl.write_text(
        "#!/usr/bin/env bash\n"
        f"printf '%s\\n' \"$@\" > '{curl_log}'\n"
        "while [[ $# -gt 0 ]]; do\n"
        "  if [[ $1 == -o ]]; then cp \"$ROMCLOUD_TEST_ARCHIVE\" \"$2\"; exit 0; fi\n"
        "  shift\n"
        "done\n"
        "exit 2\n"
    )
    fake_curl.chmod(0o755)
    (tmp_path / "source.tar.gz").write_bytes(archive_bytes)
    (fake_bin / "tar").symlink_to("/usr/bin/tar")
    return fake_bin, curl_log


def _run(
    tmp_path: Path,
    archive_bytes: bytes,
    *,
    ref: str = "main",
    extra_env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    fake_bin, _ = _fake_commands(tmp_path, archive_bytes)
    userdata = tmp_path / "userdata"
    userdata.mkdir()
    env = {
        **os.environ,
        "PATH": f"{fake_bin}:/usr/bin:/bin",
        "ROMCLOUD_USERDATA": str(userdata),
        "ROMCLOUD_REF": ref,
        "ROMCLOUD_TEST_ARCHIVE": str(tmp_path / "source.tar.gz"),
        "TMPDIR": str(tmp_path),
        **(extra_env or {}),
    }
    return subprocess.run(["bash", str(BOOTSTRAP)], env=env, capture_output=True, text=True)


def test_defaults_to_main_and_invokes_canonical_installer(tmp_path: Path) -> None:
    marker = tmp_path / "installed"
    result = _run(tmp_path, _archive(f"#!/usr/bin/env bash\ntouch '{marker}'\n"))

    assert result.returncode == 0, result.stdout + result.stderr
    assert marker.exists()
    assert "Downloading stryph4/romcloud@main" in result.stdout
    assert "https://github.com/stryph4/romcloud/archive/main.tar.gz" in (tmp_path / "curl-args").read_text()
    assert not list(tmp_path.glob("romcloud-bootstrap.*"))


@pytest.mark.parametrize("ref", ["v0.1.0", "feature/bootstrap", "a" * 40])
def test_supports_branch_tag_and_commit_refs(tmp_path: Path, ref: str) -> None:
    captured = tmp_path / "commit"
    installer = f"#!/usr/bin/env bash\nprintf '%s' \"${{ROMCLOUD_BUILD_COMMIT:-}}\" > '{captured}'\n"
    result = _run(tmp_path, _archive(installer), ref=ref)

    assert result.returncode == 0, result.stdout + result.stderr
    assert f"archive/{ref}.tar.gz" in (tmp_path / "curl-args").read_text()
    assert captured.read_text() == (ref if len(ref) == 40 else "")


def test_propagates_installer_failure_and_cleans_up(tmp_path: Path) -> None:
    result = _run(tmp_path, _archive("#!/usr/bin/env bash\nexit 23\n"))

    assert result.returncode == 23
    assert "Bootstrap complete" not in result.stdout
    assert not list(tmp_path.glob("romcloud-bootstrap.*"))


def test_rejects_invalid_ref_before_download(tmp_path: Path) -> None:
    result = _run(tmp_path, _archive(), ref="../main")

    assert result.returncode != 0
    assert "invalid ROMCLOUD_REF" in result.stderr
    assert not (tmp_path / "curl-args").exists()


def test_requires_writable_userdata(tmp_path: Path) -> None:
    fake_bin, _ = _fake_commands(tmp_path, _archive())
    env = {
        **os.environ,
        "PATH": f"{fake_bin}:/usr/bin:/bin",
        "ROMCLOUD_USERDATA": str(tmp_path / "missing"),
        "ROMCLOUD_TEST_ARCHIVE": str(tmp_path / "source.tar.gz"),
    }
    result = subprocess.run(["bash", str(BOOTSTRAP)], env=env, capture_output=True, text=True)

    assert result.returncode != 0
    assert "does not exist" in result.stderr


def test_rejects_archive_without_canonical_installer(tmp_path: Path) -> None:
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w:gz") as archive:
        data = b"not an installer"
        info = tarfile.TarInfo("romcloud-test/README.md")
        info.size = len(data)
        archive.addfile(info, io.BytesIO(data))

    result = _run(tmp_path, output.getvalue())

    assert result.returncode != 0
    assert "does not contain scripts/install.sh" in result.stderr
    assert not list(tmp_path.glob("romcloud-bootstrap.*"))
