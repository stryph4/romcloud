"""Shared install/update artifact reconciliation.

Both the bootstrap installer (``scripts/install.sh``) and the self-updater
(:func:`romcloud.lifecycle.update.perform_update`) need to write the
exact same set of managed runtime artifacts: the ``romcloud``/``romcloud-run``
wrappers, preservation of previously installed experimental Google OAuth
metadata, the optional graphical Ports UI payload (including its
EmulationStation Ports ``gamelist.xml`` entry/icon), the ROMCloud-owned
Batocera boot service, and — only if previously enabled — the
EmulationStation override. The small Batocera lifecycle hook used by Auto
SaveSync is also refreshed best-effort. This module is the single, idempotent
implementation of that logic so neither caller duplicates it, and so a fresh
install and a later self-update always produce byte-identical artifacts from
the same source revision.

Failure semantics ("ROMCloud may fail; Batocera must not")
-----------------------------------------------------------
- The ``romcloud``/``romcloud-run`` wrappers are **required**. Parked Google
  Drive metadata is preserved in place but is never downloaded or deployed by
  normal install/update/repair.
- Everything else — the graphical Ports UI (and its gamelist.xml entry),
  the boot service script, and the EmulationStation override — is
  best-effort. A missing/incompatible system Python or a never-installed ES
  override are normal states,
  not failures, and are reported back
  through the returned result objects rather than raised.
"""

from __future__ import annotations

import shutil
import subprocess
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Optional

from romcloud.infrastructure.atomic_file import atomic_write_text
from romcloud.infrastructure.google_auth import (
    GOOGLE_OAUTH_CLIENT_RELATIVE_PATH,
)
from romcloud.infrastructure.logging import get_logger

log = get_logger("installer")

DEFAULT_PORTS_DIR = Path("/userdata/roms/ports")


@dataclass(frozen=True)
class GoogleOAuthDeploymentResult:
    configured: bool
    target_path: Path
    source: str
    warning: str = ""
    unavailable_reason: str = ""


def reconcile_google_oauth_metadata(
    *,
    romcloud_home: Path,
    project_root: Path,
    environment: Optional[Mapping[str, str]] = None,
) -> GoogleOAuthDeploymentResult:
    """Preserve existing experimental metadata without deploying new secrets.

    Google Drive is dormant for the beta. Normal install/update/repair performs
    no metadata retrieval and ignores release/environment candidates. Any
    previously installed copy remains opaque and in place so experimental users
    are not broken. User OAuth tokens are deliberately outside this path and are
    never copied or removed.
    """
    romcloud_home = Path(romcloud_home)
    # Kept in the signature for install/update/repair call compatibility. The
    # parked beta path deliberately consumes neither source artifacts nor build
    # environment secrets.
    del project_root, environment
    target_path = romcloud_home / GOOGLE_OAUTH_CLIENT_RELATIVE_PATH
    if target_path.is_file():
        # Treat this as opaque legacy state: beta reconciliation neither reads
        # the client secret nor validates, rewrites, logs, or deletes the file.
        return GoogleOAuthDeploymentResult(
            True,
            target_path,
            "existing_runtime",
        )
    return GoogleOAuthDeploymentResult(False, target_path, "dormant")


# ── low-level file writing ────────────────────────────────────────────────────


def _write_executable(path: Path, content: str) -> Path:
    """Write *content* to *path* atomically (write-temp-then-rename) with
    the executable bit set."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f".{path.name}.tmp")
    tmp_path.write_text(content, encoding="utf-8")
    tmp_path.chmod(0o755)
    tmp_path.replace(path)
    return path


# ── core wrappers (required) ──────────────────────────────────────────────────


def _cli_wrapper_content(venv_python: Path) -> str:
    return f'#!/bin/bash\nexec "{venv_python}" -m romcloud.cli.main "$@"\n'


def _launch_wrapper_content(venv_python: Path) -> str:
    lines = [
        f"#!{venv_python}",
        '"""romcloud-run — Batocera 42+ EmulationStation launch wrapper.',
        "",
        "Receives the exact argv that EmulationStation would pass to emulatorlauncher.",
        "",
        "  - Non-.romcloud ROM:  exec emulatorlauncher with original argv unchanged.",
        "  - .romcloud proxy:    resolve/cache the real ROM, replace only the -rom",
        "                        value, exec emulatorlauncher with all other args intact.",
        "",
        "Example <command> for es_systems.cfg:",
        "    /userdata/system/romcloud/bin/romcloud-run %CONTROLLERSCONFIG% -system "
        "%SYSTEM% -rom %ROM% -gameinfoxml %GAMEINFOXML% -systemname %SYSTEMNAME%",
        '"""',
        "import sys as _sys",
        "",
        "from romcloud.integrations.batocera.launcher import run_launcher_wrapper",
        "",
        "run_launcher_wrapper(_sys.argv)",
        "",
    ]
    return "\n".join(lines)


@dataclass(frozen=True)
class CoreWrappersResult:
    cli_wrapper: Path
    launch_wrapper: Path


def write_core_wrappers(bin_dir: Path, venv_python: Path) -> CoreWrappersResult:
    """Write (or refresh) the ``romcloud`` and ``romcloud-run`` wrappers.

    Required — any exception here must be treated by the caller as a failed
    install/update, never a partial success.
    """
    cli_wrapper = _write_executable(bin_dir / "romcloud", _cli_wrapper_content(venv_python))
    launch_wrapper = _write_executable(bin_dir / "romcloud-run", _launch_wrapper_content(venv_python))
    return CoreWrappersResult(cli_wrapper=cli_wrapper, launch_wrapper=launch_wrapper)


# ── graphical Ports UI (best-effort) ──────────────────────────────────────────


def detect_system_python(explicit: Optional[str] = None) -> Optional[str]:
    """Resolve the Batocera system Python to run the graphical Ports UI
    under: an explicit override, else ``/usr/bin/python3``, else whatever
    ``python3`` resolves to on PATH."""
    if explicit:
        return explicit
    if Path("/usr/bin/python3").is_file():
        return "/usr/bin/python3"
    return shutil.which("python3")


def _system_python_has_pygame(system_python: str) -> bool:
    try:
        result = subprocess.run(
            [system_python, "-c", "import pygame"],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


def _display_trace_shell(log_path: Path, event: str) -> str:
    """Tiny best-effort shell trace correlated with Python monotonic logs."""
    return (
        f'export ROMCLOUD_DISPLAY_LOG="{log_path}"\n'
        f'mkdir -p "{log_path.parent}" 2>/dev/null || true\n'
        'IFS=" " read -r ROMCLOUD_MONOTONIC _ < /proc/uptime\n'
        f'printf \'monotonic=%s pid=%s parent_pid=%s event="{event}"\\n\' '
        '"$ROMCLOUD_MONOTONIC" "$$" "$PPID" '
        '>> "$ROMCLOUD_DISPLAY_LOG" 2>/dev/null || true\n'
    )


@dataclass(frozen=True)
class PortsUiResult:
    installed: bool
    system_python: Optional[str] = None
    skip_reason: Optional[str] = None
    ports_gfx_dir: Optional[Path] = None
    wrapper_path: Optional[Path] = None
    launch_progress_wrapper_path: Optional[Path] = None
    port_entry_path: Optional[Path] = None
    port_entry_skip_reason: Optional[str] = None
    error: Optional[str] = None


def install_ports_ui(
    *,
    project_root: Path,
    ports_gfx_dir: Path,
    bin_dir: Path,
    romcloud_bin: Path,
    ports_dir: Path,
    system_python: Optional[str] = None,
) -> PortsUiResult:
    """Install/refresh the graphical Ports UI from *project_root*'s
    ``ports_gfx/`` payload.

    Best-effort: any failure is reported in the returned result, never
    raised — a missing/incompatible system Python (or any other graphical
    integration problem) must never break the backend install/update.
    """
    try:
        resolved_python = detect_system_python(system_python)
        if resolved_python is None:
            return PortsUiResult(installed=False, skip_reason="no_system_python")
        if not _system_python_has_pygame(resolved_python):
            return PortsUiResult(installed=False, system_python=resolved_python, skip_reason="no_pygame")

        source = project_root / "ports_gfx"
        if not source.is_dir():
            return PortsUiResult(
                installed=False, system_python=resolved_python, skip_reason="no_source_payload"
            )

        target = ports_gfx_dir / "ports_gfx"
        ports_gfx_dir.mkdir(parents=True, exist_ok=True)
        token = uuid.uuid4().hex
        staged = ports_gfx_dir / f".ports_gfx.staged-{token}"
        previous = ports_gfx_dir / f".ports_gfx.previous-{token}"
        swapped = False
        try:
            shutil.copytree(source, staged)
            # Validate the minimum runnable payload before moving the working
            # copy aside. Both renames stay on one filesystem on Batocera.
            for required in ("__init__.py", "app.py", "client.py"):
                if not (staged / required).is_file():
                    raise RuntimeError(
                        f"staged graphical Ports UI is missing {required}"
                    )
            if target.exists():
                target.rename(previous)
            try:
                staged.rename(target)
                swapped = True
            except BaseException:
                if previous.exists() and not target.exists():
                    previous.rename(target)
                raise
        finally:
            if staged.exists():
                shutil.rmtree(staged, ignore_errors=True)

        display_log = romcloud_bin.parent.parent / "logs" / "gui-display.log"
        wrapper_content = (
            "#!/bin/bash\n"
            f'{_display_trace_shell(display_log, "wrapper_start")}'
            f'export ROMCLOUD_BIN="{romcloud_bin}"\n'
            f'export PYTHONPATH="{ports_gfx_dir}${{PYTHONPATH:+:$PYTHONPATH}}"\n'
            f'exec "{resolved_python}" -m ports_gfx "$@"\n'
        )
        wrapper_path = _write_executable(bin_dir / "romcloud-ports", wrapper_content)

        # The cache-miss graphical progress screen (see
        # romcloud.ui.graphical_progress) is driven directly over stdin/stdout
        # by the venv-side launcher process, never via ROMCLOUD_BIN/uidata —
        # it has nothing to ask the backend for, so no ROMCLOUD_BIN export.
        launch_progress_wrapper_content = (
            "#!/bin/bash\n"
            f'export PYTHONPATH="{ports_gfx_dir}${{PYTHONPATH:+:$PYTHONPATH}}"\n'
            f'exec "{resolved_python}" -m ports_gfx.launch_progress "$@"\n'
        )
        launch_progress_wrapper_path = _write_executable(
            bin_dir / "romcloud-launch-progress", launch_progress_wrapper_content
        )

        port_entry_path = None
        port_entry_skip_reason = None
        if ports_dir.is_dir():
            port_entry_content = (
                "#!/bin/bash\n"
                f'{_display_trace_shell(display_log, "port_entry_start")}'
                f'exec "{wrapper_path}" "$@"\n'
            )
            port_entry_path = _write_executable(ports_dir / "ROMCloud.sh", port_entry_content)
        else:
            port_entry_skip_reason = "ports_dir_missing"

        result = PortsUiResult(
            installed=True,
            system_python=resolved_python,
            ports_gfx_dir=target,
            wrapper_path=wrapper_path,
            launch_progress_wrapper_path=launch_progress_wrapper_path,
            port_entry_path=port_entry_path,
            port_entry_skip_reason=port_entry_skip_reason,
        )
        if previous.exists():
            shutil.rmtree(previous, ignore_errors=True)
        return result
    except Exception as exc:  # noqa: BLE001 — graphical UI must never break backend install/update
        # Wrapper or Port-entry failure after the payload swap must restore
        # the prior working GUI, not strand the installation on a partial UI.
        try:
            if "swapped" in locals() and swapped and previous.exists():
                if target.exists():
                    shutil.rmtree(target)
                previous.rename(target)
        except Exception:
            log.error("Failed to roll back graphical Ports UI payload", exc_info=True)
        log.warning("Failed to install/refresh graphical Ports UI", exc_info=True)
        return PortsUiResult(installed=False, error=str(exc))


# ── previously-enabled Batocera integrations (best-effort, only if applicable) ──


def _reconcile_mount_service_status(bin_dir: Path) -> tuple[bool, bool]:
    """Install or refresh ROMCloud's owned Batocera boot service.

    It also owns the reliable Auto SaveSync resident-loop handoff and is
    therefore applicable even when no CIFS mount is configured.

    Returns separate ``(installed, enabled)`` outcomes so a successfully
    written but disabled service is never reported as fully reconciled.
    """
    from romcloud.integrations.batocera import mount_service

    service_path = mount_service.SERVICE_SCRIPT_PATH
    try:
        mount_service.install_service(
            str(bin_dir / "romcloud"),
            service_path=service_path,
            activation_state_path=mount_service.startup_activation.state_path(
                bin_dir.parent
            ),
        )
        return True, mount_service.is_service_enabled()
    except Exception:  # noqa: BLE001 — optional integration, never fatal
        log.warning("Failed to reconcile Batocera mount service script", exc_info=True)
        return False, False


def reconcile_mount_service(bin_dir: Path) -> bool:
    """Compatibility status: true only when the script is installed and enabled."""
    installed, enabled = _reconcile_mount_service_status(bin_dir)
    return installed and enabled


def _reconcile_es_override_with_change(
    config_path: Path,
) -> tuple[Optional[bool], bool]:
    """Restore or refresh ROMCloud's EmulationStation override from the catalog.

    Returns the existing tri-state reconciliation status plus whether the
    effective on-disk ES integration changed and therefore needs a restart.
    """
    from romcloud.integrations.batocera import es_config

    override_path = es_config.ROMCLOUD_OVERRIDE_PATH
    if not config_path.exists():
        return None, False
    try:
        from romcloud.bootstrap.container import Container
        from romcloud.core.capabilities import OperatingMode
        from romcloud.infrastructure.config import load_config
        from romcloud.infrastructure.library_view import operating_mode

        config = load_config(str(config_path))
        if operating_mode(config) is OperatingMode.CONNECTED:
            return True, es_config.remove(override_path=override_path)
        container = Container(config)
        managed = container.game_repo.list_systems()
        if not managed and not override_path.exists():
            return None, False
        before = es_config.status(
            managed,
            stock_path=es_config.STOCK_ES_SYSTEMS_PATH,
            override_path=override_path,
            wrapper_path=es_config.WRAPPER_SCRIPT_PATH,
            system_registry=container.system_registry,
        )
        es_config.refresh(
            managed,
            stock_path=es_config.STOCK_ES_SYSTEMS_PATH,
            override_path=override_path,
            wrapper_path=es_config.WRAPPER_SCRIPT_PATH,
            system_registry=container.system_registry,
        )
        return True, not before.up_to_date
    except Exception:  # noqa: BLE001 — optional integration, never fatal
        log.warning("Failed to reconcile EmulationStation override", exc_info=True)
        return False, False


def reconcile_es_override(config_path: Path) -> Optional[bool]:
    """Compatibility wrapper returning the existing tri-state status."""
    status, _changed = _reconcile_es_override_with_change(config_path)
    return status


def reconcile_ports_gamelist(ports_ui: PortsUiResult, ports_dir: Path) -> Optional[bool]:
    """If the ROMCloud Ports entry (``ROMCloud.sh``) was installed this run,
    copy its bundled icon into the Ports artwork directory
    (``<ports_dir>/images/ROMCloud.png``) and ensure `gamelist.xml` has a
    matching entry referencing it by relative path — the layout verified
    against RetroGameSets/RGSX (see
    :mod:`romcloud.integrations.batocera.ports_gamelist_config`).

    Returns ``None`` if not applicable (no Ports entry installed this run —
    e.g. no system Python with pygame, or the ports directory doesn't
    exist), ``True`` if the gamelist entry was reconciled successfully, or
    ``False`` if it failed (best-effort; never raises).
    """
    if ports_ui.port_entry_path is None or ports_ui.ports_gfx_dir is None:
        return None

    from romcloud.integrations.batocera import ports_gamelist_config

    source_icon = ports_ui.ports_gfx_dir / "assets" / "icon.png"
    icon_ok = True
    if source_icon.exists():
        try:
            ports_gamelist_config.sync_icon(source_icon=source_icon, ports_dir=ports_dir)
        except Exception:  # noqa: BLE001 — best-effort; the gamelist entry itself still gets written below
            log.warning("Failed to sync ROMCloud Ports icon artwork", exc_info=True)
            icon_ok = False

    try:
        ports_gamelist_config.reconcile(
            image=ports_gamelist_config.ROMCLOUD_IMAGE_RELATIVE_PATH,
            gamelist_path=ports_dir / "gamelist.xml",
        )
        return icon_ok
    except Exception:  # noqa: BLE001 — optional integration, never fatal
        log.warning("Failed to reconcile Ports gamelist entry", exc_info=True)
        return False


def reconcile_auto_savesync_hook(bin_dir: Path) -> bool:
    """Install/refresh the best-effort Batocera game lifecycle hook."""
    try:
        from romcloud.integrations.batocera import auto_savesync

        auto_savesync.install_hook(bin_dir / "romcloud")
        return True
    except Exception:  # noqa: BLE001 - optional integration, never fatal
        log.warning("Failed to reconcile Batocera Auto SaveSync hook", exc_info=True)
        return False


# ── full reconciliation ───────────────────────────────────────────────────────


@dataclass(frozen=True)
class ReconcileReport:
    core: CoreWrappersResult
    google_oauth: GoogleOAuthDeploymentResult
    ports_ui: PortsUiResult
    mount_service: Optional[bool]
    es_override: Optional[bool]
    ports_gamelist: Optional[bool]
    autosync_hook: bool
    proxies_restored: int = 0
    mount_service_enabled: Optional[bool] = None
    game_access: Optional[bool] = None
    catalog_available: Optional[bool] = None
    es_restart_required: bool = False
    warnings: tuple[str, ...] = ()


def reconcile_install(
    *,
    romcloud_home: Path,
    project_root: Path,
    ports_dir: Optional[Path] = None,
    system_python: Optional[str] = None,
    environment: Optional[Mapping[str, str]] = None,
    repair: bool = False,
) -> ReconcileReport:
    """Reconcile every ROMCloud-managed runtime artifact under
    *romcloud_home* against *project_root* (the current source tree — the
    live checkout for a fresh install, or the freshly extracted update
    archive for a self-update).

    Idempotent: safe to call repeatedly with no observable difference after
    the first successful call. Raises if required core wrappers cannot be
    written. Parked Google metadata is preservation-only; every optional
    artifact is reconciled best-effort.
    """
    romcloud_home = Path(romcloud_home)
    project_root = Path(project_root)
    venv_python = romcloud_home / "venv" / "bin" / "python"
    bin_dir = romcloud_home / "bin"
    ports_gfx_dir = romcloud_home / "ports-gfx"
    config_path = romcloud_home / "config" / "romcloud.toml"
    resolved_ports_dir = Path(ports_dir) if ports_dir else DEFAULT_PORTS_DIR

    warnings: list[str] = []
    try:
        from romcloud.lifecycle.runtime_layout import reconcile_legacy_runtime_layout

        reconcile_legacy_runtime_layout(config_path)
    except Exception:  # noqa: BLE001 - optional conservative migration
        log.warning("Failed to reconcile legacy runtime paths", exc_info=True)
        warnings.append("Legacy runtime paths could not be reconciled.")

    core = write_core_wrappers(bin_dir, venv_python)
    # The lifecycle ledger is deletion authority; configuration alone is not.
    # Record the home only after the required wrappers were written exactly.
    from romcloud.infrastructure.ownership import record_owned_roots

    record_owned_roots(romcloud_home, {"home": romcloud_home})
    google_oauth = reconcile_google_oauth_metadata(
        romcloud_home=romcloud_home,
        project_root=project_root,
        environment=environment,
    )

    existing_ports_payload_usable = all(
        (ports_gfx_dir / "ports_gfx" / name).is_file()
        for name in ("__init__.py", "app.py", "client.py")
    )
    ports_ui = install_ports_ui(
        project_root=project_root,
        ports_gfx_dir=ports_gfx_dir,
        bin_dir=bin_dir,
        romcloud_bin=bin_dir / "romcloud",
        ports_dir=resolved_ports_dir,
        system_python=system_python,
    )

    # Use the result of this reconciliation attempt directly.  A stale service
    # script left by an earlier install must not make a failed refresh look
    # successful.
    mount_service_status, mount_service_enabled = _reconcile_mount_service_status(
        bin_dir
    )

    configured = None
    catalog_available: Optional[bool] = None
    if config_path.exists():
        try:
            from romcloud.infrastructure.config import load_config

            configured = load_config(str(config_path))
            catalog_available = (Path(configured.data_path) / "catalog.db").is_file()
        except Exception:
            configured = None

    catalog_blocks_repair = repair and configured is not None and not catalog_available
    es_restart_required = False
    if catalog_blocks_repair:
        es_override_status = None
        warnings.append(
            "Catalog state is unavailable; preserved existing EmulationStation, "
            "proxy, and Direct-link presentation without reconciliation."
        )
    else:
        es_override_status, es_restart_required = _reconcile_es_override_with_change(
            config_path
        )
    ports_gamelist_status = reconcile_ports_gamelist(ports_ui, resolved_ports_dir)
    autosync_hook_status = reconcile_auto_savesync_hook(bin_dir)
    proxies_restored = 0
    game_access_status: Optional[bool] = None
    if configured is not None and not catalog_blocks_repair:
        try:
            from romcloud.integrations.batocera.game_access import reconcile_game_access

            if configured.source.enabled:
                before = len(list(Path(configured.local_roms_path).glob("*/*.romcloud")))
                # The named ES override was already reconciled above; this pass
                # restores access artifacts only, so an optional ES failure cannot
                # prevent proxy recovery.
                reconcile_game_access(configured, refresh_es=False)
                after = len(list(Path(configured.local_roms_path).glob("*/*.romcloud")))
                proxies_restored = max(0, after - before)
                game_access_status = True
        except Exception:  # noqa: BLE001 — optional recovery, never breaks runtime repair
            log.warning("Failed to restore missing ROMCloud proxy files", exc_info=True)
            game_access_status = False

    ports_error = getattr(ports_ui, "error", None)
    ports_skip_reason = getattr(ports_ui, "skip_reason", None)
    ports_installed = bool(getattr(ports_ui, "installed", False))
    if ports_error:
        warnings.append(f"Graphical Ports UI reconciliation failed: {ports_error}")
    elif ports_skip_reason == "no_source_payload":
        warnings.append("Graphical Ports UI source payload is unavailable.")
    elif repair and not ports_installed and not existing_ports_payload_usable:
        warnings.append(
            "Graphical Ports UI remains unavailable because a compatible system "
            "Python with pygame was not found."
        )
    if mount_service_status is False:
        warnings.append("Batocera startup service script could not be reconciled.")
    elif mount_service_enabled is False:
        warnings.append(
            "Batocera startup service was written but is not enabled; enable it manually."
        )
    if es_override_status is False:
        warnings.append("EmulationStation integration could not be reconciled.")
    if ports_gamelist_status is False:
        warnings.append(
            "The shared Ports gamelist could not be safely reconciled; "
            "its original content was preserved."
        )
    if not autosync_hook_status:
        warnings.append("Auto SaveSync lifecycle hook could not be reconciled.")
    if game_access_status is False:
        warnings.append("Game-access proxies or Direct links could not be reconciled.")
    return ReconcileReport(
        core=core,
        google_oauth=google_oauth,
        ports_ui=ports_ui,
        mount_service=mount_service_status,
        mount_service_enabled=mount_service_enabled,
        es_override=es_override_status,
        ports_gamelist=ports_gamelist_status,
        autosync_hook=autosync_hook_status,
        proxies_restored=proxies_restored,
        game_access=game_access_status,
        catalog_available=catalog_available,
        es_restart_required=es_restart_required,
        warnings=tuple(warnings),
    )
