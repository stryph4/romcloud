"""Read-only diagnostics and an explicit, narrow Quick Repair registry."""

from __future__ import annotations

import json
import os
import sqlite3
import stat
import subprocess
import sys
import signal
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Callable, Iterable, Mapping
from urllib.parse import quote
from xml.etree import ElementTree as ET

from romcloud.core.models.troubleshoot import (
    FindingFix,
    RestartRequirements,
    TroubleshootFinding,
    TroubleshootReport,
)
from romcloud.core.progress import ProgressSink, emit_progress
from romcloud.infrastructure.config import AppConfig, load_config_read_only
from romcloud.infrastructure.library_view import inspect_operating_mode


@dataclass(frozen=True)
class ActivityState:
    state: str = "unknown"  # active, inactive, unknown
    detail: str = ""


@dataclass(frozen=True)
class ActivitySnapshot:
    game: ActivityState = field(default_factory=ActivityState)
    download: ActivityState = field(default_factory=ActivityState)
    savesync: ActivityState = field(default_factory=ActivityState)
    library_sync: ActivityState = field(default_factory=ActivityState)
    browser_manager: ActivityState = field(default_factory=ActivityState)
    graphical_ui: ActivityState = field(default_factory=ActivityState)

    def blockers(self, *names: str) -> tuple[str, ...]:
        blocked: list[str] = []
        for name in names:
            value = getattr(self, name)
            if value.state != "inactive":
                blocked.append(f"{name}:{value.state}")
        return tuple(blocked)


@dataclass(frozen=True)
class TroubleshootPaths:
    """Injectable Batocera integration paths, primarily for tests."""

    auto_savesync_hook: Path = Path("/userdata/system/scripts/romcloud-autosync")
    mount_service: Path = Path("/userdata/system/services/romcloud_mount")
    services_config: Path = Path("/userdata/system/batocera.conf")
    es_stock: Path = Path("/usr/share/emulationstation/es_systems.cfg")
    es_override: Path = Path(
        "/userdata/system/configs/emulationstation/es_systems_romcloud.cfg"
    )


@dataclass(frozen=True)
class DiagnosticContext:
    config_path: Path
    config: AppConfig
    romcloud_home: Path
    paths: TroubleshootPaths
    activity: ActivitySnapshot
    catalog_available: bool
    managed_systems: tuple[str, ...] = ()


class CancellationToken:
    def __init__(self) -> None:
        self.requested = False

    def request(self, *_args) -> None:  # noqa: ANN002 - signal handler shape
        self.requested = True

    def __call__(self) -> bool:
        return self.requested


@contextmanager
def cooperative_cancellation():
    """Turn SIGTERM/SIGINT into a request checked between diagnostics/fixes."""

    token = CancellationToken()
    previous: dict[int, object] = {}
    for signum in (signal.SIGTERM, signal.SIGINT):
        try:
            previous[signum] = signal.getsignal(signum)
            signal.signal(signum, token.request)
        except (ValueError, OSError):
            pass
    try:
        yield token
    finally:
        for signum, handler in previous.items():
            try:
                signal.signal(signum, handler)
            except (ValueError, OSError):
                pass


def _finding(
    finding_id: str,
    component: str,
    status: str,
    message: str,
    *,
    detail: str = "",
    severity: str | None = None,
    fixability: str = "none",
    blocked_by: Iterable[str] = (),
    metadata: Mapping[str, object] | None = None,
    restart: RestartRequirements | None = None,
) -> TroubleshootFinding:
    return TroubleshootFinding(
        id=finding_id,
        component=component,
        status=status,
        severity=severity or ("error" if status == "error" else "warning" if status in {"warning", "skipped"} else "info"),
        message=message,
        detail=detail,
        fixability=fixability,
        blocked_by=tuple(blocked_by),
        metadata=metadata or {},
        restart_required=restart or RestartRequirements(),
    )


def _safe_read(path: Path) -> tuple[str | None, str]:
    try:
        return path.read_text(encoding="utf-8"), ""
    except (OSError, UnicodeError) as exc:
        return None, str(exc)


def _file_matches(path: Path, expected: str) -> bool:
    content, _ = _safe_read(path)
    return content == expected


def inspect_activity(config: AppConfig, *, catalog_path: Path) -> ActivitySnapshot:
    """Best-effort snapshot which never creates, removes, or locks a path."""

    from romcloud.services.auto_savesync import ActiveSessionStore
    from romcloud.web.lifecycle import manager_status

    sessions_root = Path(config.data_path) / "savesync-sessions"
    if sessions_root.exists():
        game_active = ActiveSessionStore(Path(config.data_path)).has_active_session()
        game = ActivityState("active" if game_active else "inactive")
    else:
        game = ActivityState("unknown", "No lifecycle-session directory exists.")

    download = ActivityState("unknown", "Catalog download state is unavailable.")
    if catalog_path.is_file():
        try:
            with _open_sqlite_read_only(catalog_path) as conn:
                row = conn.execute(
                    "SELECT COUNT(*) FROM download_items WHERE state IN ('running','verifying')"
                ).fetchone()
            download = ActivityState("active" if int(row[0]) else "inactive")
        except sqlite3.Error as exc:
            download = ActivityState("unknown", str(exc))

    savesync = _inspect_existing_lock(Path(config.data_path) / ".savesync-auto.lock")
    library_lock = _local_library_lock(config)
    library_sync = _inspect_existing_lock(library_lock) if library_lock else ActivityState(
        "unknown", "The Library Sync lock is remote or not configured."
    )

    try:
        manager = manager_status(config.data_path)
        browser = ActivityState(
            "active" if manager.get("running") else "inactive",
            "Owned manager endpoint is reachable." if manager.get("running") else "",
        )
    except Exception as exc:  # noqa: BLE001 - diagnostic isolation
        browser = ActivityState("unknown", str(exc))

    return ActivitySnapshot(
        game=game,
        download=download,
        savesync=savesync,
        library_sync=library_sync,
        browser_manager=browser,
        graphical_ui=ActivityState("unknown", "No authoritative GUI ownership marker exists."),
    )


def _inspect_existing_lock(path: Path) -> ActivityState:
    if not path.exists():
        return ActivityState("unknown", f"No activity marker exists at {path}.")
    if path.is_symlink() or not path.is_file():
        return ActivityState("unknown", f"Activity marker is not a regular file: {path}")
    try:
        handle = path.open("r+b")
    except OSError as exc:
        return ActivityState("unknown", str(exc))
    try:
        if os.name == "nt":
            import msvcrt

            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                return ActivityState("active", f"Lock is held: {path}")
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            except OSError:
                pass
        else:
            import fcntl

            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                return ActivityState("active", f"Lock is held: {path}")
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        return ActivityState("inactive")
    finally:
        handle.close()


def _local_library_lock(config: AppConfig) -> Path | None:
    remote = config.remote_data
    if remote is None or remote.provider == "sftp":
        return None
    return Path(remote.root) / "library" / ".library-sync.lock"


def _open_sqlite_read_only(path: Path) -> sqlite3.Connection:
    # immutable=1 prevents SQLite from creating WAL/SHM sidecars. Diagnostics
    # prefer a conservative snapshot over changing the database directory.
    uri_path = quote(path.resolve(strict=False).as_posix(), safe="/:")
    conn = sqlite3.connect(f"file:{uri_path}?mode=ro&immutable=1", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def collect_diagnostics(
    config_path: str | Path,
    *,
    progress: ProgressSink = None,
    paths: TroubleshootPaths | None = None,
    activity: ActivitySnapshot | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> tuple[TroubleshootReport, DiagnosticContext | None]:
    """Run comprehensive diagnostics without intentional filesystem writes."""

    config_path = Path(config_path)
    selected_paths = paths or TroubleshootPaths()
    findings: list[TroubleshootFinding] = []

    emit_progress(progress, "troubleshoot", "configuration", "running", "Inspecting configuration")
    if not config_path.exists():
        findings.append(_finding("config.file", "configuration", "error", "ROMCloud configuration is missing.", detail=str(config_path)))
        return TroubleshootReport(tuple(findings)), None
    if config_path.is_symlink() or not config_path.is_file():
        findings.append(_finding("config.file", "configuration", "error", "ROMCloud configuration is not a regular file.", detail=str(config_path)))
        return TroubleshootReport(tuple(findings)), None
    try:
        config = load_config_read_only(str(config_path))
    except Exception as exc:  # noqa: BLE001 - one structured finding, no traceback
        findings.append(_finding("config.parse", "configuration", "error", "ROMCloud configuration could not be parsed.", detail=str(exc)))
        return TroubleshootReport(tuple(findings)), None
    findings.append(_finding("config.parse", "configuration", "healthy", "ROMCloud configuration parses successfully."))

    _inspect_credentials(config, findings)
    home = config_path.parent.parent
    catalog_path = Path(config.data_path) / "catalog.db"
    snapshot = activity or inspect_activity(config, catalog_path=catalog_path)
    context = DiagnosticContext(
        config_path=config_path,
        config=config,
        romcloud_home=home,
        paths=selected_paths,
        activity=snapshot,
        catalog_available=catalog_path.is_file(),
        managed_systems=(
            _managed_systems_read_only(catalog_path) if catalog_path.is_file() else ()
        ),
    )

    collectors: tuple[tuple[str, Callable[[DiagnosticContext, list[TroubleshootFinding]], None]], ...] = (
        ("runtime", _inspect_runtime),
        ("operating-mode", _inspect_mode),
        ("database", _inspect_database),
        ("connectivity", _inspect_connectivity),
        ("mounts", _inspect_mounts),
        ("emulationstation", _inspect_emulationstation),
        ("presentation", _inspect_presentation),
        ("ports", _inspect_ports),
        ("autosavesync", _inspect_auto_savesync),
        ("savesync", _inspect_savesync),
        ("cache", _inspect_cache),
        ("library-sync", _inspect_library_sync),
        ("browser", _inspect_browser),
    )
    was_cancelled = False
    for index, (stage, collector) in enumerate(collectors):
        if cancelled is not None and cancelled():
            was_cancelled = True
            for remaining, _ in collectors[index:]:
                findings.append(
                    _finding(
                        f"{remaining}.inspection",
                        remaining,
                        "skipped",
                        f"{remaining.replace('-', ' ').title()} inspection was cancelled before it started.",
                    )
                )
            break
        emit_progress(progress, "troubleshoot", stage, "running", f"Inspecting {stage.replace('-', ' ')}")
        try:
            collector(context, findings)
        except Exception as exc:  # noqa: BLE001 - isolate every subsystem
            findings.append(_finding(f"{stage}.inspection", stage, "error", f"{stage.replace('-', ' ').title()} inspection failed.", detail=str(exc)))
    for finding in findings:
        emit_progress(
            progress,
            "troubleshoot",
            finding.id,
            finding.status,
            finding.message,
            detail=finding.detail,
        )
    emit_progress(progress, "troubleshoot", "complete", "success", "Diagnostics complete", current=len(findings), total=len(findings))
    return TroubleshootReport(tuple(findings), cancelled=was_cancelled), context


def _inspect_credentials(config: AppConfig, findings: list[TroubleshootFinding]) -> None:
    from romcloud.infrastructure.credentials import (
        credential_lock_state,
        load_remote_data_sftp_password,
        load_remote_data_smb_password,
        load_sftp_password,
        load_smb_password,
    )

    requirements: list[tuple[str, str, Callable[[Path], str | None]]] = []
    if config.smb is not None:
        requirements.append(("smb", "ROM source SMB", load_smb_password))
    if config.source.provider == "sftp" and config.sftp is not None and not config.sftp.private_key_path:
        requirements.append(("sftp", "ROM source SFTP", load_sftp_password))
    if config.remote_data is not None and config.remote_data.provider == "smb":
        requirements.append(("remote_data_smb", "remote-data SMB", load_remote_data_smb_password))
    if config.remote_data is not None and config.remote_data.provider == "sftp" and config.remote_data.sftp is not None and not config.remote_data.sftp.private_key_path:
        requirements.append(("remote_data_sftp", "remote-data SFTP", load_remote_data_sftp_password))
    if not requirements:
        findings.append(_finding("credentials.references", "security", "healthy", "No password credential references are required."))
        return
    missing: list[str] = []
    locked: list[str] = []
    for section, label, loader in requirements:
        state = credential_lock_state(config.credentials_path, section)
        if state == "locked":
            locked.append(label)
        elif loader(config.credentials_path) is None:
            missing.append(label)
    if missing or locked:
        detail = "; ".join(filter(None, (
            f"missing: {', '.join(missing)}" if missing else "",
            f"locked: {', '.join(locked)}" if locked else "",
        )))
        findings.append(_finding("credentials.references", "security", "error", "One or more configured credentials are unavailable.", detail=detail, fixability="confirmation"))
    else:
        findings.append(_finding("credentials.references", "security", "healthy", "Configured credential references are readable."))
    if config.credentials_path.exists():
        try:
            permissions = stat.S_IMODE(config.credentials_path.stat().st_mode)
        except OSError as exc:
            findings.append(_finding("credentials.permissions", "security", "warning", "Credential-store permissions could not be inspected.", detail=str(exc)))
        else:
            private = os.name == "nt" or not bool(permissions & 0o077)
            findings.append(_finding("credentials.permissions", "security", "healthy" if private else "warning", "Credential-store permissions are restricted." if private else "Credential-store permissions allow group or other access.", detail=f"mode={permissions:o}", fixability="confirmation" if not private else "none"))


def _inspect_runtime(ctx: DiagnosticContext, findings: list[TroubleshootFinding]) -> None:
    from romcloud.lifecycle.install import _cli_wrapper_content, _launch_wrapper_content
    from romcloud.lifecycle.update import read_build_info

    home = ctx.romcloud_home
    findings.append(_finding("runtime.home", "runtime", "healthy" if home.is_dir() else "error", "ROMCloud home exists." if home.is_dir() else "ROMCloud home is missing.", detail=str(home)))
    venv_python = home / "venv" / "bin" / "python"
    python_ok = venv_python.is_file() and (os.name == "nt" or os.access(venv_python, os.X_OK))
    python_detail = str(venv_python)
    if python_ok:
        environment = os.environ.copy()
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        try:
            probe = subprocess.run(
                [str(venv_python), "-I", "-c", "import romcloud"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
                timeout=10,
                env=environment,
            )
            python_ok = probe.returncode == 0
            if not python_ok:
                python_detail = (probe.stderr or "Installed package import failed.").strip()
        except (OSError, subprocess.SubprocessError) as exc:
            python_ok = False
            python_detail = str(exc)
    findings.append(_finding("runtime.venv_python", "runtime", "healthy" if python_ok else "error", "Installed Python and package import are usable." if python_ok else "Installed Python is missing, non-executable, or cannot import ROMCloud.", detail=python_detail))
    try:
        import romcloud  # noqa: F401
    except Exception as exc:  # pragma: no cover - command could rarely reach this
        findings.append(_finding("runtime.package", "runtime", "error", "The running ROMCloud package cannot be imported.", detail=str(exc)))
    else:
        findings.append(_finding("runtime.package", "runtime", "healthy", "The running ROMCloud package imports successfully."))
    build = read_build_info(home)
    findings.append(_finding("runtime.build_metadata", "runtime", "healthy" if build else "warning", "Build metadata is readable." if build else "Build metadata is missing or malformed.", detail=str(home / "version.json")))

    wrappers = (
        ("runtime.cli_wrapper", home / "bin" / "romcloud", _cli_wrapper_content(venv_python), "ROMCloud CLI wrapper"),
        ("runtime.launch_wrapper", home / "bin" / "romcloud-run", _launch_wrapper_content(venv_python), "ROMCloud launch wrapper"),
    )
    for finding_id, path, expected, label in wrappers:
        okay = _file_matches(path, expected)
        findings.append(_finding(finding_id, "runtime", "healthy" if okay else "warning", f"{label} is current." if okay else f"{label} is missing or stale.", detail=str(path), fixability="automatic", metadata={"path": str(path)}))

    for finding_id, name, label in (
        ("runtime.graphical_wrapper", "romcloud-ports", "Graphical Ports wrapper"),
        ("runtime.launch_progress_wrapper", "romcloud-launch-progress", "Launch-progress wrapper"),
    ):
        path = home / "bin" / name
        content, _ = _safe_read(path)
        marker = "-m ports_gfx.launch_progress" if name == "romcloud-launch-progress" else "-m ports_gfx"
        okay = bool(content and marker in content and str(home / "ports-gfx") in content)
        findings.append(_finding(finding_id, "runtime", "healthy" if okay else "warning", f"{label} exists." if okay else f"{label} is missing.", detail=str(path), fixability="confirmation", blocked_by=ctx.activity.blockers("graphical_ui")))

    payload = home / "ports-gfx" / "ports_gfx"
    valid = all((payload / name).is_file() for name in ("__init__.py", "app.py", "client.py"))
    findings.append(_finding("runtime.ports_payload", "runtime", "healthy" if valid else "error", "Installed Ports GUI payload is minimally valid." if valid else "Installed Ports GUI payload is missing or incomplete.", detail=str(payload), fixability="confirmation"))


def _inspect_mode(ctx: DiagnosticContext, findings: list[TroubleshootFinding]) -> None:
    inspected = inspect_operating_mode(ctx.config)
    if inspected.state == "valid":
        findings.append(_finding("operating_mode.state", "operating-mode", "healthy", f"Operating-mode state is valid ({inspected.mode.value})."))
    else:
        findings.append(_finding("operating_mode.state", "operating-mode", "warning", f"Operating-mode state is {inspected.state}.", detail=inspected.detail, fixability="confirmation"))
    data_path = Path(ctx.config.data_path)
    data_ok = data_path.is_dir() and os.access(data_path, os.R_OK | os.W_OK)
    findings.append(_finding("data.directory", "database", "healthy" if data_ok else "error", "Configured data directory is present and writable by permission metadata." if data_ok else "Configured data directory is missing or not writable.", detail=str(data_path)))
    local_roms = Path(ctx.config.local_roms_path)
    findings.append(_finding("presentation.root", "presentation", "healthy" if local_roms.is_dir() else "error", "Local ROM presentation directory exists." if local_roms.is_dir() else "Local ROM presentation directory is missing.", detail=str(local_roms)))


def _inspect_database(ctx: DiagnosticContext, findings: list[TroubleshootFinding]) -> None:
    from romcloud.infrastructure.database import _CURRENT_SCHEMA_VERSION

    path = Path(ctx.config.data_path) / "catalog.db"
    if not path.is_file():
        findings.append(_finding("database.catalog", "database", "error", "The expected catalog database is missing.", detail=str(path)))
        findings.append(_finding("database.presentation_gate", "database", "skipped", "Presentation diagnostics were gated because the catalog is missing.", detail="No database was created."))
        return
    before = path.stat()
    try:
        with _open_sqlite_read_only(path) as conn:
            quick = str(conn.execute("PRAGMA quick_check").fetchone()[0])
            foreign = conn.execute("PRAGMA foreign_key_check").fetchmany(20)
            row = conn.execute("SELECT version FROM schema_version LIMIT 1").fetchone()
            version = int(row[0]) if row is not None else None
            tables = {str(item[0]) for item in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            required = {"games", "game_assets", "cache_entries", "proxy_records", "download_items", "cache_staging_assets", "cache_reservations"}
    except (sqlite3.Error, OSError, TypeError, ValueError) as exc:
        findings.append(_finding("database.catalog", "database", "error", "The catalog database cannot be opened read-only.", detail=str(exc)))
        return
    after = path.stat()
    findings.append(_finding("database.catalog", "database", "healthy", "The catalog database opens read-only."))
    findings.append(_finding("database.integrity", "database", "healthy" if quick == "ok" and not foreign else "error", "Catalog integrity checks passed." if quick == "ok" and not foreign else "Catalog integrity checks failed.", detail="" if quick == "ok" and not foreign else f"quick_check={quick}; foreign_key_rows={len(foreign)}"))
    schema_ok = version == _CURRENT_SCHEMA_VERSION and required.issubset(tables)
    findings.append(_finding("database.schema", "database", "healthy" if schema_ok else "warning", "Catalog schema is current." if schema_ok else "Catalog schema is missing tables or needs migration.", detail=f"found version={version!r}; expected={_CURRENT_SCHEMA_VERSION}", fixability="confirmation" if version is not None and version < _CURRENT_SCHEMA_VERSION else "none", blocked_by=ctx.activity.blockers("download", "browser_manager")))
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        findings.append(_finding("database.read_only_invariant", "database", "error", "Database metadata changed during diagnostics."))
    _inspect_download_tables(path, findings)


def _inspect_download_tables(path: Path, findings: list[TroubleshootFinding]) -> None:
    try:
        with _open_sqlite_read_only(path) as conn:
            counts = {str(row[0]): int(row[1]) for row in conn.execute("SELECT state, COUNT(*) FROM download_items GROUP BY state")}
            staging = int(conn.execute("SELECT COUNT(*) FROM cache_staging_assets").fetchone()[0])
            reservations = int(conn.execute("SELECT COUNT(*) FROM cache_reservations").fetchone()[0])
    except sqlite3.Error as exc:
        findings.append(_finding("download.state", "download", "error", "Download Manager durable state is inaccessible.", detail=str(exc)))
        return
    findings.append(_finding("download.state", "download", "healthy", "Download Manager durable tables are readable.", detail=f"states={counts}; staging={staging}; reservations={reservations}"))


def _managed_systems_read_only(path: Path) -> tuple[str, ...]:
    try:
        with _open_sqlite_read_only(path) as conn:
            return tuple(str(row[0]) for row in conn.execute("SELECT DISTINCT system FROM games WHERE is_eligible=1 ORDER BY system"))
    except sqlite3.Error:
        return ()


def _inspect_connectivity(ctx: DiagnosticContext, findings: list[TroubleshootFinding]) -> None:
    config = ctx.config
    if not config.source.enabled:
        findings.append(_finding("source.connectivity", "provider", "healthy", "Game management is disabled; no ROM source is required."))
    else:
        _inspect_provider_role(ctx, "source", config.source.provider, config.source.rom_root, findings)
    remote = config.remote_data
    if remote is None:
        findings.append(_finding("remote_data.connectivity", "provider", "healthy", "No remote-data provider is configured."))
    else:
        _inspect_provider_role(ctx, "remote_data", remote.provider, remote.root, findings)


def _inspect_provider_role(ctx: DiagnosticContext, role: str, provider_id: str, root: str, findings: list[TroubleshootFinding]) -> None:
    from romcloud.infrastructure.credentials import load_remote_data_sftp_password, load_sftp_password
    from romcloud.infrastructure.providers.local import LocalFilesystemProvider
    from romcloud.infrastructure.providers.sftp import SFTPProvider

    finding_id = f"{role}.connectivity"
    try:
        if provider_id == "sftp":
            sftp_cfg = ctx.config.sftp if role == "source" else ctx.config.remote_data.sftp
            if sftp_cfg is None:
                raise ValueError("SFTP connection settings are missing.")
            password = (load_sftp_password if role == "source" else load_remote_data_sftp_password)(ctx.config.credentials_path)
            provider = SFTPProvider(
                host=sftp_cfg.host,
                username=sftp_cfg.username,
                port=sftp_cfg.port,
                password=password,
                private_key_path=sftp_cfg.private_key_path or None,
                trusted_host_key_fingerprint=sftp_cfg.host_key_fingerprint or None,
                probe_writable=False,
            )
            result = provider.validate_access(root)
            if result.readable:
                findings.append(_finding(finding_id, "provider", "healthy", f"{role.replace('_', ' ').title()} SFTP location is readable.", detail="SFTP is intentionally read-only; no write probe was performed."))
            else:
                findings.append(_finding(finding_id, "provider", "error", f"{role.replace('_', ' ').title()} SFTP location is unavailable.", detail=result.detail))
            return
        provider = LocalFilesystemProvider()
        result = provider.validate_access(root)
        findings.append(_finding(finding_id, "provider", "healthy" if result.readable else "error", f"{role.replace('_', ' ').title()} location is readable." if result.readable else f"{role.replace('_', ' ').title()} location is unavailable.", detail=result.detail or str(root)))
    except Exception as exc:  # noqa: BLE001 - auth/trust/connectivity become findings
        findings.append(_finding(finding_id, "provider", "error", f"{role.replace('_', ' ').title()} provider validation failed.", detail=str(exc)))


def _inspect_mounts(ctx: DiagnosticContext, findings: list[TroubleshootFinding]) -> None:
    from romcloud.infrastructure import mount_worker
    from romcloud.integrations.batocera import mount_service

    targets = mount_worker.configured_mounts(ctx.config, resolve_paths=False)
    if not targets:
        findings.append(_finding("mount.integration", "mount", "healthy", "No SMB mount integration is required."))
        return
    ready = all(mount_worker._configured_mount_is_ready(target) for target in targets)
    blockers = ctx.activity.blockers("game", "download", "savesync", "library_sync")
    findings.append(_finding("mount.integration", "mount", "healthy" if ready else "warning", "Configured SMB locations are mounted." if ready else "One or more configured SMB locations are not mounted.", fixability="conditional", blocked_by=blockers))
    lock = mount_worker.lock_path(ctx.romcloud_home)
    worker_running = False
    if lock.is_file() and not lock.is_symlink():
        try:
            pid = int(lock.read_text(encoding="ascii").strip())
            worker_running = mount_worker._pid_alive(pid) and mount_worker._worker_cmdline_matches(pid, proc_root=Path("/proc"))
        except (OSError, ValueError):
            worker_running = False
    worker_needed = not ready and not worker_running
    findings.append(_finding("mount.worker", "mount", "warning" if worker_needed else "healthy", "The required ROMCloud mount worker is not running." if worker_needed else "Mount worker state is appropriate.", detail=str(lock), fixability="conditional" if worker_needed else "none", blocked_by=blockers))
    expected = mount_service.generate_service_script(str(ctx.romcloud_home / "bin" / "romcloud"))
    service_current = _file_matches(ctx.paths.mount_service, expected)
    enabled = mount_service.is_service_enabled(config_path=ctx.paths.services_config)
    findings.append(_finding("mount.service", "mount", "healthy" if service_current and enabled else "warning", "ROMCloud startup service is current and enabled." if service_current and enabled else "ROMCloud startup service is missing, stale, or disabled.", detail=f"current={service_current}; enabled={enabled}", fixability="automatic", restart=RestartRequirements(service=True)))


def _inspect_emulationstation(
    ctx: DiagnosticContext, findings: list[TroubleshootFinding]
) -> None:
    from romcloud.core.capabilities import OperatingMode
    from romcloud.integrations.batocera import es_config

    if not ctx.catalog_available:
        findings.append(
            _finding(
                "es.integration",
                "emulationstation",
                "skipped",
                "EmulationStation reconciliation was gated by the missing catalog.",
            )
        )
        return
    mode = inspect_operating_mode(ctx.config)
    if mode.state != "valid":
        findings.append(
            _finding(
                "es.integration",
                "emulationstation",
                "skipped",
                "EmulationStation state cannot be evaluated until operating mode is valid.",
                detail=mode.detail,
            )
        )
        return
    blockers = ctx.activity.blockers("game", "download", "browser_manager")
    if mode.mode is OperatingMode.CONNECTED:
        exists = ctx.paths.es_override.exists()
        findings.append(
            _finding(
                "es.integration.remove",
                "emulationstation",
                "warning" if exists else "healthy",
                "ROMCloud's ES override is stale in Direct mode."
                if exists
                else "No ROMCloud ES override is active in Direct mode.",
                fixability="conditional" if exists else "none",
                blocked_by=blockers,
                restart=RestartRequirements(emulationstation=exists),
            )
        )
        return
    if not ctx.paths.es_stock.is_file():
        findings.append(
            _finding(
                "es.integration",
                "emulationstation",
                "warning",
                "Batocera's stock EmulationStation registry is unavailable.",
                detail=str(ctx.paths.es_stock),
            )
        )
        return
    status = es_config.status(
        ctx.managed_systems,
        stock_path=ctx.paths.es_stock,
        override_path=ctx.paths.es_override,
        wrapper_path=ctx.romcloud_home / "bin" / "romcloud-run",
    )
    okay = status.wrapper_installed and status.override_exists and status.up_to_date
    findings.append(
        _finding(
            "es.integration.refresh",
            "emulationstation",
            "healthy" if okay else "warning",
            "ROMCloud's EmulationStation integration is current."
            if okay
            else "ROMCloud's EmulationStation override or owned overlay fields are stale.",
            detail=(
                f"wrapper={status.wrapper_installed}; override={status.override_exists}; "
                f"up_to_date={status.up_to_date}"
            ),
            fixability="conditional" if not okay else "none",
            blocked_by=blockers,
            restart=RestartRequirements(emulationstation=not okay),
        )
    )


def _inspect_presentation(ctx: DiagnosticContext, findings: list[TroubleshootFinding]) -> None:
    manifest = Path(ctx.config.data_path) / "direct-links.json"
    if not manifest.exists():
        findings.append(_finding("presentation.direct_manifest", "presentation", "warning", "Direct-link ownership manifest is missing.", detail=str(manifest)))
    else:
        content, error = _safe_read(manifest)
        valid = False
        if content is not None:
            try:
                payload = json.loads(content)
                valid = isinstance(payload, dict) and payload.get("version") == 1 and isinstance(payload.get("links"), list)
            except json.JSONDecodeError:
                pass
        findings.append(_finding("presentation.direct_manifest", "presentation", "healthy" if valid else "error", "Direct-link ownership manifest is valid." if valid else "Direct-link ownership manifest is malformed.", detail=error or str(manifest)))
        if valid:
            links = payload.get("links", [])
            missing_links: list[str] = []
            conflicts: list[str] = []
            for record in links:
                if not isinstance(record, dict) or not isinstance(record.get("path"), str) or not isinstance(record.get("target"), str):
                    conflicts.append("invalid manifest record")
                    continue
                path = Path(record["path"])
                if not path.exists() and not path.is_symlink():
                    missing_links.append(str(path))
                elif not path.is_symlink():
                    conflicts.append(str(path))
                else:
                    link_target = Path(os.readlink(path))
                    if not link_target.is_absolute():
                        link_target = path.parent / link_target
                    if os.path.normcase(os.path.abspath(link_target)) != os.path.normcase(
                        os.path.abspath(record["target"])
                    ):
                        conflicts.append(str(path))
            if conflicts:
                findings.append(_finding("presentation.direct_link_foreign", "presentation", "error", "One or more Direct destinations are foreign or have the wrong target.", detail="; ".join(conflicts[:10])))
            if missing_links:
                findings.append(_finding("presentation.direct_link_missing", "presentation", "warning", f"{len(missing_links)} owned Direct link(s) are missing.", detail="; ".join(missing_links[:10]), fixability="conditional", blocked_by=ctx.activity.blockers("game")))

    catalog = Path(ctx.config.data_path) / "catalog.db"
    if not ctx.catalog_available:
        return
    missing: list[tuple[str, str]] = []
    foreign: list[str] = []
    from romcloud.integrations.batocera.proxy_ownership import proxy_payload
    try:
        with _open_sqlite_read_only(catalog) as conn:
            records = tuple(conn.execute("SELECT game_id, proxy_path FROM proxy_records ORDER BY proxy_path"))
    except sqlite3.Error as exc:
        findings.append(_finding("presentation.proxies", "presentation", "error", "Proxy ownership records are inaccessible.", detail=str(exc)))
        return
    for record in records:
        path = Path(str(record[1]))
        if not path.exists() and not path.is_symlink():
            missing.append((str(record[0]), str(path)))
        elif (payload := proxy_payload(path)) is None or payload.get("game_id") != str(record[0]):
            foreign.append(str(path))
    blockers = ctx.activity.blockers("game")
    if foreign:
        findings.append(_finding("presentation.proxy_foreign", "presentation", "error", "Existing proxy destinations are malformed or not provably ROMCloud-owned.", detail="; ".join(foreign[:10])))
    if missing:
        findings.append(_finding("presentation.proxy_missing", "presentation", "warning", f"{len(missing)} owned proxy file(s) are missing.", detail="; ".join(path for _, path in missing[:10]), fixability="conditional", blocked_by=blockers, metadata={"game_ids": [game_id for game_id, _ in missing]}))
    elif not foreign:
        findings.append(_finding("presentation.proxies", "presentation", "healthy", "Owned proxy presentation is present."))


def _inspect_ports(ctx: DiagnosticContext, findings: list[TroubleshootFinding]) -> None:
    from romcloud.integrations.batocera.ports_gamelist import upsert_romcloud_entry
    from romcloud.integrations.batocera.ports_gamelist_config import ROMCLOUD_IMAGE_RELATIVE_PATH

    ports_dir = Path(ctx.config.local_roms_path) / "ports"
    owned_launcher = ports_dir / "ROMCloud.sh"
    applicable = ports_dir.is_dir() and owned_launcher.is_file()
    gamelist = ports_dir / "gamelist.xml"
    existing: str | None = None
    if gamelist.exists():
        existing, error = _safe_read(gamelist)
        if existing is None:
            findings.append(_finding("ports.gamelist", "ports", "error", "The shared Ports gamelist is unreadable.", detail=error))
            return
    try:
        desired = upsert_romcloud_entry(existing, image=ROMCLOUD_IMAGE_RELATIVE_PATH).xml
    except ET.ParseError as exc:
        findings.append(_finding("ports.gamelist", "ports", "error", "The shared Ports gamelist is malformed and was preserved.", detail=str(exc)))
    else:
        current = existing == desired
        findings.append(_finding("ports.gamelist", "ports", "healthy" if current else "warning", "The ROMCloud Ports gamelist entry is current." if current else "The ROMCloud Ports gamelist entry is missing or stale.", detail=str(gamelist), fixability="automatic" if applicable else "none"))
    source_icon = ctx.romcloud_home / "ports-gfx" / "ports_gfx" / "assets" / "icon.png"
    dest_icon = ports_dir / "images" / "ROMCloud.png"
    icon_current = source_icon.is_file() and dest_icon.is_file() and source_icon.read_bytes() == dest_icon.read_bytes()
    findings.append(_finding("ports.icon", "ports", "healthy" if icon_current else "warning", "The ROMCloud Ports icon is current." if icon_current else "The ROMCloud Ports icon is missing or stale.", detail=str(dest_icon), fixability="automatic" if source_icon.is_file() and applicable else "none"))


def _inspect_auto_savesync(ctx: DiagnosticContext, findings: list[TroubleshootFinding]) -> None:
    from romcloud.integrations.batocera.auto_savesync import hook_content

    expected = hook_content(ctx.romcloud_home / "bin" / "romcloud")
    current = _file_matches(ctx.paths.auto_savesync_hook, expected)
    findings.append(_finding("autosavesync.hook", "savesync", "healthy" if current else "warning", "Auto SaveSync lifecycle hook is current." if current else "Auto SaveSync lifecycle hook is missing or stale.", detail=str(ctx.paths.auto_savesync_hook), fixability="conditional", blocked_by=ctx.activity.blockers("game", "savesync")))
    from romcloud.integrations.batocera import auto_savesync as integration

    pid_path = integration.menu_loop_pid_path(Path(ctx.config.data_path))
    loop_running = False
    if pid_path.is_file() and not pid_path.is_symlink():
        try:
            pid = int(pid_path.read_text(encoding="ascii").strip())
            loop_running = integration._pid_alive(pid) and integration._menu_loop_cmdline_matches(pid)
        except (OSError, ValueError):
            loop_running = False
    expected = ctx.config.saves.auto_sync_enabled
    findings.append(_finding("autosavesync.loop", "savesync", "warning" if expected and not loop_running else "healthy", "Auto SaveSync resident loop is not running." if expected and not loop_running else "Auto SaveSync resident loop state is appropriate.", detail=str(pid_path), fixability="conditional" if expected and not loop_running else "none", blocked_by=ctx.activity.blockers("game", "savesync")))


def _inspect_savesync(ctx: DiagnosticContext, findings: list[TroubleshootFinding]) -> None:
    from romcloud.infrastructure.savesync_state import read_state

    path = Path(ctx.config.data_path) / "savesync-state.json"
    if not path.exists():
        findings.append(_finding("savesync.state", "savesync", "warning", "SaveSync state has not been initialized.", detail=str(path)))
        return
    before = path.stat()
    try:
        state = read_state(path)
    except Exception as exc:  # noqa: BLE001
        findings.append(_finding("savesync.state", "savesync", "error", "SaveSync state is malformed or unsupported.", detail=str(exc)))
        return
    after = path.stat()
    active_conflicts = len(state.active_conflicts)
    findings.append(_finding("savesync.state", "savesync", "warning" if active_conflicts else "healthy", "SaveSync state is readable." if not active_conflicts else f"SaveSync has {active_conflicts} unresolved conflict(s).", fixability="none"))
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        findings.append(_finding("savesync.read_only_invariant", "savesync", "error", "SaveSync state changed during diagnostics."))
    if ctx.config.remote_data is not None and ctx.config.remote_data.provider == "sftp":
        findings.append(_finding("savesync.sftp_policy", "savesync", "healthy", "SaveSync writes are unavailable by policy for read-only SFTP; Library Sync reads remain supported."))


def _inspect_cache(ctx: DiagnosticContext, findings: list[TroubleshootFinding]) -> None:
    path = Path(ctx.config.cache.path)
    if not path.exists():
        findings.append(_finding("cache.root", "cache", "warning", "Cache root is missing.", detail=str(path), fixability="confirmation"))
        return
    if not path.is_dir():
        findings.append(_finding("cache.root", "cache", "error", "Cache root is not a directory.", detail=str(path)))
        return
    try:
        usage = os.statvfs(path) if hasattr(os, "statvfs") else None
        free = usage.f_bavail * usage.f_frsize if usage else None
    except OSError as exc:
        findings.append(_finding("cache.root", "cache", "error", "Cache root cannot be inspected.", detail=str(exc)))
        return
    low = free is not None and free < ctx.config.cache.min_free_gb * 1024**3
    writable = os.access(path, os.R_OK | os.W_OK)
    status = "error" if not writable else "warning" if low else "healthy"
    findings.append(_finding("cache.root", "cache", status, "Cache root is not writable by permission metadata." if not writable else "Cache free space is below the configured reserve." if low else "Cache root is accessible.", detail=str(path)))


def _inspect_library_sync(ctx: DiagnosticContext, findings: list[TroubleshootFinding]) -> None:
    if not ctx.config.library_sync.enabled:
        findings.append(_finding("library_sync.state", "library-sync", "healthy", "Library Sync is disabled."))
        return
    canonical = Path(ctx.config.data_path) / "library" / "library.json"
    if not canonical.exists():
        findings.append(_finding("library_sync.state", "library-sync", "warning", "Local canonical library state is missing.", detail=str(canonical)))
        return
    try:
        payload = json.loads(canonical.read_text(encoding="utf-8"))
        valid = payload.get("schema_version") == 1 and isinstance(payload.get("records"), dict)
    except (OSError, UnicodeError, ValueError, AttributeError) as exc:
        findings.append(_finding("library_sync.state", "library-sync", "error", "Local canonical library state is malformed.", detail=str(exc)))
        return
    findings.append(_finding("library_sync.state", "library-sync", "healthy" if valid else "error", "Local canonical library state is readable." if valid else "Local canonical library schema is unsupported.", detail=str(canonical)))


def _inspect_browser(ctx: DiagnosticContext, findings: list[TroubleshootFinding]) -> None:
    from romcloud.web.lifecycle import local_browser_runtime_status, manager_status

    try:
        manager = manager_status(ctx.config.data_path)
        runtime = local_browser_runtime_status(ctx.config.data_path)
    except Exception as exc:  # noqa: BLE001
        findings.append(_finding("browser.manager", "browser", "warning", "Browser manager/runtime status could not be determined.", detail=str(exc)))
        return
    findings.append(_finding("browser.manager", "browser", "healthy" if manager.get("running") else "warning", "Browser manager is running." if manager.get("running") else "Browser manager is not running.", detail="Manager startup remains an explicit user action.", fixability="none"))
    findings.append(_finding("browser.runtime", "browser", "healthy" if runtime.get("available") else "warning", "A usable local browser runtime is available." if runtime.get("available") else "No usable local browser runtime was found."))


def run_quick_repair(
    config_path: str | Path,
    *,
    progress: ProgressSink = None,
    paths: TroubleshootPaths | None = None,
    activity: ActivitySnapshot | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> TroubleshootReport:
    """Diagnose, apply only whitelisted eligible fixes, then re-diagnose."""

    initial, context = collect_diagnostics(
        config_path,
        progress=progress,
        paths=paths,
        activity=activity,
        cancelled=cancelled,
    )
    if context is None:
        return replace(initial, mode="quick_repair")
    handlers = _fix_handlers(context)
    outcomes: dict[str, FindingFix] = {}
    was_cancelled = False
    for finding in initial.findings:
        if not finding.eligible_for_quick_repair or finding.id not in handlers:
            continue
        if cancelled is not None and cancelled():
            was_cancelled = True
            outcomes[finding.id] = FindingFix(False, None, False, "Cancelled before this fix started.")
            continue
        # A previous repair can also fix a later finding (the two core wrappers
        # are one example).  More importantly, activity may have started since
        # the initial diagnostic.  Re-run the pure precondition check directly
        # before every mutation and never act on a stale finding.
        current, _ = collect_diagnostics(
            config_path,
            paths=paths,
            activity=activity,
            cancelled=cancelled,
        )
        current_finding = next(
            (item for item in current.findings if item.id == finding.id), None
        )
        if current_finding is None or current_finding.status == "healthy":
            outcomes[finding.id] = FindingFix(
                False, True, False, "Already repaired by an earlier Quick Repair step."
            )
            continue
        if not current_finding.eligible_for_quick_repair:
            # Leave the current diagnostic result intact.  This includes a new
            # active-operation/uncertainty blocker detected at the boundary.
            continue
        emit_progress(progress, "troubleshoot", finding.id, "running", f"Quick Repair: {finding.message}")
        try:
            changed = bool(handlers[finding.id]())
        except Exception as exc:  # noqa: BLE001 - preserve partial results
            outcomes[finding.id] = FindingFix(True, False, False, str(exc))
            emit_progress(progress, "troubleshoot", finding.id, "error", "Quick Repair step failed", detail=str(exc))
        else:
            outcomes[finding.id] = FindingFix(True, True, changed, "")
            emit_progress(progress, "troubleshoot", finding.id, "success", "Quick Repair step completed")

    final, _ = collect_diagnostics(
        config_path,
        progress=progress,
        paths=paths,
        activity=activity,
        cancelled=cancelled,
    )
    initial_by_id = {item.id: item for item in initial.findings}
    merged: list[TroubleshootFinding] = []
    for finding in final.findings:
        outcome = outcomes.get(finding.id)
        if outcome is None:
            merged.append(finding)
            continue
        if outcome.succeeded and finding.status == "healthy":
            old = initial_by_id.get(finding.id, finding)
            merged.append(replace(finding, status="fixed", fixability=old.fixability, fix=outcome, restart_required=old.restart_required))
        elif outcome.succeeded:
            merged.append(replace(finding, fix=FindingFix(True, False, outcome.changed, "Post-fix diagnostic remains unhealthy.")))
        else:
            merged.append(replace(finding, status="error", severity="error", fix=outcome))
    for finding_id, outcome in outcomes.items():
        if finding_id not in {item.id for item in final.findings}:
            old = initial_by_id[finding_id]
            merged.append(replace(old, status="fixed" if outcome.succeeded else "error", severity="info" if outcome.succeeded else "error", fix=outcome))
    return TroubleshootReport(tuple(merged), mode="quick_repair", cancelled=was_cancelled)


def _fix_handlers(ctx: DiagnosticContext) -> dict[str, Callable[[], bool]]:
    from romcloud.integrations.batocera import auto_savesync, mount_service, ports_gamelist_config
    from romcloud.lifecycle.install import write_core_wrappers

    home = ctx.romcloud_home
    ports_dir = Path(ctx.config.local_roms_path) / "ports"

    def wrappers() -> bool:
        write_core_wrappers(home / "bin", home / "venv" / "bin" / "python")
        return True

    def hook() -> bool:
        before = ctx.paths.auto_savesync_hook.read_bytes() if ctx.paths.auto_savesync_hook.is_file() else None
        auto_savesync.install_hook(home / "bin" / "romcloud", hook_path=ctx.paths.auto_savesync_hook)
        return before != ctx.paths.auto_savesync_hook.read_bytes()

    def service() -> bool:
        before = ctx.paths.mount_service.read_bytes() if ctx.paths.mount_service.is_file() else None
        mount_service.install_service(
            str(home / "bin" / "romcloud"),
            service_path=ctx.paths.mount_service,
            services_config_path=ctx.paths.services_config,
        )
        return before != ctx.paths.mount_service.read_bytes()

    def gamelist() -> bool:
        return ports_gamelist_config.reconcile(gamelist_path=ports_dir / "gamelist.xml")

    def icon() -> bool:
        return ports_gamelist_config.sync_icon(
            source_icon=home / "ports-gfx" / "ports_gfx" / "assets" / "icon.png",
            ports_dir=ports_dir,
        )

    def proxies() -> bool:
        from romcloud.lifecycle.manage import restore_owned_proxies

        finding = next(item for item in collect_diagnostics(ctx.config_path, paths=ctx.paths, activity=ctx.activity)[0].findings if item.id == "presentation.proxy_missing")
        game_ids = {str(value) for value in finding.metadata.get("game_ids", [])}
        return restore_owned_proxies(ctx.config, game_ids=game_ids) > 0

    def direct_links() -> bool:
        from romcloud.integrations.batocera.game_access import reconcile_direct_links

        report = reconcile_direct_links(ctx.config)
        return bool(report.restored or report.removed)

    def es_refresh() -> bool:
        from romcloud.integrations.batocera import es_config

        es_config.refresh(
            ctx.managed_systems,
            stock_path=ctx.paths.es_stock,
            override_path=ctx.paths.es_override,
            wrapper_path=ctx.romcloud_home / "bin" / "romcloud-run",
        )
        return True

    def es_remove() -> bool:
        from romcloud.integrations.batocera import es_config

        return es_config.remove(override_path=ctx.paths.es_override)

    def mount_missing() -> bool:
        from romcloud.services.connections import mount_connections

        return bool(mount_connections(ctx.config).get("changed"))

    def mount_worker_start() -> bool:
        from romcloud.infrastructure import mount_worker

        mount_worker.spawn_worker(ctx.romcloud_home)
        return True

    def autosync_loop() -> bool:
        from romcloud.integrations.batocera import auto_savesync

        auto_savesync.spawn_menu_loop(Path(ctx.config.data_path))
        return True

    return {
        "runtime.cli_wrapper": wrappers,
        "runtime.launch_wrapper": wrappers,
        "autosavesync.hook": hook,
        "mount.service": service,
        "ports.gamelist": gamelist,
        "ports.icon": icon,
        "presentation.proxy_missing": proxies,
        "presentation.direct_link_missing": direct_links,
        "es.integration.refresh": es_refresh,
        "es.integration.remove": es_remove,
        "mount.integration": mount_missing,
        "mount.worker": mount_worker_start,
        "autosavesync.loop": autosync_loop,
    }
