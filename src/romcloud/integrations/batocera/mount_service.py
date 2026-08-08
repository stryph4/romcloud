"""Batocera custom-service integration for the mounted SMB source.

Batocera's "custom services" mechanism is the standard, long-documented
extension point for running a script at boot: any executable script placed
under ``/userdata/system/services/`` that responds to ``start``/``stop``/
``status`` arguments can be registered with ``batocera-services enable
<name>``. This module generates and installs exactly one such script,
``romcloud-mount``, which just shells out to ``romcloud mount
start|stop|status`` (see :mod:`romcloud.cli.commands.mount`) so all the
actual mount logic lives in one place, testable without a Batocera install.

This module does not touch Tailscale configuration in any way — reachability
waiting (see :mod:`romcloud.infrastructure.mount`) is transport-agnostic; it
only waits for the configured SMB server:port to answer, whatever route
(LAN, VPN/Tailscale, etc.) gets it there.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from romcloud.infrastructure.logging import get_logger

log = get_logger("batocera.mount_service")

SERVICE_NAME = "romcloud-mount"
SERVICE_SCRIPT_PATH = Path(f"/userdata/system/services/{SERVICE_NAME}")


def generate_service_script(romcloud_bin: str) -> str:
    """Return the content of the `romcloud-mount` custom-service script.

    *romcloud_bin* is the absolute path to the installed `romcloud` CLI
    wrapper (see `scripts/install.sh`), which already execs the venv's own
    python — this script never needs to know about Python/venv paths itself.
    """
    return (
        "#!/bin/bash\n"
        "# ROMCloud SMB source mount — Batocera custom service.\n"
        "# Installed by `romcloud mount install`. Generated — do not edit by hand.\n"
        "#\n"
        "# One-time enablement after install:\n"
        f"#   batocera-services enable {SERVICE_NAME}\n"
        "set -euo pipefail\n"
        "\n"
        f'ROMCLOUD_BIN="{romcloud_bin}"\n'
        "\n"
        'case "${1:-}" in\n'
        "    start)\n"
        '        exec "${ROMCLOUD_BIN}" mount start\n'
        "        ;;\n"
        "    stop)\n"
        '        exec "${ROMCLOUD_BIN}" mount stop\n'
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


def install_service(romcloud_bin: str, *, service_path: Path = SERVICE_SCRIPT_PATH) -> Path:
    """Write the service script (mode 0755) and try to enable it.

    Enabling via ``batocera-services`` is best-effort: if the binary isn't
    present (e.g. running this in a dev environment, or on a Batocera
    version where the mechanism differs) this logs a warning instead of
    failing the whole install — the script is still written and can be
    enabled manually.
    """
    service_path.parent.mkdir(parents=True, exist_ok=True)
    service_path.write_text(generate_service_script(romcloud_bin), encoding="utf-8")
    service_path.chmod(0o755)
    log.info("Wrote service script: %s", service_path)

    try:
        subprocess.run(
            ["batocera-services", "enable", SERVICE_NAME],
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError:
        log.warning(
            "batocera-services not found — enable manually: "
            "batocera-services enable %s",
            SERVICE_NAME,
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
        )
    except FileNotFoundError:
        pass

    if not service_path.exists():
        return False
    service_path.unlink()
    log.info("Removed service script: %s", service_path)
    return True


def is_service_installed(*, service_path: Path = SERVICE_SCRIPT_PATH) -> bool:
    return service_path.exists()
