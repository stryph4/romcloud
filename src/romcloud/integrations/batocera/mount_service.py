"""Batocera custom-service integration for ROMCloud boot lifecycle work.

Batocera's "custom services" mechanism is the standard, long-documented
extension point for running a script at boot: any executable script placed
under ``/userdata/system/services/`` that responds to ``start``/``stop``/
``status`` arguments can be registered with ``batocera-services enable
<name>``. This module generates and installs exactly one such script,
``romcloud_mount``. The historical name is retained for compatibility; its
local-only ``boot-start`` handoff now also detaches Auto SaveSync's resident
menu loop when that persisted setting is enabled.  The same owned service
ensures the singleton Library Manager so remote access is ready after boot.

Boot safety ("ROMCloud may fail; Batocera must not")
Real Batocera 42 hardware testing showed that a service's ``start`` action
blocking on DNS/network/Tailscale/CIFS reachability can hang or disrupt
boot. The generated script therefore:

- Routes ``start`` to ``romcloud mount boot-start`` — which never blocks;
  it only spawns a detached background worker and returns immediately (see
  :mod:`romcloud.infrastructure.mount_worker`). The manager readiness check
  is local-only, performs no source discovery or polling, and is bounded to
  five seconds.
- Wraps both ``start`` and ``stop`` with ``|| true`` and an explicit
  ``exit 0``, so that *even if* the ``romcloud`` command itself fails
  outright (missing venv, broken Python, malformed config, etc.), the
  service script itself always reports success to Batocera's service
  supervisor. The worst possible outcome is "cloud games unavailable" —
  never a blocked or failed boot.
- Uses ``set -uo pipefail`` (not ``-e``), so an unexpected failure
  mid-script can never abort it in a way that skips the final ``exit 0``.

This module does not touch Tailscale configuration in any way — reachability
waiting (see :mod:`romcloud.infrastructure.mount`) is transport-agnostic; it
only waits for the configured SMB server:port to answer, whatever route
(LAN, VPN/Tailscale, etc.) gets it there.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from romcloud.infrastructure.logging import get_logger
from romcloud.integrations.batocera import startup_activation

log = get_logger("batocera.mount_service")

SERVICE_NAME = "romcloud_mount"
LEGACY_SERVICE_NAME = "romcloud-mount"
SERVICE_SCRIPT_PATH = Path(f"/userdata/system/services/{SERVICE_NAME}")
LEGACY_SERVICE_PATH = Path(f"/userdata/system/services/{LEGACY_SERVICE_NAME}")
SYSTEM_CONFIG_PATH = Path("/userdata/system/batocera.conf")


def _service_is_enabled(config_path: Path) -> bool:
    """Return whether Batocera's persisted service list contains ROMCloud."""
    try:
        lines = config_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return False
    configured = ""
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("system.services="):
            configured = stripped.partition("=")[2].strip().strip('"\'')
    return SERVICE_NAME in configured.replace(",", " ").split()


def generate_service_script(romcloud_bin: str) -> str:
    """Return the content of the `romcloud_mount` custom-service script.

    *romcloud_bin* is the absolute path to the installed `romcloud` CLI
    wrapper (see `scripts/install.sh`), which already execs the venv's own
    python — this script never needs to know about Python/venv paths itself.
    """
    romcloud_home = Path(romcloud_bin).parent.parent
    return (
        "#!/bin/bash\n"
        "# ROMCloud SMB source mount — Batocera custom service.\n"
        "# Installed by `romcloud mount install`. Generated — do not edit by hand.\n"
        "#\n"
        "# One-time enablement after install:\n"
        f"#   batocera-services enable {SERVICE_NAME}\n"
        "#\n"
        "# `start` must never block or fail Batocera's boot sequence — it only\n"
        "# triggers a detached background worker (see `romcloud mount boot-start`)\n"
        "# and always reports success here, regardless of whether the mount\n"
        "# ultimately succeeds. ROMCloud may fail; Batocera must not.\n"
        "set -uo pipefail\n"
        "\n"
        f'export ROMCLOUD_BIN="{romcloud_bin}"\n'
        f'export ROMCLOUD_CONFIG="{romcloud_home / "config" / "romcloud.toml"}"\n'
        f'STARTUP_LOG="{romcloud_home / "logs" / "startup-service.log"}"\n'
        "\n"
        'case "${1:-}" in\n'
        "    start)\n"
        '        mkdir -p "$(dirname "${STARTUP_LOG}")" 2>/dev/null || true\n'
        "        {\n"
        '            ROMCLOUD_BOOT_ID="$(cat /proc/sys/kernel/random/boot_id 2>/dev/null || true)"\n'
        '            ROMCLOUD_UPTIME="$(cut -d " " -f 1 /proc/uptime 2>/dev/null || true)"\n'
        '            printf \'boot_id=%s uptime=%s pid=%s parent_pid=%s event=start '
        'romcloud_bin=%s config=%s\\n\' "${ROMCLOUD_BOOT_ID}" "${ROMCLOUD_UPTIME}" '
        '"$$" "${PPID}" "${ROMCLOUD_BIN}" "${ROMCLOUD_CONFIG}"\n'
        '            "${ROMCLOUD_BIN}" mount boot-start\n'
        "            ROMCLOUD_MOUNT_RC=$?\n"
        '            printf \'event=mount_boot_start_complete status=%s\\n\' "${ROMCLOUD_MOUNT_RC}"\n'
        '            if "${ROMCLOUD_BIN}" uidata manager-boot-start; then\n'
        "                printf 'event=manager_boot_start_complete status=0\\n'\n"
        "            else\n"
        "                ROMCLOUD_MANAGER_RC=$?\n"
        '                printf \'event=manager_boot_start_failed status=%s\\n\' "${ROMCLOUD_MANAGER_RC}"\n'
        "            fi\n"
        '        } >> "${STARTUP_LOG}" 2>&1\n'
        "        exit 0\n"
        "        ;;\n"
        "    stop)\n"
        '        mkdir -p "$(dirname "${STARTUP_LOG}")" 2>/dev/null || true\n'
        "        {\n"
        '            printf \'event=stop pid=%s parent_pid=%s\\n\' "$$" "${PPID}"\n'
        '            "${ROMCLOUD_BIN}" uidata manager-stop || true\n'
        '            "${ROMCLOUD_BIN}" mount stop --shutdown || true\n'
        '        } >> "${STARTUP_LOG}" 2>&1\n'
        "        exit 0\n"
        "        ;;\n"
        "    status)\n"
        '        exec "${ROMCLOUD_BIN}" mount status\n'
        "        ;;\n"
        "    *)\n"
        '        echo "Usage: $0 {start|stop|status}" >&2\n'
        "        exit 1\n"
        "        ;;\n"
        "esac\n"
    )


def install_service(
    romcloud_bin: str,
    *,
    service_path: Path = SERVICE_SCRIPT_PATH,
    activation_state_path: Path | None = None,
    services_config_path: Path | None = None,
) -> Path:
    """Write the service script (mode 0755) and try to enable it.

    Enabling via ``batocera-services`` is best-effort: if the binary isn't
    present (e.g. running this in a dev environment, or on a Batocera
    version where the mechanism differs) this logs a warning instead of
    failing the whole install — the script is still written and can be
    enabled manually.
    """
    services_config_path = services_config_path or SYSTEM_CONFIG_PATH
    generated = generate_service_script(romcloud_bin)
    try:
        previous = service_path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        previous = None
    changed = previous != generated
    was_enabled = _service_is_enabled(services_config_path)

    service_path.parent.mkdir(parents=True, exist_ok=True)
    if changed:
        temporary = service_path.with_name(f".{service_path.name}.tmp")
        temporary.write_text(generated, encoding="utf-8")
        temporary.chmod(0o755)
        temporary.replace(service_path)
    service_path.chmod(0o755)
    if changed:
        log.info("Wrote service script: %s", service_path)
    else:
        log.debug("Service script already current: %s", service_path)

    # If an old legacy service file exists and appears to be the
    # ROMCloud-generated script, remove it so Batocera doesn't warn about
    # invalid service names. Only remove when the file content matches the
    # expected ROMCloud header to avoid touching unrelated files.
    if LEGACY_SERVICE_PATH.exists():
        try:
            content = LEGACY_SERVICE_PATH.read_text(encoding="utf-8")
        except Exception:
            content = ""

        if "ROMCloud SMB source mount" in content or "romcloud mount boot-start" in content:
            try:
                LEGACY_SERVICE_PATH.unlink()
                log.info("Removed legacy service script: %s", LEGACY_SERVICE_PATH)
            except Exception:
                log.warning(
                    "Failed to remove legacy service script: %s",
                    LEGACY_SERVICE_PATH,
                )

    enabled = was_enabled
    if not was_enabled:
        try:
            result = subprocess.run(
                ["batocera-services", "enable", SERVICE_NAME],
                capture_output=True,
                text=True,
                check=False,
                timeout=10.0,
            )
            enabled = result.returncode == 0 and _service_is_enabled(
                services_config_path
            )
            if not enabled:
                detail = (result.stderr or result.stdout or "no error output").strip()
                log.warning(
                    "Batocera did not persist enablement for %s: %s",
                    SERVICE_NAME,
                    detail,
                )
        except FileNotFoundError:
            log.warning(
                "batocera-services not found — enable manually: "
                "batocera-services enable %s",
                SERVICE_NAME,
            )
        except subprocess.TimeoutExpired:
            log.warning(
                "Timed out enabling %s; enable it manually after Batocera services recover",
                SERVICE_NAME,
            )

    if activation_state_path is not None and enabled and (changed or not was_enabled):
        startup_activation.mark_restart_required(activation_state_path)
        log.info(
            "Startup integration changed; restart required before future-boot "
            "availability is considered active"
        )

    return service_path


def remove_service(*, service_path: Path = SERVICE_SCRIPT_PATH) -> bool:
    """Disable (best-effort) and delete the service script.

    Never touches any other file under /userdata/system/services/.
    """
    try:
        subprocess.run(
            ["batocera-services", "disable", SERVICE_NAME],
            capture_output=True,
            text=True,
            check=False,
            timeout=10.0,
        )
    except FileNotFoundError:
        pass
    except subprocess.TimeoutExpired:
        log.warning("Timed out disabling Batocera service %s", SERVICE_NAME)

    removed = False
    if service_path.exists():
        try:
            service_path.unlink()
            log.info("Removed service script: %s", service_path)
            removed = True
        except Exception:
            log.warning("Failed to remove service script: %s", service_path)

    # Also remove legacy path if it's the ROMCloud-owned script.
    if LEGACY_SERVICE_PATH.exists():
        try:
            content = LEGACY_SERVICE_PATH.read_text(encoding="utf-8")
        except Exception:
            content = ""

        if "ROMCloud SMB source mount" in content or "romcloud mount boot-start" in content:
            try:
                LEGACY_SERVICE_PATH.unlink()
                log.info("Removed legacy service script: %s", LEGACY_SERVICE_PATH)
                removed = True
            except Exception:
                log.warning("Failed to remove legacy service script: %s", LEGACY_SERVICE_PATH)

    return removed


def is_service_installed(*, service_path: Path = SERVICE_SCRIPT_PATH) -> bool:
    # Consider either the canonical or legacy service script as "installed".
    return service_path.exists() or LEGACY_SERVICE_PATH.exists()
