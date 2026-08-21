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
SHA = "a" * 40


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
        f"printf '%s\\n' \"$*\" >> '{curl_log}'\n"
        "url=''\n"
        "output=''\n"
        "while [[ $# -gt 0 ]]; do\n"
        "  if [[ $1 == -o ]]; then output=$2; shift 2; continue; fi\n"
        "  if [[ $1 == http* ]]; then url=$1; fi\n"
        "  shift\n"
        "done\n"
        "if [[ $url == https://api.github.com/* ]]; then\n"
        f"  printf '{{\"sha\":\"{SHA}\"}}' > \"$output\"\n"
        "else\n"
        "  cp \"$ROMCLOUD_TEST_ARCHIVE\" \"$output\"\n"
        "fi\n"
    )
    fake_curl.chmod(0o755)
    (tmp_path / "source.tar.gz").write_bytes(archive_bytes)
    (fake_bin / "tar").symlink_to("/usr/bin/tar")
    return fake_bin, curl_log


def _run(
    tmp_path: Path,
    archive_bytes: bytes,
    *,
    channel: str | None = None,
    extra_env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    fake_bin, _ = _fake_commands(tmp_path, archive_bytes)
    userdata = tmp_path / "userdata"
    userdata.mkdir()
    env = {
        **os.environ,
        "PATH": f"{fake_bin}:/usr/bin:/bin",
        "ROMCLOUD_USERDATA": str(userdata),
        "ROMCLOUD_TEST_ARCHIVE": str(tmp_path / "source.tar.gz"),
        "TMPDIR": str(tmp_path),
        **(extra_env or {}),
    }
    argv = ["bash", str(BOOTSTRAP)]
    if channel is not None:
        argv.extend(["--channel", channel])
    return subprocess.run(argv, env=env, capture_output=True, text=True)


def test_defaults_to_stable_main_and_invokes_canonical_installer(tmp_path: Path) -> None:
    marker = tmp_path / "installed"
    result = _run(tmp_path, _archive(f"#!/usr/bin/env bash\ntouch '{marker}'\n"))

    assert result.returncode == 0, result.stdout + result.stderr
    assert marker.exists()
    assert f"Downloading stryph4/romcloud stable@{SHA[:12]}" in result.stdout
    calls = (tmp_path / "curl-args").read_text()
    assert "/commits/main" in calls
    assert f"https://github.com/stryph4/romcloud/archive/{SHA}.tar.gz" in calls
    assert not list(tmp_path.glob("romcloud-bootstrap.*"))


@pytest.mark.parametrize(
    ("channel", "ref"), [("stable", "main"), ("develop", "develop")]
)
def test_supports_only_allowlisted_channels(tmp_path: Path, channel: str, ref: str) -> None:
    captured = tmp_path / "installer-input"
    installer = (
        "#!/usr/bin/env bash\n"
        f"printf '%s\\n%s\\n%s' \"${{ROMCLOUD_BUILD_COMMIT:-}}\" "
        f"\"${{ROMCLOUD_UPDATE_CHANNEL:-}}\" \"$*\" > '{captured}'\n"
    )
    result = _run(tmp_path, _archive(installer), channel=channel)

    assert result.returncode == 0, result.stdout + result.stderr
    assert f"/commits/{ref}" in (tmp_path / "curl-args").read_text()
    assert captured.read_text().splitlines() == [SHA, channel, f"--channel {channel}"]


def test_propagates_installer_failure_and_cleans_up(tmp_path: Path) -> None:
    result = _run(tmp_path, _archive("#!/usr/bin/env bash\nexit 23\n"))

    assert result.returncode == 23
    assert "Bootstrap complete" not in result.stdout
    assert not list(tmp_path.glob("romcloud-bootstrap.*"))


@pytest.mark.parametrize(
    "channel", ["main", "feature/foo", "../main", "https://evil", "$(id)", "main; id"]
)
def test_rejects_invalid_channel_before_download(tmp_path: Path, channel: str) -> None:
    result = _run(tmp_path, _archive(), channel=channel)

    assert result.returncode != 0
    assert "invalid channel" in result.stderr
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
