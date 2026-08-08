#!/usr/bin/env bash
# ROMCloud installer
#
# Idempotent — safe to re-run. Never:
#   - Resets existing configuration
#   - Deletes the catalog database
#   - Removes cached ROMs
#   - Overwrites the cache directory
#
# Usage:
#   bash scripts/install.sh [--prefix PATH]
#
# Defaults to Batocera layout under /userdata.

set -euo pipefail

# ── defaults ──────────────────────────────────────────────────────────────────
ROMCLOUD_HOME="${ROMCLOUD_HOME:-/userdata/system/romcloud}"
CACHE_ROOT="${CACHE_ROOT:-/userdata/romcloud-cache}"
LOCAL_ROMS="${LOCAL_ROMS:-/userdata/roms}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"

APP_DIR="${ROMCLOUD_HOME}/app"
BIN_DIR="${ROMCLOUD_HOME}/bin"
CONFIG_DIR="${ROMCLOUD_HOME}/config"
DATA_DIR="${ROMCLOUD_HOME}/data"
LOGS_DIR="${ROMCLOUD_HOME}/logs"

# ── parse args ────────────────────────────────────────────────────────────────
for arg in "$@"; do
    case "$arg" in
        --prefix=*)
            ROMCLOUD_HOME="${arg#*=}"
            APP_DIR="${ROMCLOUD_HOME}/app"
            BIN_DIR="${ROMCLOUD_HOME}/bin"
            CONFIG_DIR="${ROMCLOUD_HOME}/config"
            DATA_DIR="${ROMCLOUD_HOME}/data"
            LOGS_DIR="${ROMCLOUD_HOME}/logs"
            ;;
        --help|-h)
            echo "Usage: bash install.sh [--prefix=PATH]"
            exit 0
            ;;
    esac
done

echo "Installing ROMCloud to ${ROMCLOUD_HOME} ..."

# ── verify required dependencies ──────────────────────────────────────────────
MISSING_DEPS=()
command -v python3 &>/dev/null || MISSING_DEPS+=("python3")
command -v pip3 &>/dev/null || MISSING_DEPS+=("pip3")
if [[ ${#MISSING_DEPS[@]} -gt 0 ]]; then
    echo "ERROR: missing required dependencies: ${MISSING_DEPS[*]}" >&2
    echo "Install python3 and pip3, then re-run this installer." >&2
    exit 1
fi

# ── create directory structure ────────────────────────────────────────────────
mkdir -p "${APP_DIR}" "${BIN_DIR}" "${CONFIG_DIR}" "${DATA_DIR}" "${LOGS_DIR}"
mkdir -p "${CACHE_ROOT}/.partial"

echo "  Created directory structure."

# ── install Python package ────────────────────────────────────────────────────
pip3 install --target="${APP_DIR}" --upgrade --quiet "${PROJECT_DIR}"
echo "  Installed Python package to ${APP_DIR}."

# ── write version file ────────────────────────────────────────────────────────
if [[ -f "${PROJECT_DIR}/version.json" ]]; then
    cp "${PROJECT_DIR}/version.json" "${ROMCLOUD_HOME}/version.json"
fi

# ── write CLI wrapper ─────────────────────────────────────────────────────────
# Built with printf (not an unquoted heredoc) so runtime-only tokens such as
# "$@" and "${PYTHONPATH:-}" are never subject to expansion by the installer's
# own shell — only APP_DIR is substituted, via %s.
{
    printf '#!/bin/bash\n'
    printf 'export PYTHONPATH="%s:${PYTHONPATH:-}"\n' "${APP_DIR}"
    printf 'exec python3 -m romcloud.cli.main "$@"\n'
} > "${BIN_DIR}/romcloud"
chmod +x "${BIN_DIR}/romcloud"
echo "  Wrote CLI wrapper: ${BIN_DIR}/romcloud"

# ── write Batocera launch wrapper ─────────────────────────────────────────────
# The wrapper is a Python script that receives the exact argv EmulationStation
# would pass to emulatorlauncher and handles .romcloud interception.
# Verified command format on Batocera 42:
#   emulatorlauncher %CONTROLLERSCONFIG% -system %SYSTEM% -rom %ROM% \
#       -gameinfoxml %GAMEINFOXML% -systemname %SYSTEMNAME%
cat > "${BIN_DIR}/romcloud-run" << 'LAUNCHER'
#!/usr/bin/env python3
"""romcloud-run — Batocera 42+ EmulationStation launch wrapper.

Receives the exact argv that EmulationStation would pass to emulatorlauncher.

  - Non-.romcloud ROM:  exec emulatorlauncher with original argv unchanged.
  - .romcloud proxy:    resolve/cache the real ROM, replace only the -rom
                        value, exec emulatorlauncher with all other args intact.

Example <command> for es_systems.cfg:
    /userdata/system/romcloud/bin/romcloud-run %CONTROLLERSCONFIG% -system %SYSTEM% -rom %ROM% -gameinfoxml %GAMEINFOXML% -systemname %SYSTEMNAME%
"""
import sys as _sys

_APP = "/userdata/system/romcloud/app"
if _APP not in _sys.path:
    _sys.path.insert(0, _APP)

from romcloud.integrations.batocera.launcher import run_launcher_wrapper

run_launcher_wrapper(_sys.argv)
LAUNCHER
chmod +x "${BIN_DIR}/romcloud-run"
echo "  Wrote launch wrapper: ${BIN_DIR}/romcloud-run"

# ── write default config (only if none exists) ────────────────────────────────
CONFIG_FILE="${CONFIG_DIR}/romcloud.toml"
if [[ ! -f "${CONFIG_FILE}" ]]; then
    cat > "${CONFIG_FILE}" << CONFIG
# ROMCloud configuration
# Edit this file or run: romcloud configure

[source]
provider = "local"
rom_root = "/mnt/rom-source/ROMs"

[cache]
path = "${CACHE_ROOT}"
max_size_gb = 50.0
min_free_gb = 5.0

[local_roms]
path = "${LOCAL_ROMS}"

[data]
path = "${DATA_DIR}"

[logging]
level = "INFO"
path = "${LOGS_DIR}"
CONFIG
    echo "  Wrote default config: ${CONFIG_FILE}"
    echo "  Edit it or run: romcloud configure"
else
    echo "  Config already exists at ${CONFIG_FILE} — not overwritten."
fi

# ── add to PATH on Batocera (via custom.sh) ───────────────────────────────────
CUSTOM_SH="${CUSTOM_SH:-/userdata/system/custom.sh}"
if [[ -f "${CUSTOM_SH}" ]] || [[ -d "$(dirname "${CUSTOM_SH}")" ]]; then
    if ! grep -qF "romcloud/bin" "${CUSTOM_SH}" 2>/dev/null; then
        # Single-quoted format string: "$PATH" is written literally so it is
        # expanded at custom.sh runtime, not by the installer.
        printf 'export PATH="%s:$PATH"\n' "${BIN_DIR}" >> "${CUSTOM_SH}"
        echo "  Added ${BIN_DIR} to PATH in ${CUSTOM_SH}"
    else
        echo "  PATH entry already present in ${CUSTOM_SH}."
    fi
fi

# ── summary ───────────────────────────────────────────────────────────────────
echo ""
echo "ROMCloud installed successfully."
echo ""
echo "  CLI:       ${BIN_DIR}/romcloud"
echo "  Config:    ${CONFIG_FILE}"
echo "  Cache:     ${CACHE_ROOT}"
echo "  Catalog:   ${DATA_DIR}/catalog.db"
echo ""
echo "Next steps:"
echo "  1. Edit ${CONFIG_FILE} to point at your ROM source."
echo "     (Or run: romcloud configure)"
echo "  2. Run:   romcloud healthcheck"
echo "  3. Run:   romcloud refresh"
echo ""
echo "Batocera / EmulationStation integration:"
echo "  SPIKE — see src/romcloud/integrations/batocera/es_config.py"
echo "  Run: romcloud healthcheck  (includes integration status)"
