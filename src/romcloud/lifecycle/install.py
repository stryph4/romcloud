"""Shared install/update artifact reconciliation.

Both the bootstrap installer (``scripts/install.sh``) and the self-updater
(:func:`romcloud.lifecycle.update.perform_update`) need to write the
exact same set of managed runtime artifacts: the ``romcloud``/``romcloud-run``
wrappers, externally supplied Google OAuth deployment metadata, the optional
graphical Ports UI payload (including its
EmulationStation Ports ``gamelist.xml`` entry/icon), the ROMCloud-owned
Batocera boot service, and — only if previously enabled — the
EmulationStation override. The small Batocera lifecycle hook used by Auto
SaveSync is also refreshed best-effort. This module is the single, idempotent
implementation of that logic so neither caller duplicates it, and so a fresh
install and a later self-update always produce byte-identical artifacts from
the same source revision.

Failure semantics ("ROMCloud may fail; Batocera must not")
-----------------------------------------------------------
- The ``romcloud``/``romcloud-run`` wrappers are **required**. Google OAuth
  deployment metadata is optional unless the release contains an explicit
  ``runtime/google-oauth-client.required`` marker. Optional metadata failures
  produce warnings and never roll back otherwise-valid ROMCloud updates.
- Everything else — the graphical Ports UI (and its gamelist.xml entry),
  the boot service script, and the EmulationStation override — is
  best-effort. A missing/incompatible system Python or a never-installed ES
  override are normal states,
  not failures, and are reported back
  through the returned result objects rather than raised.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping, Optional

from romcloud.core.exceptions import ConfigurationError
from romcloud.infrastructure.atomic_file import atomic_write_text
from romcloud.infrastructure.google_auth import (
    GOOGLE_OAUTH_CLIENT_RELATIVE_PATH,
    GOOGLE_OAUTH_STATUS_RELATIVE_PATH,
    GoogleOAuthClientConfig,
)
from romcloud.infrastructure.logging import get_logger

log = get_logger("installer")

DEFAULT_PORTS_DIR = Path("/userdata/roms/ports")
GOOGLE_OAUTH_LOCATOR_RELATIVE_PATH = Path("runtime/google-oauth-client.url")
GOOGLE_OAUTH_REQUIRED_RELATIVE_PATH = Path("runtime/google-oauth-client.required")
_MAX_GOOGLE_OAUTH_METADATA_BYTES = 16 * 1024
GOOGLE_OAUTH_RETRY_GUIDANCE = (
    "Other ROMCloud features are unaffected. Retry later or run Repair after "
    "the issue is resolved."
)


@dataclass(frozen=True)
class GoogleOAuthDeploymentResult:
    configured: bool
    target_path: Path
    source: str
    warning: str = ""
    unavailable_reason: str = ""


def _download_google_oauth_metadata(url: str) -> bytes:
    parsed = urllib.parse.urlsplit(url)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
    ):
        raise ConfigurationError(
            "Google Drive deployment metadata locator must be a plain HTTPS URL"
        )
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "romcloud-installer"},
    )
    try:
        with urllib.request.urlopen(request, timeout=15.0) as response:
            final_url = str(getattr(response, "geturl", lambda: url)())
            final = urllib.parse.urlsplit(final_url)
            if (
                final.scheme != "https"
                or not final.hostname
                or final.username is not None
                or final.password is not None
                or final.fragment
            ):
                raise ConfigurationError(
                    "Google Drive deployment metadata redirected outside HTTPS"
                )
            payload = response.read(_MAX_GOOGLE_OAUTH_METADATA_BYTES + 1)
    except ConfigurationError:
        raise
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise ConfigurationError(
            "Google Drive deployment metadata could not be downloaded"
        ) from exc
    if not payload or len(payload) > _MAX_GOOGLE_OAUTH_METADATA_BYTES:
        raise ConfigurationError(
            "Google Drive deployment metadata download is empty or too large"
        )
    return payload


def _parse_google_oauth_metadata(
    payload: bytes | str,
    *,
    require_deployment_schema: bool = False,
) -> GoogleOAuthClientConfig:
    try:
        raw = payload.decode("utf-8") if isinstance(payload, bytes) else payload
        parsed = json.loads(raw)
        return GoogleOAuthClientConfig.from_mapping(
            parsed,
            require_deployment_schema=require_deployment_schema,
        )
    except (UnicodeError, ValueError, TypeError, ConfigurationError) as exc:
        raise ConfigurationError(
            "Google Drive deployment metadata is malformed"
        ) from exc


def _write_google_oauth_status(
    romcloud_home: Path,
    *,
    available: bool,
    warning: str = "",
    unavailable_reason: str = "",
) -> None:
    """Best-effort, credential-free state for setup/wizard availability UX."""
    path = romcloud_home / GOOGLE_OAUTH_STATUS_RELATIVE_PATH
    payload = {
        "version": 1,
        "available": available,
        "warning": warning,
        "unavailable_reason": unavailable_reason,
    }
    try:
        atomic_write_text(
            path,
            json.dumps(payload, sort_keys=True, separators=(",", ":")),
            mode=0o600,
        )
    except OSError:
        log.warning("Could not persist Google Drive deployment availability")


def _optional_google_oauth_failure(
    *,
    romcloud_home: Path,
    target_path: Path,
    existing: Optional[GoogleOAuthClientConfig],
    reason: str,
) -> GoogleOAuthDeploymentResult:
    if existing is not None:
        warning = (
            "Google Drive configuration could not be refreshed, so ROMCloud kept "
            f"the previously installed configuration. {GOOGLE_OAUTH_RETRY_GUIDANCE}"
        )
        _write_google_oauth_status(
            romcloud_home,
            available=True,
            warning=warning,
        )
        return GoogleOAuthDeploymentResult(
            True,
            target_path,
            "existing_runtime",
            warning=warning,
        )

    unavailable_reason = f"Google Drive is unavailable because {reason}."
    warning = f"{unavailable_reason} {GOOGLE_OAUTH_RETRY_GUIDANCE}"
    _write_google_oauth_status(
        romcloud_home,
        available=False,
        warning=warning,
        unavailable_reason=unavailable_reason,
    )
    return GoogleOAuthDeploymentResult(
        False,
        target_path,
        "unavailable",
        warning=warning,
        unavailable_reason=unavailable_reason,
    )


def reconcile_google_oauth_metadata(
    *,
    romcloud_home: Path,
    project_root: Path,
    environment: Optional[Mapping[str, str]] = None,
    fetcher: Callable[[str], bytes] = _download_google_oauth_metadata,
) -> GoogleOAuthDeploymentResult:
    """Install or validate build-owned Google OAuth client metadata.

    Production source trees advertise Google Drive with the committed,
    non-secret ``runtime/google-oauth-client.url`` locator. Optional retrieval
    failures disable Google Drive without failing ROMCloud. A valid installed
    copy remains usable and can never be overwritten by malformed new input.
    Releases may opt into fail-closed behavior with the explicit ``.required``
    marker. User OAuth tokens are deliberately outside this path and never
    copied.
    """
    romcloud_home = Path(romcloud_home)
    project_root = Path(project_root)
    target_path = romcloud_home / GOOGLE_OAUTH_CLIENT_RELATIVE_PATH
    source_path = project_root / GOOGLE_OAUTH_CLIENT_RELATIVE_PATH
    locator_path = project_root / GOOGLE_OAUTH_LOCATOR_RELATIVE_PATH
    required_path = project_root / GOOGLE_OAUTH_REQUIRED_RELATIVE_PATH
    current_environment = os.environ if environment is None else environment
    client_id = str(
        current_environment.get("ROMCLOUD_GOOGLE_OAUTH_CLIENT_ID", "")
    ).strip()
    client_secret = str(
        current_environment.get("ROMCLOUD_GOOGLE_OAUTH_CLIENT_SECRET", "")
    ).strip()

    existing: Optional[GoogleOAuthClientConfig] = None
    if target_path.is_file():
        try:
            existing = _parse_google_oauth_metadata(target_path.read_bytes())
        except (OSError, ConfigurationError):
            # An invalid installed copy is never used as fallback and is not
            # allowed to obscure a valid refresh candidate.
            existing = None

    config: Optional[GoogleOAuthClientConfig] = None
    source = "unavailable"
    failure: Optional[ConfigurationError] = None
    failure_reason = "its configuration could not be retrieved"
    try:
        if client_id or client_secret:
            config = GoogleOAuthClientConfig.from_mapping(
                {"client_id": client_id, "client_secret": client_secret}
            )
            source = "build_environment"
        elif source_path.is_file():
            config = _parse_google_oauth_metadata(
                source_path.read_bytes(),
                require_deployment_schema=True,
            )
            source = "release_payload"
        elif locator_path.is_file():
            locator = locator_path.read_text(encoding="utf-8").strip()
            if (
                not locator
                or len(locator) > 4096
                or any(ch in locator for ch in "\r\n")
            ):
                raise ConfigurationError(
                    "Google Drive deployment metadata locator is malformed"
                )
            config = _parse_google_oauth_metadata(
                fetcher(locator),
                require_deployment_schema=True,
            )
            source = "deployment_url"
        elif existing is not None:
            config = existing
            source = "existing_runtime"
    except Exception as exc:  # noqa: BLE001 - optional provider must not fail ROMCloud
        failure = (
            exc
            if isinstance(exc, ConfigurationError)
            else ConfigurationError(
                "Google Drive deployment metadata could not be retrieved"
            )
        )
        if "malformed" in str(failure).casefold() or "schema" in str(failure).casefold():
            failure_reason = "the retrieved configuration was malformed"

    if failure is not None:
        if required_path.is_file():
            raise failure
        return _optional_google_oauth_failure(
            romcloud_home=romcloud_home,
            target_path=target_path,
            existing=existing,
            reason=failure_reason,
        )

    if config is None:
        return GoogleOAuthDeploymentResult(
            False,
            target_path,
            source,
            unavailable_reason=(
                "Google Drive is not configured in this ROMCloud build."
            ),
        )

    atomic_write_text(target_path, config.serialized(), mode=0o600)
    _write_google_oauth_status(romcloud_home, available=True)
    return GoogleOAuthDeploymentResult(True, target_path, source)


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


def reconcile_mount_service(bin_dir: Path) -> bool:
    """Install or refresh ROMCloud's owned Batocera boot service.

    It also owns the reliable Auto SaveSync resident-loop handoff and is
    therefore applicable even when no CIFS mount is configured.

    Returns ``True`` on success or ``False`` on a best-effort failure.
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
        from romcloud.core.capabilities import OperatingMode
        from romcloud.infrastructure.config import load_config
        from romcloud.infrastructure.library_view import operating_mode

        config = load_config(str(config_path))
        if operating_mode(config) is OperatingMode.CONNECTED:
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
            system_registry=container.system_registry,
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


def reconcile_install(
    *,
    romcloud_home: Path,
    project_root: Path,
    ports_dir: Optional[Path] = None,
    system_python: Optional[str] = None,
    environment: Optional[Mapping[str, str]] = None,
    google_oauth_fetcher: Callable[[str], bytes] = _download_google_oauth_metadata,
) -> ReconcileReport:
    """Reconcile every ROMCloud-managed runtime artifact under
    *romcloud_home* against *project_root* (the current source tree — the
    live checkout for a fresh install, or the freshly extracted update
    archive for a self-update).

    Idempotent: safe to call repeatedly with no observable difference after
    the first successful call. Raises if required core wrappers cannot be
    written or explicitly-required Google OAuth deployment metadata cannot be
    safely installed. Optional Google metadata failures and every other
    optional artifact are reconciled best-effort.
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
    google_oauth = reconcile_google_oauth_metadata(
        romcloud_home=romcloud_home,
        project_root=project_root,
        environment=environment,
        fetcher=google_oauth_fetcher,
    )

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
    autosync_hook_status = reconcile_auto_savesync_hook(bin_dir)
    proxies_restored = 0
    if config_path.exists():
        try:
            from romcloud.infrastructure.config import load_config
            from romcloud.integrations.batocera.game_access import reconcile_game_access

            configured = load_config(str(config_path))
            if configured.source.enabled:
                before = len(list(Path(configured.local_roms_path).glob("*/*.romcloud")))
                # The named ES override was already reconciled above; this pass
                # restores access artifacts only, so an optional ES failure cannot
                # prevent proxy recovery.
                reconcile_game_access(configured, refresh_es=False)
                after = len(list(Path(configured.local_roms_path).glob("*/*.romcloud")))
                proxies_restored = max(0, after - before)
        except Exception:  # noqa: BLE001 — optional recovery, never breaks runtime repair
            log.warning("Failed to restore missing ROMCloud proxy files", exc_info=True)

    return ReconcileReport(
        core=core,
        google_oauth=google_oauth,
        ports_ui=ports_ui,
        mount_service=mount_service_status,
        es_override=es_override_status,
        ports_gamelist=ports_gamelist_status,
        autosync_hook=autosync_hook_status,
        proxies_restored=proxies_restored,
    )
