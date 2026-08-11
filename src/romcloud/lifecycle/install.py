"""Shared install/update artifact reconciliation.

Both the bootstrap installer (``scripts/install.sh``) and the self-updater
(:func:`romcloud.lifecycle.update.perform_update`) need to write the
exact same set of managed runtime artifacts: the ``romcloud``/``romcloud-run``
wrappers, the optional graphical Ports UI payload (including its
EmulationStation Ports ``gamelist.xml`` entry/icon), and — only if
previously enabled — the Batocera mount service script and the
EmulationStation override. This module is the single, idempotent implementation of that
logic so neither caller duplicates it, and so a fresh install and a later
self-update always produce byte-identical artifacts from the same source
revision.

Failure semantics ("ROMCloud may fail; Batocera must not")
-----------------------------------------------------------
- The ``romcloud``/``romcloud-run`` wrappers are **required** — writing them
  is the one thing this module lets raise. Callers must treat any exception
  from :func:`write_core_wrappers` (and therefore from :func:`reconcile_install`)
  as a failed install/update.
- Everything else — the graphical Ports UI (and its gamelist.xml entry),
  the mount service script, and the EmulationStation override — is
  best-effort. A missing/incompatible system Python, a never-configured
  mount service, or a never-installed ES override are all normal states,
  not failures, and are reported back
  through the returned result objects rather than raised.
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from romcloud.infrastructure.logging import get_logger

log = get_logger("installer")

DEFAULT_PORTS_DIR = Path("/userdata/roms/ports")


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
        if target.exists():
            shutil.rmtree(target)
        shutil.copytree(source, target)

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

        return PortsUiResult(
            installed=True,
            system_python=resolved_python,
            ports_gfx_dir=target,
            wrapper_path=wrapper_path,
            launch_progress_wrapper_path=launch_progress_wrapper_path,
            port_entry_path=port_entry_path,
            port_entry_skip_reason=port_entry_skip_reason,
        )
    except Exception as exc:  # noqa: BLE001 — graphical UI must never break backend install/update
        log.warning("Failed to install/refresh graphical Ports UI", exc_info=True)
        return PortsUiResult(installed=False, error=str(exc))


# ── previously-enabled Batocera integrations (best-effort, only if applicable) ──


def reconcile_mount_service(bin_dir: Path) -> Optional[bool]:
    """If the Batocera mount service script was already installed, refresh
    its content to match the just-installed backend code.

    Returns ``None`` if the service was never installed (not applicable —
    nothing to reconcile), ``True`` if it was refreshed successfully, or
    ``False`` if refreshing it failed (best-effort; never raises).
    """
    from romcloud.integrations.batocera import mount_service

    service_path = mount_service.SERVICE_SCRIPT_PATH
    if not service_path.exists():
        return None
    try:
        mount_service.install_service(str(bin_dir / "romcloud"), service_path=service_path)
        return True
    except Exception:  # noqa: BLE001 — optional integration, never fatal
        log.warning("Failed to reconcile Batocera mount service script", exc_info=True)
        return False


def reconcile_es_override(config_path: Path) -> Optional[bool]:
    """Restore or refresh ROMCloud's EmulationStation override from the catalog.

    Returns ``None`` if not applicable (no usable configuration/catalog), ``True``
    if it was refreshed successfully, or ``False`` if refreshing it failed
    (best-effort; never raises).
    """
    from romcloud.integrations.batocera import es_config

    override_path = es_config.ROMCLOUD_OVERRIDE_PATH
    if not config_path.exists():
        return None
    try:
        from romcloud.bootstrap.container import Container
        from romcloud.infrastructure.config import load_config

        config = load_config(str(config_path))
        if config.game_access_mode == "direct_nas":
            es_config.remove(override_path=override_path)
            return True
        container = Container(config)
        managed = container.game_repo.list_systems()
        if not managed and not override_path.exists():
            return None
        es_config.refresh(
            managed,
            stock_path=es_config.STOCK_ES_SYSTEMS_PATH,
            override_path=override_path,
            wrapper_path=es_config.WRAPPER_SCRIPT_PATH,
        )
        return True
    except Exception:  # noqa: BLE001 — optional integration, never fatal
        log.warning("Failed to reconcile EmulationStation override", exc_info=True)
        return False


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
    if source_icon.exists():
        try:
            ports_gamelist_config.sync_icon(source_icon=source_icon, ports_dir=ports_dir)
        except Exception:  # noqa: BLE001 — best-effort; the gamelist entry itself still gets written below
            log.warning("Failed to sync ROMCloud Ports icon artwork", exc_info=True)

    try:
        ports_gamelist_config.reconcile(
            image=ports_gamelist_config.ROMCLOUD_IMAGE_RELATIVE_PATH,
            gamelist_path=ports_dir / "gamelist.xml",
        )
        return True
    except Exception:  # noqa: BLE001 — optional integration, never fatal
        log.warning("Failed to reconcile Ports gamelist entry", exc_info=True)
        return False


# ── full reconciliation ───────────────────────────────────────────────────────


@dataclass(frozen=True)
class ReconcileReport:
    core: CoreWrappersResult
    ports_ui: PortsUiResult
    mount_service: Optional[bool]
    es_override: Optional[bool]
    ports_gamelist: Optional[bool]
    proxies_restored: int = 0


def reconcile_install(
    *,
    romcloud_home: Path,
    project_root: Path,
    ports_dir: Optional[Path] = None,
    system_python: Optional[str] = None,
) -> ReconcileReport:
    """Reconcile every ROMCloud-managed runtime artifact under
    *romcloud_home* against *project_root* (the current source tree — the
    live checkout for a fresh install, or the freshly extracted update
    archive for a self-update).

    Idempotent: safe to call repeatedly with no observable difference after
    the first successful call. Raises only if the required core wrappers
    cannot be written; every other artifact is reconciled best-effort.
    """
    romcloud_home = Path(romcloud_home)
    project_root = Path(project_root)
    venv_python = romcloud_home / "venv" / "bin" / "python"
    bin_dir = romcloud_home / "bin"
    ports_gfx_dir = romcloud_home / "ports-gfx"
    config_path = romcloud_home / "config" / "romcloud.toml"
    resolved_ports_dir = Path(ports_dir) if ports_dir else DEFAULT_PORTS_DIR

    try:
        from romcloud.lifecycle.runtime_layout import reconcile_legacy_runtime_layout

        reconcile_legacy_runtime_layout(config_path)
    except Exception:  # noqa: BLE001 - optional conservative migration
        log.warning("Failed to reconcile legacy runtime paths", exc_info=True)

    core = write_core_wrappers(bin_dir, venv_python)

    ports_ui = install_ports_ui(
        project_root=project_root,
        ports_gfx_dir=ports_gfx_dir,
        bin_dir=bin_dir,
        romcloud_bin=bin_dir / "romcloud",
        ports_dir=resolved_ports_dir,
        system_python=system_python,
    )

    mount_service_status = reconcile_mount_service(bin_dir)
    es_override_status = reconcile_es_override(config_path)
    ports_gamelist_status = reconcile_ports_gamelist(ports_ui, resolved_ports_dir)
    proxies_restored = 0
    if config_path.exists():
        try:
            from romcloud.infrastructure.config import load_config
            from romcloud.integrations.batocera.game_access import reconcile_game_access

            configured = load_config(str(config_path))
            before = len(list(Path(configured.local_roms_path).glob("*/*.romcloud")))
            reconcile_game_access(configured)
            after = len(list(Path(configured.local_roms_path).glob("*/*.romcloud")))
            proxies_restored = max(0, after - before)
        except Exception:  # noqa: BLE001 — optional recovery, never breaks runtime repair
            log.warning("Failed to restore missing ROMCloud proxy files", exc_info=True)

    return ReconcileReport(
        core=core,
        ports_ui=ports_ui,
        mount_service=mount_service_status,
        es_override=es_override_status,
        ports_gamelist=ports_gamelist_status,
        proxies_restored=proxies_restored,
    )
