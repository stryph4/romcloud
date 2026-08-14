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
3. Reject the download outright if it is zero bytes or implausibly small
   (see ``_MIN_ARCHIVE_BYTES``) — a real source archive is always far
   larger, so a truncated/empty response is caught before it is ever
   opened as a zip.
4. Extract it into an isolated temp directory (under ``romcloud_home`` by
   default, falling back to the system temp dir), verifying every member
   path stays inside the extraction directory first (no path traversal),
   that the archive isn't empty, and that it passes zip CRC integrity
   checking (:func:`safe_extract_zip`).
5. Validate the *extracted* candidate before it ever touches a venv
   (:func:`validate_extracted_candidate`): a defined set of critical files
   must exist and be non-empty, and no ``.py`` file anywhere under ``src/``
   or ``ports_gfx/`` may be zero bytes — this is the check that would have
   caught the real Batocera failure this module is hardened against (a
   corrupted download whose zip opened fine but whose extracted tree was
   hundreds of zero-byte files).
6. Build and pip-install the candidate into a throwaway venv — never
   directly over the active install — then smoke-test it (``--version``,
   ``--help``, and a lightweight subcommand) via
   :func:`_build_and_smoke_test_candidate`. Only a candidate that actually
   runs is ever activated.
7. Activate the validated candidate by swapping it in for the live venv
   directory (the previous venv is kept as a backup until the next step
   succeeds). Reconcile every ROMCloud-managed runtime artifact (the
   ``romcloud``/``romcloud-run`` wrappers, the graphical Ports UI, and —
   only if previously enabled — the Batocera mount service script and the
   EmulationStation override) against that same extracted source tree, via
    :mod:`romcloud.lifecycle.install` (shared with
   ``scripts/install.sh`` so neither duplicates this logic). The wrappers
   are required — a failure there fails the whole update; every other
   artifact is reconciled best-effort ("ROMCloud may fail; Batocera must
   not"). If reconciliation fails after activation, the swap is rolled
   back — the previous venv is restored — before the error propagates.
8. Only once activation *and* the required wrapper reconciliation succeed,
   persist the new :class:`BuildInfo` — any earlier failure leaves the
   previous ``version.json`` (and therefore the previously installed
   backend/wrappers) as the source of truth, so a failed update is never
   reported as installed, and the previously working runtime is left
   untouched.
9. The temp directory (download, extraction, candidate venv, and any
   rolled-back backup venv) is always removed (success or failure).

This module normally leaves ``romcloud.toml``, ``credentials.toml``, the
cache root, the local ROMs directory, the catalog database, and logs alone.
The shared reconciler has one narrow exception: it atomically rewrites exact
pre-beta ROMCloud default mount/cache paths into the consolidated runtime
layout, while physical cleanup remains conservative and best-effort; it never
migrates or deletes remote synchronized data. It *does* rewrite the ``romcloud``/
``romcloud-run`` wrappers, ``version.json``, and — only where already
present/configured — the graphical Ports UI payload, the Batocera mount
service script, and ROMCloud's own EmulationStation override file.
"""

from __future__ import annotations

import json
import signal
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import zipfile
from contextlib import contextmanager
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
from romcloud.core.progress import ProgressSink, emit_progress
from romcloud.lifecycle.install import DEFAULT_PORTS_DIR as _DEFAULT_PORTS_DIR
from romcloud.infrastructure.logging import get_logger

log = get_logger("update")


@contextmanager
def _hard_network_deadline(seconds: float):  # noqa: ANN202
    """Interrupt a stuck stdlib socket call at a true wall-clock deadline.

    Batocera runs updater work on the CLI process's main thread, where POSIX
    ``SIGALRM`` can interrupt even a trickle-fed blocking read. Other hosts
    retain urllib's per-socket timeout and the caller's process deadline.
    """
    if (
        not hasattr(signal, "setitimer")
        or not hasattr(signal, "SIGALRM")
        or threading.current_thread() is not threading.main_thread()
    ):
        yield
        return

    duration = max(0.01, seconds)
    previous_handler = signal.getsignal(signal.SIGALRM)
    previous_timer = signal.getitimer(signal.ITIMER_REAL)
    started = time.monotonic()

    def timed_out(_signum, _frame) -> None:  # noqa: ANN001
        raise TimeoutError(f"network operation exceeded its {duration:.1f}s deadline")

    signal.signal(signal.SIGALRM, timed_out)
    effective = min(duration, previous_timer[0]) if previous_timer[0] > 0 else duration
    signal.setitimer(signal.ITIMER_REAL, effective)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous_handler)
        if previous_timer[0] > 0:
            remaining = max(0.01, previous_timer[0] - (time.monotonic() - started))
            signal.setitimer(signal.ITIMER_REAL, remaining, previous_timer[1])


DEFAULT_REPO = "stryph4/romcloud"
DEFAULT_BRANCH = "main"

_GITHUB_API_BASE = "https://api.github.com"
_GITHUB_WEB_BASE = "https://github.com"
_GITHUB_RAW_BASE = "https://raw.githubusercontent.com"
_USER_AGENT = "romcloud-updater"

# A real GitHub source archive for this project is always far larger than
# this; a downloaded file below it is a truncated/empty/error-page response,
# never real source, and must never reach the extractor.
_MIN_ARCHIVE_BYTES = 2048

# The minimal set of files an extracted candidate must have, present and
# non-empty, before it is ever built into a venv or touches the live
# runtime. Deliberately small and stable — this is a floor, not a full
# manifest; :func:`validate_extracted_candidate` also sweeps every ``.py``
# file under ``src/`` and ``ports_gfx/`` for zero-byte corruption.
_CRITICAL_RELATIVE_FILES = (
    "pyproject.toml",
    "src/romcloud/__init__.py",
    "src/romcloud/cli/main.py",
)

# Argv tails (after "<candidate python> -m romcloud.cli.main") used to prove
# the candidate actually runs before it is ever activated. All three must
# exit zero. "configure --help" is side-effect-free and needs no existing
# romcloud.toml, unlike most other subcommands.
_SMOKE_TEST_ARGV_TAILS = (
    ["--version"],
    ["--help"],
    ["configure", "--help"],
)

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
    latest_version: Optional[str] = None


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


def project_version_url(repo: str, ref: str) -> str:
    return f"{_GITHUB_RAW_BASE}/{repo}/{ref}/pyproject.toml"


def get_project_version_at_commit(
    repo: str,
    ref: str,
    *,
    opener: OpenerType = urllib.request.urlopen,
) -> str | None:
    """Fetch only pyproject metadata for a user-facing available version."""
    try:
        return _fetch_project_version(repo, ref, opener=opener)
    except UpdateDownloadError:
        # This metadata is cosmetic when the installed build already has a
        # commit identity; the commit comparison remains authoritative.
        return None


def _fetch_project_version(
    repo: str,
    ref: str,
    *,
    opener: OpenerType,
    timeout: float = 15.0,
) -> str | None:
    if tomllib is None:
        return None
    request = urllib.request.Request(
        project_version_url(repo, ref), headers={"User-Agent": _USER_AGENT}
    )
    try:
        with _hard_network_deadline(timeout):
            with opener(request, timeout=timeout) as response:
                data = tomllib.loads(response.read().decode("utf-8"))
        value = data.get("project", {}).get("version")
        return str(value) if value else None
    except (OSError, ValueError, UnicodeError) as exc:
        raise UpdateDownloadError(
            f"Failed to read update version metadata for {ref}: {exc}"
        ) from exc


def fetch_json(url: str, *, opener: OpenerType = urllib.request.urlopen, timeout: float = 15.0) -> dict:
    request = urllib.request.Request(
        url, headers={"User-Agent": _USER_AGENT, "Accept": "application/vnd.github+json"}
    )
    try:
        with _hard_network_deadline(timeout):
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
    total_timeout: float = 120.0,
    clock: Callable[[], float] = time.monotonic,
    min_size: int = 0,
) -> None:
    """Download *url* to *dest_path*, then reject the result if it's empty
    or (when *min_size* is given) implausibly small — never leaves a
    truncated/empty download for a caller to mistake for real content."""
    request = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    try:
        deadline = clock() + max(0.01, total_timeout)
        with _hard_network_deadline(total_timeout):
            with opener(request, timeout=timeout) as response, open(dest_path, "wb") as out:
                while True:
                    if clock() >= deadline:
                        raise TimeoutError(
                            f"download exceeded its {total_timeout:.1f}s total deadline"
                        )
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    out.write(chunk)
    except urllib.error.HTTPError as exc:
        raise UpdateDownloadError(f"Failed to download {url}: HTTP {exc.code} {exc.reason}") from exc
    except urllib.error.URLError as exc:
        raise UpdateDownloadError(f"Failed to download {url}: {exc.reason}") from exc
    except (OSError, TimeoutError) as exc:
        raise UpdateDownloadError(f"Failed to download {url}: {exc}") from exc

    size = dest_path.stat().st_size
    if size == 0:
        raise UpdateDownloadError(f"Downloaded file is empty (0 bytes): {url}")
    if min_size and size < min_size:
        raise UpdateDownloadError(
            f"Downloaded file is implausibly small ({size} bytes, expected at "
            f"least {min_size}): {url}"
        )


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
        infolist = zf.infolist()
        if not infolist:
            raise UpdateArchiveError("Downloaded archive is empty (contains no files)")

        # CRC-check every member's compressed data before extracting
        # anything — catches a corrupted/truncated download whose central
        # directory still parses but whose content doesn't match.
        bad_member = zf.testzip()
        if bad_member is not None:
            raise UpdateArchiveError(
                f"Downloaded archive failed integrity check (corrupt member: {bad_member!r})"
            )

        for member in infolist:
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


def validate_extracted_candidate(project_root: Path) -> None:
    """Reject an extracted update candidate before it is built into a venv
    or otherwise touches the live runtime.

    Guards against the exact Batocera failure mode this module exists to
    prevent: a corrupted/truncated download whose zip *opened* successfully
    but whose extracted tree was partly or wholly zero-byte files.
    """
    for rel in _CRITICAL_RELATIVE_FILES:
        path = project_root / rel
        if not path.is_file():
            raise UpdateArchiveError(f"Update candidate is missing a required file: {rel}")
        if path.stat().st_size == 0:
            raise UpdateArchiveError(f"Update candidate has a zero-byte required file: {rel}")

    zero_byte: list[Path] = []
    for sub in ("src", "ports_gfx"):
        base_dir = project_root / sub
        if not base_dir.is_dir():
            continue
        for path in base_dir.rglob("*.py"):
            if path.is_file() and path.stat().st_size == 0:
                zero_byte.append(path.relative_to(project_root))

    if zero_byte:
        raise UpdateArchiveError(
            f"Update candidate contains {len(zero_byte)} zero-byte Python file(s) "
            f"(e.g. {zero_byte[0]}) — refusing to install a corrupted payload"
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
    progress: ProgressSink = None,
) -> CheckResult:
    """Check whether a newer commit is available, without installing anything."""
    emit_progress(
        progress, "update", "check_started", "running", "Checking for ROMCloud updates"
    )
    current = read_build_info(romcloud_home)
    latest = get_latest_commit(repo, branch, opener=opener)
    latest_version: str | None = None
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
    if progress is not None and latest_version is None:
        latest_version = get_project_version_at_commit(
            repo, latest.sha, opener=opener
        )
    message = (
        "A ROMCloud update is available"
        if update_available
        else "ROMCloud is up to date"
    )
    emit_progress(
        progress,
        "update",
        "check_completed",
        "success",
        message,
        metadata={
            "update_available": update_available,
            "available_version": latest_version or latest.short_sha,
        },
    )
    return CheckResult(
        current=current,
        latest_commit=latest,
        update_available=update_available,
        latest_version=latest_version,
    )


def _read_latest_project_version(
    romcloud_home: Path,
    *,
    repo: str,
    latest: CommitInfo,
    opener: OpenerType,
) -> str:
    del romcloud_home  # check-only mode never creates temporary files
    return _fetch_project_version(repo, latest.sha, opener=opener) or "unknown"


def _make_temp_dir(romcloud_home: Path) -> Path:
    """A temp directory under romcloud_home if writable, else under the
    system temp dir (e.g. /tmp) — never anywhere else."""
    try:
        romcloud_home.mkdir(parents=True, exist_ok=True)
        return Path(tempfile.mkdtemp(prefix="romcloud-update-", dir=str(romcloud_home)))
    except OSError:
        return Path(tempfile.mkdtemp(prefix="romcloud-update-"))


def _run_step(
    runner: RunnerType,
    argv: list,
    *,
    timeout: float,
    action: str,
) -> "subprocess.CompletedProcess[str]":
    """Run *argv*, raising :class:`UpdateInstallError` with actionable
    detail on a nonzero exit or a timeout. Shared by every candidate
    staging/activation step."""
    try:
        result = runner(argv, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise UpdateInstallError(f"{action} timed out after {timeout:.0f}s") from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "unknown error").strip()
        raise UpdateInstallError(f"{action} failed: {detail}")
    return result


def _build_and_smoke_test_candidate(
    candidate_dir: Path,
    base_python: Path,
    project_root: Path,
    *,
    runner: RunnerType,
    timeout: float,
) -> None:
    """Build the extracted candidate into a throwaway venv and prove it
    actually runs — never pip-install an unproven payload directly over the
    active install. Raises :class:`UpdateInstallError` on any failure,
    before the live runtime has been touched.
    """
    _run_step(
        runner,
        [str(base_python), "-m", "venv", str(candidate_dir)],
        timeout=timeout,
        action="Creating the candidate environment",
    )
    candidate_python = candidate_dir / "bin" / "python"

    try:
        pip_check = runner(
            [str(candidate_python), "-m", "pip", "--version"],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        pip_present = pip_check.returncode == 0
    except subprocess.TimeoutExpired:
        pip_present = False
    if not pip_present:
        _run_step(
            runner,
            [str(candidate_python), "-m", "ensurepip", "--upgrade"],
            timeout=timeout,
            action="Bootstrapping pip in the candidate environment",
        )

    _run_step(
        runner,
        [str(candidate_python), "-m", "pip", "install", "--upgrade", "--quiet", str(project_root)],
        timeout=timeout,
        action="Installing the update into the candidate environment",
    )

    for tail in _SMOKE_TEST_ARGV_TAILS:
        _run_step(
            runner,
            [str(candidate_python), "-m", "romcloud.cli.main", *tail],
            timeout=timeout,
            action=f"Candidate smoke test ({' '.join(tail)})",
        )


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
    progress: ProgressSink = None,
    install_timeout: float = 300.0,
    reconcile_timeout: float = 120.0,
) -> UpdateResult:
    """Download the latest commit's archive, build and smoke-test it in a
    throwaway candidate venv, activate it in place of the live venv, and
    reconcile every ROMCloud-managed runtime artifact (wrappers, graphical
    Ports UI, previously-enabled Batocera integrations) against that exact
    same source revision.

    Never leaves a partially-upgraded install recorded as current: the new
    :class:`BuildInfo` is only written after activation *and* reconciling
    the required core wrappers succeed — a failure anywhere along the way
    leaves the previous ``version.json`` and the previously installed
    backend/wrappers untouched, so a failed update is never reported as
    installed and the working runtime is never left in a half-upgraded
    state. Reconciling optional artifacts (the graphical Ports UI, the
    mount service script, the EmulationStation override) is best-effort
    and never fails the update — "ROMCloud may fail; Batocera must not".
    The temporary download/extraction/candidate-venv directory is always
    removed, on success or failure.
    """
    previous = read_build_info(romcloud_home)
    manager_was_running = False
    manager_data_path = romcloud_home / "data"
    config_path = romcloud_home / "config" / "romcloud.toml"
    if config_path.is_file():
        try:
            from romcloud.infrastructure.config import load_config

            manager_data_path = Path(load_config(str(config_path)).data_path)
        except Exception:  # noqa: BLE001 - update can proceed with the standard layout
            pass
    emit_progress(
        progress, "update", "resolve", "running", "Resolving the latest ROMCloud release"
    )
    latest = get_latest_commit(repo, branch, opener=opener)

    tmp_root = _make_temp_dir(romcloud_home)
    try:
        archive_path = tmp_root / "romcloud-update.zip"
        emit_progress(
            progress, "update", "download", "running", "Downloading the update"
        )
        download_file(
            archive_download_url(repo, latest.sha),
            archive_path,
            opener=opener,
            min_size=_MIN_ARCHIVE_BYTES,
        )

        extract_dir = tmp_root / "extracted"
        emit_progress(
            progress, "update", "verify", "running", "Verifying and unpacking the update"
        )
        safe_extract_zip(archive_path, extract_dir)
        project_root = find_extracted_project_root(extract_dir)
        validate_extracted_candidate(project_root)

        candidate_dir = tmp_root / "venv-candidate"
        log.info("Staging update candidate at %s from %s", candidate_dir, project_root)
        emit_progress(
            progress, "update", "stage", "running", "Building and smoke-testing the update"
        )
        _build_and_smoke_test_candidate(
            candidate_dir,
            venv_python,
            project_root,
            runner=runner,
            timeout=install_timeout,
        )

        from romcloud.web.lifecycle import manager_status, stop_manager

        manager_was_running = bool(manager_status(manager_data_path).get("running"))
        if manager_was_running:
            stop_manager(manager_data_path)

        resolved_ports_dir = Path(ports_dir) if ports_dir else _DEFAULT_PORTS_DIR
        live_venv_dir = venv_python.parent.parent
        backup_venv_dir = tmp_root / "venv-previous"
        had_previous_venv = live_venv_dir.exists()

        log.info("Activating validated candidate at %s", live_venv_dir)
        emit_progress(
            progress, "update", "install", "running", "Activating the ROMCloud update"
        )
        if had_previous_venv:
            live_venv_dir.rename(backup_venv_dir)
        candidate_dir.rename(live_venv_dir)

        try:
            log.info("Reconciling installed runtime artifacts from %s", project_root)
            emit_progress(
                progress,
                "update",
                "reconcile",
                "running",
                "Updating ROMCloud launchers and integrations",
            )
            try:
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
                    timeout=reconcile_timeout,
                )
            except subprocess.TimeoutExpired as exc:
                raise UpdateInstallError(
                    f"Runtime reconciliation timed out after {reconcile_timeout:.0f}s"
                ) from exc
            reconcile_log = (reconcile_result.stdout or "") + (reconcile_result.stderr or "")
            if reconcile_result.returncode != 0:
                detail = reconcile_log.strip() or "unknown reconciliation error"
                raise UpdateInstallError(
                    f"Backend upgraded, but failed to reconcile installed runtime artifacts: {detail}"
                )
        except BaseException:
            # The validated candidate is already activated; if reconciling
            # the runtime it depends on fails, restore whatever was running
            # before rather than leave a half-activated update in place.
            log.warning(
                "Reconciliation failed after activation — rolling back to the previous venv"
            )
            shutil.rmtree(live_venv_dir, ignore_errors=True)
            if had_previous_venv:
                backup_venv_dir.rename(live_venv_dir)
            raise

        new_info = BuildInfo(
            version=read_project_version(project_root),
            commit=latest.sha,
            commit_short=latest.short_sha,
            build_date=datetime.now(timezone.utc).isoformat(),
            source=f"github:{repo}@{branch}",
        )
        write_build_info(romcloud_home, new_info)
        log.info("Updated ROMCloud to %s (%s)", new_info.version, new_info.commit_short)
        emit_progress(
            progress,
            "update",
            "completed",
            "success",
            f"ROMCloud {new_info.version} installed successfully",
            metadata={"version": new_info.version, "restart_required": True},
        )
        return UpdateResult(previous=previous, new=new_info, reconcile_log=reconcile_log.strip())
    finally:
        shutil.rmtree(tmp_root, ignore_errors=True)
        if manager_was_running:
            from romcloud.web.lifecycle import start_manager

            start_manager(romcloud_home / "bin" / "romcloud", manager_data_path)
