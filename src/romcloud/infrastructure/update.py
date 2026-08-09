"""Self-update — download the latest source from GitHub and upgrade the
persistent venv in place. No git dependency; stdlib-only networking,
archive extraction, and JSON handling for maximum portability across
Batocera builds.

Design
------
1. Resolve the latest commit SHA on *branch* via the GitHub REST API
   (``GET /repos/{repo}/commits/{branch}``) — this is also the "build
   identifier" persisted at :func:`write_build_info` time, since formal
   PyPI-style versioning isn't bumped for every change (see
   ``romcloud.__version__`` for that, which is a secondary/display-only
   signal here).
2. Download the archive for *that exact commit*
   (``https://github.com/{repo}/archive/{sha}.zip``) — not the branch name —
   so what gets installed and what gets recorded as "current" can never
   drift apart between the check and the download.
3. Extract it into an isolated temp directory (under ``romcloud_home`` by
   default, falling back to the system temp dir), verifying every member
   path stays inside the extraction directory first (no path traversal).
4. Upgrade the existing persistent venv in place:
   ``<venv python> -m pip install --upgrade <extracted project>``.
5. Reconcile every ROMCloud-managed runtime artifact (the ``romcloud``/
   ``romcloud-run`` wrappers, the graphical Ports UI, and — only if
   previously enabled — the Batocera mount service script and the
   EmulationStation override) against that same extracted source tree, via
   :mod:`romcloud.infrastructure.installer` (shared with
   ``scripts/install.sh`` so neither duplicates this logic). The wrappers
   are required — a failure there fails the whole update; every other
   artifact is reconciled best-effort ("ROMCloud may fail; Batocera must
   not").
6. Only once both the venv upgrade *and* the required wrapper reconciliation
   succeed, persist the new :class:`BuildInfo` — any earlier failure leaves
   the previous ``version.json`` (and therefore the previously installed
   backend/wrappers) as the source of truth, so a failed update is never
   reported as installed.
7. The temp directory is always removed (success or failure).

This module never touches ``romcloud.toml``, ``credentials.toml``, the
cache root, the local ROMs directory, the catalog database, or logs — those
are exclusively user/runtime state. It *does* rewrite the ``romcloud``/
``romcloud-run`` wrappers, ``version.json``, and — only where already
present/configured — the graphical Ports UI payload, the Batocera mount
service script, and ROMCloud's own EmulationStation override file.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

if sys.version_info >= (3, 11):
    import tomllib
else:
    try:
        import tomllib  # type: ignore[no-redef]
    except ImportError:
        try:
            import tomli as tomllib  # type: ignore[no-redef]
        except ImportError:
            tomllib = None  # type: ignore[assignment]

from romcloud.core.exceptions import (
    UpdateArchiveError,
    UpdateDownloadError,
    UpdateInstallError,
)
from romcloud.infrastructure.installer import DEFAULT_PORTS_DIR as _DEFAULT_PORTS_DIR
from romcloud.infrastructure.logging import get_logger

log = get_logger("update")

DEFAULT_REPO = "stryph4/romcloud"
DEFAULT_BRANCH = "main"

_GITHUB_API_BASE = "https://api.github.com"
_GITHUB_WEB_BASE = "https://github.com"
_USER_AGENT = "romcloud-updater"

OpenerType = Callable[..., object]
RunnerType = Callable[..., "subprocess.CompletedProcess[str]"]


# ── data types ────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class CommitInfo:
    sha: str
    date: str
    message: str

    @property
    def short_sha(self) -> str:
        return self.sha[:12]


@dataclass(frozen=True)
class BuildInfo:
    version: str
    commit: Optional[str]
    commit_short: Optional[str]
    build_date: str
    source: str


@dataclass(frozen=True)
class CheckResult:
    current: Optional[BuildInfo]
    latest_commit: CommitInfo
    update_available: bool


@dataclass(frozen=True)
class UpdateResult:
    previous: Optional[BuildInfo]
    new: BuildInfo
    reconcile_log: str = ""
    """Captured stdout/stderr from reconciling installer-managed runtime
    artifacts (wrappers, graphical Ports UI, previously-enabled Batocera
    integrations) — see :func:`perform_update`."""


# ── GitHub API / download ────────────────────────────────────────────────────


def commit_api_url(repo: str, branch: str) -> str:
    return f"{_GITHUB_API_BASE}/repos/{repo}/commits/{branch}"


def archive_download_url(repo: str, ref: str) -> str:
    """URL for a GitHub source archive at *ref* (a branch, tag, or commit SHA)."""
    return f"{_GITHUB_WEB_BASE}/{repo}/archive/{ref}.zip"


def fetch_json(url: str, *, opener: OpenerType = urllib.request.urlopen, timeout: float = 15.0) -> dict:
    request = urllib.request.Request(
        url, headers={"User-Agent": _USER_AGENT, "Accept": "application/vnd.github+json"}
    )
    try:
        with opener(request, timeout=timeout) as response:
            data = response.read()
    except urllib.error.HTTPError as exc:
        raise UpdateDownloadError(f"GitHub API request failed for {url}: HTTP {exc.code} {exc.reason}") from exc
    except urllib.error.URLError as exc:
        raise UpdateDownloadError(f"GitHub API request failed for {url}: {exc.reason}") from exc
    except OSError as exc:
        raise UpdateDownloadError(f"GitHub API request failed for {url}: {exc}") from exc

    try:
        return json.loads(data)
    except json.JSONDecodeError as exc:
        raise UpdateDownloadError(f"GitHub API returned invalid JSON for {url}: {exc}") from exc


def get_latest_commit(
    repo: str = DEFAULT_REPO,
    branch: str = DEFAULT_BRANCH,
    *,
    opener: OpenerType = urllib.request.urlopen,
) -> CommitInfo:
    data = fetch_json(commit_api_url(repo, branch), opener=opener)
    try:
        sha = data["sha"]
        commit = data["commit"]
        date = commit["committer"]["date"]
        message = (commit.get("message") or "").splitlines()[0] if commit.get("message") else ""
    except (KeyError, TypeError) as exc:
        raise UpdateDownloadError(f"Unexpected GitHub API response shape: {exc}") from exc
    return CommitInfo(sha=sha, date=date, message=message)


def download_file(
    url: str,
    dest_path: Path,
    *,
    opener: OpenerType = urllib.request.urlopen,
    timeout: float = 60.0,
) -> None:
    request = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    try:
        with opener(request, timeout=timeout) as response, open(dest_path, "wb") as out:
            shutil.copyfileobj(response, out)
    except urllib.error.HTTPError as exc:
        raise UpdateDownloadError(f"Failed to download {url}: HTTP {exc.code} {exc.reason}") from exc
    except urllib.error.URLError as exc:
        raise UpdateDownloadError(f"Failed to download {url}: {exc.reason}") from exc
    except OSError as exc:
        raise UpdateDownloadError(f"Failed to download {url}: {exc}") from exc


# ── safe archive extraction ──────────────────────────────────────────────────


def _resolve_member_path(dest_dir: Path, member_name: str) -> Path:
    """Resolve *member_name* (a zip entry name) under *dest_dir*, refusing
    to ever produce a path outside of it (zip-slip / path traversal)."""
    normalized = member_name.replace("\\", "/")
    if normalized.startswith("/"):
        raise UpdateArchiveError(f"Unsafe path in archive (absolute path): {member_name!r}")

    parts = [p for p in normalized.split("/") if p not in ("", ".")]
    if any(part == ".." for part in parts):
        raise UpdateArchiveError(f"Unsafe path in archive (path traversal): {member_name!r}")

    target = dest_dir.joinpath(*parts) if parts else dest_dir
    resolved_dest = dest_dir.resolve()
    resolved_target = target.resolve()
    if resolved_target != resolved_dest and resolved_dest not in resolved_target.parents:
        raise UpdateArchiveError(f"Unsafe path in archive (escapes extraction dir): {member_name!r}")
    return target


def _apply_unix_permissions(member: "zipfile.ZipInfo", target: Path) -> None:
    mode = (member.external_attr >> 16) & 0o777
    if mode:
        try:
            target.chmod(mode)
        except OSError:
            pass


def safe_extract_zip(zip_path: Path, dest_dir: Path) -> None:
    """Extract *zip_path* into *dest_dir*, rejecting any entry that would
    write outside of it. Preserves each member's unix file permissions."""
    dest_dir.mkdir(parents=True, exist_ok=True)

    try:
        zf = zipfile.ZipFile(zip_path)
    except zipfile.BadZipFile as exc:
        raise UpdateArchiveError(f"Downloaded archive is not a valid zip file: {exc}") from exc

    with zf:
        for member in zf.infolist():
            target = _resolve_member_path(dest_dir, member.filename)
            if member.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(member) as src, open(target, "wb") as dst:
                shutil.copyfileobj(src, dst)
            _apply_unix_permissions(member, target)


def find_extracted_project_root(extract_dir: Path) -> Path:
    """Locate the project root inside an extracted GitHub archive.

    GitHub source archives always contain exactly one top-level directory
    (e.g. ``romcloud-<sha>/``); return it.
    """
    entries = list(extract_dir.iterdir())
    dirs = [p for p in entries if p.is_dir()]

    if len(entries) == 1 and len(dirs) == 1:
        return dirs[0]
    if (extract_dir / "pyproject.toml").exists():
        return extract_dir

    raise UpdateArchiveError(
        f"Unexpected archive layout in {extract_dir} — expected a single top-level directory."
    )


# ── build/version metadata ───────────────────────────────────────────────────


def _version_file(romcloud_home: Path) -> Path:
    return romcloud_home / "version.json"


def read_build_info(romcloud_home: Path) -> Optional[BuildInfo]:
    """Read the persisted build identity. Returns None if missing/corrupt —
    never raises (a missing/unreadable version.json just means "unknown")."""
    path = _version_file(romcloud_home)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return BuildInfo(
        version=str(data.get("version", "unknown")),
        commit=data.get("commit"),
        commit_short=data.get("commit_short"),
        build_date=str(data.get("build_date", "")),
        source=str(data.get("source", "")),
    )


def write_build_info(romcloud_home: Path, info: BuildInfo) -> Path:
    path = _version_file(romcloud_home)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": info.version,
        "commit": info.commit,
        "commit_short": info.commit_short,
        "build_date": info.build_date,
        "source": info.source,
    }
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return path


def read_project_version(project_root: Path) -> str:
    """Best-effort read of the `[project] version` field from pyproject.toml.

    Never raises; falls back to "unknown" so a metadata read failure never
    aborts an otherwise-successful update.
    """
    pyproject = project_root / "pyproject.toml"
    if tomllib is None or not pyproject.exists():
        return "unknown"
    try:
        with pyproject.open("rb") as fh:
            data = tomllib.load(fh)
        return str(data.get("project", {}).get("version", "unknown"))
    except Exception:  # noqa: BLE001
        return "unknown"


# ── orchestration ─────────────────────────────────────────────────────────────


def check_for_update(
    romcloud_home: Path,
    *,
    repo: str = DEFAULT_REPO,
    branch: str = DEFAULT_BRANCH,
    opener: OpenerType = urllib.request.urlopen,
) -> CheckResult:
    """Check whether a newer commit is available, without installing anything."""
    current = read_build_info(romcloud_home)
    latest = get_latest_commit(repo, branch, opener=opener)
    if current is None:
        update_available = True
    elif current.commit is None:
        if current.version == "unknown":
            update_available = True
        else:
            latest_version = _read_latest_project_version(
                romcloud_home, repo=repo, latest=latest, opener=opener
            )
            update_available = current.version != latest_version
    else:
        update_available = current.commit != latest.sha
    return CheckResult(current=current, latest_commit=latest, update_available=update_available)


def _read_latest_project_version(
    romcloud_home: Path,
    *,
    repo: str,
    latest: CommitInfo,
    opener: OpenerType,
) -> str:
    tmp_root = _make_temp_dir(romcloud_home)
    try:
        archive_path = tmp_root / "romcloud-update.zip"
        download_file(archive_download_url(repo, latest.sha), archive_path, opener=opener)

        extract_dir = tmp_root / "extracted"
        safe_extract_zip(archive_path, extract_dir)
        project_root = find_extracted_project_root(extract_dir)
        return read_project_version(project_root)
    finally:
        shutil.rmtree(tmp_root, ignore_errors=True)


def _make_temp_dir(romcloud_home: Path) -> Path:
    """A temp directory under romcloud_home if writable, else under the
    system temp dir (e.g. /tmp) — never anywhere else."""
    try:
        romcloud_home.mkdir(parents=True, exist_ok=True)
        return Path(tempfile.mkdtemp(prefix="romcloud-update-", dir=str(romcloud_home)))
    except OSError:
        return Path(tempfile.mkdtemp(prefix="romcloud-update-"))


def perform_update(
    romcloud_home: Path,
    venv_python: Path,
    *,
    repo: str = DEFAULT_REPO,
    branch: str = DEFAULT_BRANCH,
    opener: OpenerType = urllib.request.urlopen,
    runner: RunnerType = subprocess.run,
    ports_dir: Optional[Path] = None,
    system_python: Optional[str] = None,
) -> UpdateResult:
    """Download the latest commit's archive, upgrade the persistent venv,
    and reconcile every ROMCloud-managed runtime artifact (wrappers,
    graphical Ports UI, previously-enabled Batocera integrations) against
    that exact same source revision.

    Never leaves a partially-upgraded install recorded as current: the new
    :class:`BuildInfo` is only written after both the venv upgrade *and*
    reconciling the required core wrappers succeed — a failure in either
    leaves the previous ``version.json`` (and therefore the previously
    installed backend/wrappers) as the source of truth, so a failed update
    is never reported as installed. Reconciling optional artifacts (the
    graphical Ports UI, the mount service script, the EmulationStation
    override) is best-effort and never fails the update — "ROMCloud may
    fail; Batocera must not". The temporary download/extraction directory is
    always removed, on success or failure.
    """
    previous = read_build_info(romcloud_home)
    latest = get_latest_commit(repo, branch, opener=opener)

    tmp_root = _make_temp_dir(romcloud_home)
    try:
        archive_path = tmp_root / "romcloud-update.zip"
        download_file(archive_download_url(repo, latest.sha), archive_path, opener=opener)

        extract_dir = tmp_root / "extracted"
        safe_extract_zip(archive_path, extract_dir)
        project_root = find_extracted_project_root(extract_dir)

        log.info("Upgrading venv at %s from %s", venv_python, project_root)
        result = runner(
            [str(venv_python), "-m", "pip", "install", "--upgrade", "--quiet", str(project_root)],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "unknown pip error").strip()
            raise UpdateInstallError(f"Failed to install update into the venv: {detail}")

        resolved_ports_dir = Path(ports_dir) if ports_dir else _DEFAULT_PORTS_DIR
        log.info("Reconciling installed runtime artifacts from %s", project_root)
        reconcile_result = runner(
            [
                str(venv_python),
                "-m",
                "romcloud.cli.main",
                "_reconcile-install",
                "--romcloud-home",
                str(romcloud_home),
                "--project-root",
                str(project_root),
                "--ports-dir",
                str(resolved_ports_dir),
                "--system-python",
                system_python or "",
            ],
            capture_output=True,
            text=True,
        )
        reconcile_log = (reconcile_result.stdout or "") + (reconcile_result.stderr or "")
        if reconcile_result.returncode != 0:
            detail = reconcile_log.strip() or "unknown reconciliation error"
            raise UpdateInstallError(
                f"Backend upgraded, but failed to reconcile installed runtime artifacts: {detail}"
            )

        new_info = BuildInfo(
            version=read_project_version(project_root),
            commit=latest.sha,
            commit_short=latest.short_sha,
            build_date=datetime.now(timezone.utc).isoformat(),
            source=f"github:{repo}@{branch}",
        )
        write_build_info(romcloud_home, new_info)
        log.info("Updated ROMCloud to %s (%s)", new_info.version, new_info.commit_short)
        return UpdateResult(previous=previous, new=new_info, reconcile_log=reconcile_log.strip())
    finally:
        shutil.rmtree(tmp_root, ignore_errors=True)
