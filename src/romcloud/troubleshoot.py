"""Read-only diagnostics and an explicit, narrow Quick Repair registry."""

from __future__ import annotations

import json
import os
import sqlite3
import stat
import subprocess
import signal
import tempfile
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
from romcloud.infrastructure.library_view import (
    OperatingModeInspection,
    inspect_operating_mode,
)
from romcloud.integrations.batocera.system_registry import EffectiveSystemRegistry


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
    es_user_config_dir: Path = Path("/userdata/system/configs/emulationstation")
    es_system_config_dir: Path = Path("/usr/share/emulationstation")
    es_legacy_config_dir: Path = Path("/etc/emulationstation")


@dataclass(frozen=True)
class CatalogInspection:
    """Read-only catalog authorization state for catalog-derived repairs."""

    state: str  # missing, unreadable, corrupt, incompatible, integrity_failed, trusted
    detail: str = ""
    version: int | None = None
    tables: frozenset[str] = frozenset()
    managed_systems: tuple[str, ...] = ()
    changed_during_check: bool = False

    @property
    def trusted(self) -> bool:
        return self.state == "trusted"


@dataclass(frozen=True)
class DiagnosticContext:
    config_path: Path
    config: AppConfig
    romcloud_home: Path
    paths: TroubleshootPaths
    activity: ActivitySnapshot
    mode: OperatingModeInspection
    catalog: CatalogInspection
    system_registry: EffectiveSystemRegistry | None = None
    system_registry_error: str = ""
    secrets: tuple[str, ...] = ()

    @property
    def catalog_available(self) -> bool:
        """Compatibility spelling; availability now means explicitly trusted."""

        return self.catalog.trusted

    @property
    def managed_systems(self) -> tuple[str, ...]:
        return self.catalog.managed_systems


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


def _sanitize_text(value: object, secrets: Iterable[str] = ()) -> str:
    """Apply structured redaction plus exact replacement of loaded secrets."""

    from romcloud.core.progress import redact_text
    from romcloud.infrastructure.diagnostics import redact_metadata

    exact = redact_text(str(value), *tuple(secrets))
    return str(redact_metadata({"detail": exact}).get("detail", ""))


def _sanitize_findings(
    findings: Iterable[TroubleshootFinding], secrets: Iterable[str]
) -> list[TroubleshootFinding]:
    return [
        replace(
            finding,
            message=_sanitize_text(finding.message, secrets),
            detail=_sanitize_text(finding.detail, secrets),
        )
        for finding in findings
    ]


def inspect_activity(config: AppConfig, *, catalog_path: Path) -> ActivitySnapshot:
    """Best-effort snapshot which never creates, removes, or locks a path."""

    from romcloud.services.auto_savesync import ActiveSessionStore
    from romcloud.web.lifecycle import manager_state_path, manager_status

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
                running = int(conn.execute(
                    "SELECT COUNT(*) FROM download_items WHERE state IN ('running','verifying')"
                ).fetchone()[0])
                reservations = int(
                    conn.execute("SELECT COUNT(*) FROM cache_reservations").fetchone()[0]
                )
            download = ActivityState(
                "active" if running or reservations else "inactive",
                f"running={running}; reservations={reservations}",
            )
        except sqlite3.Error as exc:
            download = ActivityState("unknown", str(exc))

    savesync = _inspect_existing_lock(Path(config.data_path) / ".savesync-auto.lock")
    library_lock = _local_library_lock(config)
    library_sync = _inspect_existing_lock(library_lock) if library_lock else ActivityState(
        "unknown", "The Library Sync lock is remote or not configured."
    )

    manager_marker = manager_state_path(config.data_path)
    if not manager_marker.is_file() or manager_marker.is_symlink():
        browser = ActivityState(
            "unknown", "No authoritative browser-manager marker exists."
        )
    else:
        try:
            manager = manager_status(config.data_path)
            browser = ActivityState(
                "active" if manager.get("running") else "inactive",
                "Owned manager endpoint is reachable."
                if manager.get("running")
                else "Owned manager marker is present but not active.",
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
    # ``immutable=1`` is deliberately not used: it ignores committed rows that
    # are still resident in an existing WAL.  URI read-only mode plus
    # ``query_only`` sees the live committed snapshot without authorizing any
    # database-content mutation. SQLite may consult existing WAL/SHM sidecars.
    uri_path = quote(path.resolve(strict=False).as_posix(), safe="/:")
    conn = sqlite3.connect(f"file:{uri_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    return conn


def _inspect_catalog(path: Path) -> CatalogInspection:
    """Establish whether catalog contents are safe to use as repair authority."""

    from romcloud.infrastructure.database import _CURRENT_SCHEMA_VERSION

    required = frozenset(
        {
            "games",
            "game_assets",
            "cache_entries",
            "proxy_records",
            "download_items",
            "cache_staging_assets",
            "cache_reservations",
        }
    )
    if not path.is_file():
        return CatalogInspection("missing", detail=str(path))
    try:
        before = path.stat()
        with _open_sqlite_read_only(path) as conn:
            quick = str(conn.execute("PRAGMA quick_check").fetchone()[0])
            if quick != "ok":
                return CatalogInspection("integrity_failed", detail=quick)
            foreign = conn.execute("PRAGMA foreign_key_check").fetchmany(20)
            if foreign:
                return CatalogInspection(
                    "integrity_failed",
                    detail=f"foreign_key_rows={len(foreign)}",
                )
            tables = frozenset(
                str(row[0])
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            )
            if "schema_version" not in tables:
                return CatalogInspection(
                    "incompatible", detail="schema_version table is missing", tables=tables
                )
            row = conn.execute("SELECT version FROM schema_version LIMIT 1").fetchone()
            version = int(row[0]) if row is not None else None
            if version != _CURRENT_SCHEMA_VERSION or not required.issubset(tables):
                return CatalogInspection(
                    "incompatible",
                    detail=(
                        f"found version={version!r}; expected={_CURRENT_SCHEMA_VERSION}; "
                        f"missing tables={sorted(required - tables)}"
                    ),
                    version=version,
                    tables=tables,
                )
            systems = tuple(
                str(row[0])
                for row in conn.execute(
                    "SELECT DISTINCT system FROM games "
                    "WHERE is_eligible=1 ORDER BY system"
                )
            )
        after = path.stat()
    except (sqlite3.DatabaseError, OSError, TypeError, ValueError) as exc:
        detail = str(exc)
        corrupt = isinstance(exc, sqlite3.DatabaseError) and any(
            marker in detail.casefold()
            for marker in ("malformed", "not a database", "file is encrypted")
        )
        return CatalogInspection("corrupt" if corrupt else "unreadable", detail=detail)
    changed = (before.st_size, before.st_mtime_ns) != (
        after.st_size,
        after.st_mtime_ns,
    )
    return CatalogInspection(
        "trusted",
        version=version,
        tables=tables,
        managed_systems=systems,
        changed_during_check=changed,
    )


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

    secrets = _inspect_credentials(config, findings)
    home = config_path.parent.parent
    catalog_path = Path(config.data_path) / "catalog.db"
    catalog = _inspect_catalog(catalog_path)
    mode = inspect_operating_mode(config)
    snapshot = activity or inspect_activity(config, catalog_path=catalog_path)
    registry = None
    registry_error = ""
    if catalog.trusted and mode.state == "valid":
        from romcloud.integrations.batocera.system_registry import (
            inspect_live_system_registry,
        )

        try:
            registry = inspect_live_system_registry(
                user_config_dir=selected_paths.es_user_config_dir,
                system_config_dir=selected_paths.es_system_config_dir,
                legacy_config_dir=selected_paths.es_legacy_config_dir,
            )
        except Exception as exc:  # noqa: BLE001 - uncertainty is an ES finding
            registry_error = _sanitize_text(exc, secrets)
    context = DiagnosticContext(
        config_path=config_path,
        config=config,
        romcloud_home=home,
        paths=selected_paths,
        activity=snapshot,
        mode=mode,
        catalog=catalog,
        system_registry=registry,
        system_registry_error=registry_error,
        secrets=secrets,
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
            findings.append(_finding(f"{stage}.inspection", stage, "error", f"{stage.replace('-', ' ').title()} inspection failed.", detail=_sanitize_text(exc, secrets)))
    findings = _sanitize_findings(findings, secrets)
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


def _inspect_credentials(
    config: AppConfig, findings: list[TroubleshootFinding]
) -> tuple[str, ...]:
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
        return ()
    missing: list[str] = []
    locked: list[str] = []
    secrets: list[str] = []
    for section, label, loader in requirements:
        state = credential_lock_state(config.credentials_path, section)
        if state == "locked":
            locked.append(label)
        else:
            value = loader(config.credentials_path)
            if value is None:
                missing.append(label)
            else:
                secrets.append(value)
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
    return tuple(secrets)


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
        findings.append(_finding(finding_id, "runtime", "healthy" if okay else "warning", f"{label} is current." if okay else f"{label} is missing or stale.", detail=str(path), fixability="automatic" if python_ok else "confirmation", blocked_by=() if python_ok else ("runtime:broken",), metadata={"path": str(path)}))

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
    inspected = ctx.mode
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
    path = Path(ctx.config.data_path) / "catalog.db"
    catalog = ctx.catalog
    if catalog.state == "missing":
        findings.append(_finding("database.catalog", "database", "error", "The expected catalog database is missing.", detail=str(path)))
        findings.append(_finding("database.presentation_gate", "database", "skipped", "Presentation diagnostics were gated because the catalog is missing.", detail="No database was created."))
        return
    if not catalog.trusted:
        messages = {
            "unreadable": "The catalog database cannot be opened read-only.",
            "corrupt": "The catalog database is corrupt.",
            "incompatible": "The catalog schema is incompatible with this ROMCloud build.",
            "integrity_failed": "Catalog integrity checks failed.",
        }
        findings.append(
            _finding(
                "database.catalog",
                "database",
                "error",
                messages.get(catalog.state, "The catalog database is not trusted."),
                detail=catalog.detail,
            )
        )
        findings.append(
            _finding(
                "database.presentation_gate",
                "database",
                "skipped",
                "Catalog-derived diagnostics and repairs were gated.",
                detail=f"catalog trust state={catalog.state}",
            )
        )
        if catalog.state == "incompatible":
            findings.append(
                _finding(
                    "database.schema",
                    "database",
                    "warning",
                    "Catalog schema migration or a compatible ROMCloud build is required.",
                    detail=catalog.detail,
                    fixability="confirmation",
                    blocked_by=ctx.activity.blockers("download", "browser_manager"),
                )
            )
        return
    findings.append(_finding("database.catalog", "database", "healthy", "The catalog database opens read-only."))
    findings.append(_finding("database.integrity", "database", "healthy", "Catalog integrity checks passed."))
    findings.append(_finding("database.schema", "database", "healthy", "Catalog schema is current.", detail=f"version={catalog.version}"))
    if catalog.changed_during_check:
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
        if provider_id == "google_drive":
            findings.append(
                _finding(
                    finding_id,
                    "provider",
                    "healthy",
                    f"{role.replace('_', ' ').title()} Google Drive support is not active in this beta.",
                    detail="No local-path or writable capability was assumed.",
                )
            )
            return
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
                kind = _provider_failure_kind(result.detail)
                findings.append(_finding(finding_id, "provider", "error", f"{role.replace('_', ' ').title()} SFTP {kind} failure.", detail=result.detail, metadata={"failure_kind": kind}))
            return
        provider = LocalFilesystemProvider()
        result = provider.validate_access(root)
        unavailable = (
            f"{role.replace('_', ' ').title()} SMB path is configured but not mounted or readable."
            if provider_id == "smb"
            else f"{role.replace('_', ' ').title()} local path is unavailable."
        )
        findings.append(_finding(finding_id, "provider", "healthy" if result.readable else "error", f"{role.replace('_', ' ').title()} location is readable." if result.readable else unavailable, detail=result.detail or str(root)))
    except Exception as exc:  # noqa: BLE001 - auth/trust/connectivity become findings
        detail = _sanitize_text(exc, ctx.secrets)
        kind = _provider_failure_kind(detail)
        findings.append(_finding(finding_id, "provider", "error", f"{role.replace('_', ' ').title()} provider {kind} failure.", detail=detail, metadata={"failure_kind": kind}))


def _provider_failure_kind(detail: str) -> str:
    text = str(detail).casefold()
    if "host key" in text and any(word in text for word in ("mismatch", "changed", "does not match")):
        return "host-key-mismatch"
    if "host key" in text or "fingerprint" in text:
        return "host-key-unknown"
    if any(word in text for word in ("authentication", "auth failed", "permission denied (publickey")):
        return "authentication"
    if any(word in text for word in ("timed out", "timeout", "unreachable", "refused", "resolve")):
        return "unreachable"
    if any(word in text for word in ("does not exist", "not found", "missing root")):
        return "missing-root"
    if any(word in text for word in ("permission", "read access denied")):
        return "read-permission"
    return "connectivity"


def _inspect_mounts(ctx: DiagnosticContext, findings: list[TroubleshootFinding]) -> None:
    from romcloud.infrastructure import mount_worker
    from romcloud.integrations.batocera import mount_service

    targets = mount_worker.configured_mounts(ctx.config, resolve_paths=False)
    if not targets:
        findings.append(_finding("mount.integration", "mount", "healthy", "No SMB mount integration is required."))
    else:
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

    # The owned boot service also starts the Auto SaveSync resident loop and
    # Library Manager, so its health is independent of SMB configuration.
    expected = mount_service.generate_service_script(str(ctx.romcloud_home / "bin" / "romcloud"))
    service_current = _file_matches(ctx.paths.mount_service, expected)
    enabled = mount_service.is_service_enabled(config_path=ctx.paths.services_config)
    findings.append(_finding("mount.service", "mount", "healthy" if service_current and enabled else "warning", "ROMCloud startup service is current and enabled." if service_current and enabled else "ROMCloud startup service is missing, stale, or disabled.", detail=f"current={service_current}; enabled={enabled}", fixability="automatic", restart=RestartRequirements(service=True)))


def _inspect_emulationstation(
    ctx: DiagnosticContext, findings: list[TroubleshootFinding]
) -> None:
    from romcloud.core.capabilities import OperatingMode
    from romcloud.integrations.batocera import es_config

    if not ctx.catalog.trusted:
        findings.append(
            _finding(
                "es.integration",
                "emulationstation",
                "skipped",
                "EmulationStation reconciliation was gated by an untrusted catalog.",
                detail=f"catalog trust state={ctx.catalog.state}",
            )
        )
        return
    mode = ctx.mode
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
    if ctx.system_registry is None:
        findings.append(
            _finding(
                "es.integration",
                "emulationstation",
                "skipped",
                "The effective live EmulationStation registry is not trustworthy.",
                detail=ctx.system_registry_error,
            )
        )
        return
    status = es_config.status(
        ctx.managed_systems,
        stock_path=ctx.paths.es_stock,
        override_path=ctx.paths.es_override,
        wrapper_path=ctx.romcloud_home / "bin" / "romcloud-run",
        system_registry=ctx.system_registry,
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
    from romcloud.core.capabilities import OperatingMode

    if ctx.mode.state != "valid" or ctx.mode.mode is None:
        findings.append(
            _finding(
                "presentation.mode_gate",
                "presentation",
                "skipped",
                "Presentation health cannot be evaluated until operating mode is valid.",
                detail=ctx.mode.detail,
            )
        )
        return
    if not ctx.catalog.trusted:
        findings.append(
            _finding(
                "presentation.catalog_gate",
                "presentation",
                "skipped",
                "Presentation health cannot authorize repairs from an untrusted catalog.",
                detail=f"catalog trust state={ctx.catalog.state}",
            )
        )
        return

    if ctx.mode.mode is OperatingMode.CONNECTED:
        _inspect_direct_presentation(ctx, findings)
        return
    _inspect_proxy_presentation(
        ctx,
        findings,
        offline=ctx.mode.mode is OperatingMode.OFFLINE,
    )


def _inspect_direct_presentation(
    ctx: DiagnosticContext, findings: list[TroubleshootFinding]
) -> None:
    from romcloud.integrations.batocera.proxy_ownership import is_within

    manifest = Path(ctx.config.data_path) / "direct-links.json"
    if not manifest.exists():
        findings.append(_finding("presentation.direct_manifest", "presentation", "warning", "Direct-link ownership manifest is missing.", detail=str(manifest)))
    else:
        content, error = _safe_read(manifest)
        valid = False
        if content is not None:
            try:
                payload = json.loads(content)
                links_value = payload.get("links") if isinstance(payload, dict) else None
                valid = (
                    isinstance(payload, dict)
                    and payload.get("version") == 1
                    and isinstance(links_value, list)
                    and all(
                        isinstance(record, dict)
                        and isinstance(record.get("path"), str)
                        and isinstance(record.get("target"), str)
                        for record in links_value
                    )
                )
            except json.JSONDecodeError:
                pass
        findings.append(_finding("presentation.direct_manifest", "presentation", "healthy" if valid else "error", "Direct-link ownership manifest is valid." if valid else "Direct-link ownership manifest is malformed.", detail=error or str(manifest)))
        if valid:
            links = payload.get("links", [])
            missing_links: list[dict[str, str]] = []
            conflicts: list[str] = []
            unauthorized: list[str] = []
            unavailable: list[str] = []
            expected = _expected_direct_pairs(ctx)
            local_root = Path(ctx.config.local_roms_path)
            source_root = Path(ctx.config.source.rom_root)
            for record in links:
                path = Path(record["path"])
                target = Path(record["target"])
                pair = (_path_key(path), _path_key(target))
                if pair not in expected:
                    unauthorized.append(str(path))
                    continue
                system_dir = path.parent
                authorized = (
                    system_dir.is_dir()
                    and not system_dir.is_symlink()
                    and target.is_dir()
                    and not target.is_symlink()
                    and is_within(path, local_root)
                    and is_within(target, source_root)
                )
                if not path.exists() and not path.is_symlink() and authorized:
                    missing_links.append(
                        {"path": str(path), "target": str(record["target"])}
                    )
                elif not path.exists() and not path.is_symlink():
                    unavailable.append(str(path))
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
            if unauthorized:
                findings.append(_finding("presentation.direct_link_unauthorized", "presentation", "warning", "One or more Direct manifest records are stale or outside the currently managed path set.", detail="; ".join(unauthorized[:10])))
            if unavailable:
                findings.append(_finding("presentation.direct_link_unavailable", "presentation", "warning", "One or more owned Direct links cannot be restored because their user-owned system directory or source directory is unavailable.", detail="; ".join(unavailable[:10])))
            if missing_links:
                findings.append(_finding("presentation.direct_link_missing", "presentation", "warning", f"{len(missing_links)} owned Direct link(s) are missing.", detail="; ".join(item["path"] for item in missing_links[:10]), fixability="conditional", blocked_by=ctx.activity.blockers("game"), metadata={"links": missing_links}))


def _path_key(path: Path) -> str:
    return os.path.normcase(os.path.abspath(path))


def _expected_direct_pairs(ctx: DiagnosticContext) -> set[tuple[str, str]]:
    local_root = Path(ctx.config.local_roms_path)
    source_root = Path(ctx.config.source.rom_root)
    return {
        (
            _path_key(local_root / system / "ROMCloud"),
            _path_key(source_root / system),
        )
        for system in ctx.managed_systems
    }


def _cached_member_size(path: Path) -> int:
    if path.is_file():
        return path.stat().st_size
    return sum(item.stat().st_size for item in path.rglob("*") if item.is_file())


def _valid_cached_game_ids_read_only(
    ctx: DiagnosticContext, conn: sqlite3.Connection
) -> set[str]:
    """Mirror CacheService's playable-cache rule without repositories/writes."""

    from romcloud.core.cache_paths import resolve_cache_path
    from romcloud.core.exceptions import CacheError

    selected = ctx.config.source.selected_systems
    rows = tuple(
        conn.execute(
            "SELECT c.game_id, c.cache_path, g.system "
            "FROM cache_entries c JOIN games g ON g.id = c.game_id "
            "WHERE c.status = 'complete' AND c.membership_resolved = 1 "
            "AND g.is_eligible = 1 ORDER BY c.game_id"
        )
    )
    valid: set[str] = set()
    cache_root = Path(ctx.config.cache.path)
    for row in rows:
        game_id = str(row[0])
        cache_path = Path(str(row[1]))
        system = str(row[2])
        if selected is not None and system not in selected:
            continue
        members = tuple(
            conn.execute(
                "SELECT relative_path, expected_size, is_primary "
                "FROM cache_members WHERE game_id = ? "
                "ORDER BY is_primary DESC, relative_path",
                (game_id,),
            )
        )
        if not members or not any(bool(member[2]) for member in members):
            continue
        playable = True
        for member in members:
            relative_path = str(member[0])
            try:
                direct = resolve_cache_path(cache_root, system, relative_path)
            except CacheError:
                playable = False
                break
            member_path = direct
            if not direct.exists() and cache_path.is_dir():
                nested = cache_path / Path(relative_path).name
                if nested.exists():
                    member_path = nested
            if not member_path.exists() or member_path.is_symlink():
                playable = False
                break
            expected_size = member[1]
            if expected_size is not None:
                try:
                    actual_size = _cached_member_size(member_path)
                    expected_size = int(expected_size)
                except (OSError, TypeError, ValueError):
                    playable = False
                    break
                if actual_size != expected_size:
                    playable = False
                    break
        if playable:
            valid.add(game_id)
    return valid


def _inspect_proxy_presentation(
    ctx: DiagnosticContext,
    findings: list[TroubleshootFinding],
    *,
    offline: bool,
) -> None:
    catalog = Path(ctx.config.data_path) / "catalog.db"
    missing: list[tuple[str, str]] = []
    foreign: list[str] = []
    from romcloud.integrations.batocera.proxy_ownership import proxy_payload
    try:
        with _open_sqlite_read_only(catalog) as conn:
            playable_ids = (
                _valid_cached_game_ids_read_only(ctx, conn) if offline else None
            )
            all_records = tuple(
                conn.execute(
                    "SELECT p.game_id, p.proxy_path FROM proxy_records p "
                    "JOIN games g ON g.id = p.game_id "
                    "WHERE g.is_eligible = 1 "
                    "ORDER BY p.proxy_path"
                )
            )
            records = tuple(
                record
                for record in all_records
                if playable_ids is None or str(record[0]) in playable_ids
            )
            if playable_ids is None:
                unregistered = int(
                    conn.execute(
                        "SELECT COUNT(*) FROM games g "
                        "LEFT JOIN proxy_records p ON p.game_id = g.id "
                        "WHERE g.is_eligible = 1 AND p.game_id IS NULL"
                    ).fetchone()[0]
                )
            else:
                registered = {str(record[0]) for record in all_records}
                unregistered = len(playable_ids - registered)
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
        label = "offline-playable" if offline else "selected"
        findings.append(_finding("presentation.proxies", "presentation", "healthy", f"Owned proxy presentation is present for {label} games."))
    if unregistered:
        findings.append(
            _finding(
                "presentation.proxy_unregistered",
                "presentation",
                "warning",
                f"{unregistered} expected game(s) have no validated proxy ownership record.",
                detail="Quick Repair will not invent ownership destinations.",
            )
        )


def _inspect_ports(ctx: DiagnosticContext, findings: list[TroubleshootFinding]) -> None:
    from romcloud.integrations.batocera.ports_gamelist import upsert_romcloud_entry
    from romcloud.integrations.batocera.ports_gamelist_config import ROMCLOUD_IMAGE_RELATIVE_PATH

    ports_dir = Path(ctx.config.local_roms_path) / "ports"
    owned_launcher = ports_dir / "ROMCloud.sh"
    applicable = ports_dir.is_dir() and owned_launcher.is_file()
    launcher_content, launcher_error = _safe_read(owned_launcher)
    graphical_wrapper = ctx.romcloud_home / "bin" / "romcloud-ports"
    launcher_current = bool(
        launcher_content
        and 'exec "' in launcher_content
        and str(graphical_wrapper) in launcher_content
    )
    findings.append(
        _finding(
            "ports.launcher",
            "ports",
            "healthy" if launcher_current else "warning",
            "The ROMCloud Ports launcher targets the installed graphical wrapper."
            if launcher_current
            else "The ROMCloud Ports launcher is missing or stale.",
            detail=launcher_error or str(owned_launcher),
            fixability="confirmation",
        )
    )
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
        findings.append(_finding("savesync.state", "savesync", "healthy", "SaveSync state has not been initialized; no sync history exists yet.", detail=str(path)))
        if ctx.config.remote_data is not None and ctx.config.remote_data.provider == "sftp":
            findings.append(_finding("savesync.sftp_policy", "savesync", "healthy", "SaveSync writes are unavailable by policy for read-only SFTP; Library Sync reads remain supported."))
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
    findings.append(_finding("browser.manager", "browser", "healthy", "Browser manager is running." if manager.get("running") else "Browser manager is idle (not required outside an active browser session).", fixability="none"))
    findings.append(_finding("browser.runtime", "browser", "healthy", "A usable local browser runtime is available." if runtime.get("available") else "No local browser runtime is installed; browser views remain optional."))


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
        if current.cancelled:
            was_cancelled = True
            outcomes[finding.id] = FindingFix(
                False, None, False, "Cancelled before this fix started."
            )
            continue
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
            detail = _sanitize_text(exc, context.secrets)
            outcomes[finding.id] = FindingFix(True, False, False, detail)
            emit_progress(progress, "troubleshoot", finding.id, "error", "Quick Repair step failed", detail=detail)
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
        if not outcome.attempted and outcome.succeeded is None:
            merged.append(
                replace(
                    finding,
                    status="skipped",
                    severity="warning",
                    fix=outcome,
                )
            )
            continue
        if outcome.succeeded and finding.status == "healthy":
            old = initial_by_id.get(finding.id, finding)
            merged.append(
                replace(
                    finding,
                    status=(
                        "fixed"
                        if outcome.changed or not outcome.attempted
                        else "healthy"
                    ),
                    fixability=old.fixability,
                    fix=outcome,
                    restart_required=old.restart_required,
                )
            )
        elif outcome.succeeded:
            merged.append(replace(finding, fix=FindingFix(True, False, outcome.changed, "Post-fix diagnostic remains unhealthy.")))
        else:
            merged.append(replace(finding, status="error", severity="error", fix=outcome))
    final_ids = {item.id for item in final.findings}
    for finding_id, old in initial_by_id.items():
        if finding_id not in final_ids:
            outcome = outcomes.get(finding_id)
            if outcome is None:
                merged.append(old)
                continue
            if not outcome.attempted and outcome.succeeded is None:
                merged.append(
                    replace(old, status="skipped", severity="warning", fix=outcome)
                )
            elif outcome.succeeded and not outcome.changed:
                merged.append(
                    replace(old, status="healthy", severity="info", fix=outcome)
                )
            else:
                merged.append(replace(old, status="fixed" if outcome.succeeded else "error", severity="info" if outcome.succeeded else "error", fix=outcome))
    return TroubleshootReport(tuple(merged), mode="quick_repair", cancelled=was_cancelled)


def _atomic_create_text_no_replace(path: Path, content: str) -> bool:
    """Atomically materialize one absent owned file without replacing a race."""

    path.parent.mkdir(parents=True, exist_ok=True)
    if path.parent.is_symlink():
        return False
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            return False
        return True
    finally:
        temporary.unlink(missing_ok=True)


def _restore_diagnosed_proxies(
    ctx: DiagnosticContext, game_ids: set[str]
) -> bool:
    """Restore only missing, registered proxies from a trusted catalog."""

    from romcloud.core.capabilities import OperatingMode
    from romcloud.integrations.batocera.proxy_ownership import is_within

    catalog_path = Path(ctx.config.data_path) / "catalog.db"
    catalog = _inspect_catalog(catalog_path)
    mode = inspect_operating_mode(ctx.config)
    if not catalog.trusted or mode.state != "valid" or mode.mode not in {
        OperatingMode.CACHE,
        OperatingMode.OFFLINE,
    }:
        raise RuntimeError(
            "Proxy repair authority changed after diagnostics; no proxy was written."
        )
    offline = mode.mode is OperatingMode.OFFLINE
    changed = False
    local_root = Path(ctx.config.local_roms_path)
    with _open_sqlite_read_only(catalog_path) as conn:
        playable_ids = (
            _valid_cached_game_ids_read_only(ctx, conn) if offline else None
        )
        for game_id in sorted(game_ids):
            row = conn.execute(
                "SELECT p.proxy_path, g.title, g.system, g.source_provider, "
                "g.source_root, g.is_eligible FROM proxy_records p "
                "JOIN games g ON g.id = p.game_id WHERE p.game_id = ?",
                (game_id,),
            ).fetchone()
            if row is None or not bool(row[5]):
                continue
            if playable_ids is not None and game_id not in playable_ids:
                continue
            path = Path(str(row[0]))
            if path.exists() or path.is_symlink() or not is_within(path, local_root):
                continue
            assets = [
                {
                    "filename": str(asset[0]),
                    "relative_path": str(asset[1]),
                    "is_primary": bool(asset[2]),
                }
                for asset in conn.execute(
                    "SELECT filename, relative_path, is_primary FROM game_assets "
                    "WHERE game_id = ? ORDER BY id",
                    (game_id,),
                )
            ]
            payload = {
                "romcloud_version": "1",
                "game_id": game_id,
                "title": str(row[1]),
                "system": str(row[2]),
                "source_provider": str(row[3]),
                "source_root": str(row[4]),
                "assets": assets,
            }
            changed = (
                _atomic_create_text_no_replace(
                    path, json.dumps(payload, indent=2, ensure_ascii=False)
                )
                or changed
            )
    return changed


def _restore_diagnosed_direct_links(
    ctx: DiagnosticContext, records: Iterable[Mapping[str, object]]
) -> bool:
    """Restore only absent links named by a valid Direct ownership manifest."""

    from romcloud.core.capabilities import OperatingMode
    from romcloud.integrations.batocera.proxy_ownership import is_within

    catalog = _inspect_catalog(Path(ctx.config.data_path) / "catalog.db")
    mode = inspect_operating_mode(ctx.config)
    manifest = Path(ctx.config.data_path) / "direct-links.json"
    try:
        payload = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError):
        return False
    if (
        not catalog.trusted
        or mode.state != "valid"
        or mode.mode is not OperatingMode.CONNECTED
        or not isinstance(payload, dict)
        or payload.get("version") != 1
        or not isinstance(payload.get("links"), list)
        or not all(
            isinstance(item, dict)
            and isinstance(item.get("path"), str)
            and isinstance(item.get("target"), str)
            for item in payload.get("links", [])
        )
    ):
        raise RuntimeError(
            "Direct-link repair authority changed after diagnostics; no link was written."
        )
    manifest_records = {
        (str(item.get("path")), str(item.get("target")))
        for item in payload["links"]
        if isinstance(item, dict)
        and isinstance(item.get("path"), str)
        and isinstance(item.get("target"), str)
    }
    local_root = Path(ctx.config.local_roms_path)
    source_root = Path(ctx.config.source.rom_root)
    expected = {
        (
            _path_key(local_root / system / "ROMCloud"),
            _path_key(source_root / system),
        )
        for system in catalog.managed_systems
    }
    changed = False
    for record in records:
        pair = (str(record.get("path", "")), str(record.get("target", "")))
        if pair not in manifest_records:
            continue
        path, target = map(Path, pair)
        normalized_pair = (_path_key(path), _path_key(target))
        system_dir = path.parent
        if (
            normalized_pair not in expected
            or path.exists()
            or path.is_symlink()
            or not system_dir.is_dir()
            or system_dir.is_symlink()
            or not target.is_dir()
            or target.is_symlink()
            or not is_within(path, local_root)
            or not is_within(target, source_root)
        ):
            continue
        try:
            path.symlink_to(target, target_is_directory=True)
        except FileExistsError:
            continue
        changed = True
    return changed


def _files_snapshot(root: Path) -> dict[str, bytes]:
    if not root.is_dir():
        return {}
    result: dict[str, bytes] = {}
    for path in root.iterdir():
        if path.is_file() and not path.is_symlink() and (
            path.name.startswith("es_systems_")
            or path.name == "romcloud-es-overlay-patches.json"
        ):
            try:
                result[path.name] = path.read_bytes()
            except OSError:
                pass
    return result


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
        config_before = ctx.paths.services_config.read_bytes() if ctx.paths.services_config.is_file() else None
        mount_service.install_service(
            str(home / "bin" / "romcloud"),
            service_path=ctx.paths.mount_service,
            services_config_path=ctx.paths.services_config,
        )
        after = ctx.paths.mount_service.read_bytes() if ctx.paths.mount_service.is_file() else None
        config_after = ctx.paths.services_config.read_bytes() if ctx.paths.services_config.is_file() else None
        return before != after or config_before != config_after

    def gamelist() -> bool:
        return ports_gamelist_config.reconcile(gamelist_path=ports_dir / "gamelist.xml")

    def icon() -> bool:
        return ports_gamelist_config.sync_icon(
            source_icon=home / "ports-gfx" / "ports_gfx" / "assets" / "icon.png",
            ports_dir=ports_dir,
        )

    def proxies() -> bool:
        finding = next(item for item in collect_diagnostics(ctx.config_path, paths=ctx.paths, activity=ctx.activity)[0].findings if item.id == "presentation.proxy_missing")
        game_ids = {str(value) for value in finding.metadata.get("game_ids", [])}
        return _restore_diagnosed_proxies(ctx, game_ids)

    def direct_links() -> bool:
        finding = next(item for item in collect_diagnostics(ctx.config_path, paths=ctx.paths, activity=ctx.activity)[0].findings if item.id == "presentation.direct_link_missing")
        records = finding.metadata.get("links", [])
        return _restore_diagnosed_direct_links(
            ctx, records if isinstance(records, list) else []
        )

    def es_refresh() -> bool:
        from romcloud.core.capabilities import OperatingMode
        from romcloud.integrations.batocera import es_config
        from romcloud.integrations.batocera.system_registry import (
            inspect_live_system_registry,
        )

        catalog = _inspect_catalog(Path(ctx.config.data_path) / "catalog.db")
        mode = inspect_operating_mode(ctx.config)
        if (
            not catalog.trusted
            or mode.state != "valid"
            or mode.mode not in {OperatingMode.CACHE, OperatingMode.OFFLINE}
        ):
            raise RuntimeError(
                "ES repair authority changed after diagnostics; no ES file was written."
            )
        before = _files_snapshot(ctx.paths.es_user_config_dir)
        registry = inspect_live_system_registry(
            user_config_dir=ctx.paths.es_user_config_dir,
            system_config_dir=ctx.paths.es_system_config_dir,
            legacy_config_dir=ctx.paths.es_legacy_config_dir,
        )
        es_config.refresh(
            catalog.managed_systems,
            stock_path=ctx.paths.es_stock,
            override_path=ctx.paths.es_override,
            wrapper_path=ctx.romcloud_home / "bin" / "romcloud-run",
            system_registry=registry,
        )
        return before != _files_snapshot(ctx.paths.es_user_config_dir)

    def es_remove() -> bool:
        from romcloud.core.capabilities import OperatingMode
        from romcloud.integrations.batocera import es_config

        catalog = _inspect_catalog(Path(ctx.config.data_path) / "catalog.db")
        mode = inspect_operating_mode(ctx.config)
        if (
            not catalog.trusted
            or mode.state != "valid"
            or mode.mode is not OperatingMode.CONNECTED
        ):
            raise RuntimeError(
                "ES repair authority changed after diagnostics; no ES file was written."
            )
        before = _files_snapshot(ctx.paths.es_user_config_dir)
        es_config.remove(override_path=ctx.paths.es_override)
        return before != _files_snapshot(ctx.paths.es_user_config_dir)

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
