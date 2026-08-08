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

VENV_DIR="${ROMCLOUD_HOME}/venv"
BIN_DIR="${ROMCLOUD_HOME}/bin"
CONFIG_DIR="${ROMCLOUD_HOME}/config"
DATA_DIR="${ROMCLOUD_HOME}/data"
LOGS_DIR="${ROMCLOUD_HOME}/logs"

# ── parse args ────────────────────────────────────────────────────────────────
for arg in "$@"; do
    case "$arg" in
        --prefix=*)
            ROMCLOUD_HOME="${arg#*=}"
            VENV_DIR="${ROMCLOUD_HOME}/venv"
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
# Only python3 is required globally. ROMCloud (and pip itself, if needed) live
# entirely inside a private virtual environment — nothing is installed into
# Batocera's system Python, and no global pip3 is required.
if ! command -v python3 &>/dev/null; then
    echo "ERROR: missing required dependency: python3" >&2
    echo "Install python3, then re-run this installer." >&2
    exit 1
fi

# ── create directory structure ────────────────────────────────────────────────
mkdir -p "${BIN_DIR}" "${CONFIG_DIR}" "${DATA_DIR}" "${LOGS_DIR}"
mkdir -p "${CACHE_ROOT}/.partial"

echo "  Created directory structure."

# ── create isolated virtual environment ───────────────────────────────────────
# Idempotent: reuse an existing venv rather than recreating it, so re-running
# the installer doesn't discard whatever is already installed.
if [[ -x "${VENV_DIR}/bin/python" ]]; then
    echo "  Virtual environment already exists at ${VENV_DIR} — reusing."
else
    if ! python3 -m venv "${VENV_DIR}"; then
        echo "ERROR: failed to create virtual environment at ${VENV_DIR}" >&2
        exit 1
    fi
    echo "  Created virtual environment: ${VENV_DIR}"
fi

# Some python3 -m venv builds (observed on Batocera 42) don't ship pip because
# there is no global pip3 to seed them with. Bootstrap it with the venv's own
# Python if it's missing.
if ! "${VENV_DIR}/bin/python" -m pip --version &>/dev/null; then
    if ! "${VENV_DIR}/bin/python" -m ensurepip --upgrade; then
        echo "ERROR: failed to bootstrap pip inside ${VENV_DIR}" >&2
        exit 1
    fi
    echo "  Bootstrapped pip inside the virtual environment."
fi

# ── install Python package ────────────────────────────────────────────────────
# Installed with the venv's own python -m pip — never the system pip/python.
if ! "${VENV_DIR}/bin/python" -m pip install --upgrade --quiet "${PROJECT_DIR}"; then
    echo "ERROR: failed to install ROMCloud into ${VENV_DIR}" >&2
    exit 1
fi
echo "  Installed ROMCloud into ${VENV_DIR}."

# ── write version file ────────────────────────────────────────────────────────
if [[ -f "${PROJECT_DIR}/version.json" ]]; then
    cp "${PROJECT_DIR}/version.json" "${ROMCLOUD_HOME}/version.json"
fi

# ── write CLI wrapper ─────────────────────────────────────────────────────────
# Built with printf (not an unquoted heredoc) so runtime-only tokens such as
# "$@" are never subject to expansion by the installer's own shell — only the
# venv python path is substituted, via %s. Execs the venv's python directly,
# so ROMCloud never depends on (or touches) Batocera's system Python.
{
    printf '#!/bin/bash\n'
    printf 'exec "%s" -m romcloud.cli.main "$@"\n' "${VENV_DIR}/bin/python"
} > "${BIN_DIR}/romcloud"
chmod +x "${BIN_DIR}/romcloud"
echo "  Wrote CLI wrapper: ${BIN_DIR}/romcloud"

# ── write Batocera launch wrapper ─────────────────────────────────────────────
# The wrapper is a Python script that receives the exact argv EmulationStation
# would pass to emulatorlauncher and handles .romcloud interception. Its
# shebang points directly at the venv's python (substituted via printf, the
# only non-literal line); ROMCloud is already importable there, so no
# PYTHONPATH/sys.path hacks are needed.
# Verified command format on Batocera 42:
#   emulatorlauncher %CONTROLLERSCONFIG% -system %SYSTEM% -rom %ROM% \
#       -gameinfoxml %GAMEINFOXML% -systemname %SYSTEMNAME%
{
    printf '#!%s\n' "${VENV_DIR}/bin/python"
    cat << 'LAUNCHER'
"""romcloud-run — Batocera 42+ EmulationStation launch wrapper.

Receives the exact argv that EmulationStation would pass to emulatorlauncher.

  - Non-.romcloud ROM:  exec emulatorlauncher with original argv unchanged.
  - .romcloud proxy:    resolve/cache the real ROM, replace only the -rom
                        value, exec emulatorlauncher with all other args intact.

Example <command> for es_systems.cfg:
    /userdata/system/romcloud/bin/romcloud-run %CONTROLLERSCONFIG% -system %SYSTEM% -rom %ROM% -gameinfoxml %GAMEINFOXML% -systemname %SYSTEMNAME%
"""
import sys as _sys

from romcloud.integrations.batocera.launcher import run_launcher_wrapper

run_launcher_wrapper(_sys.argv)
LAUNCHER
} > "${BIN_DIR}/romcloud-run"
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

# ── summary ───────────────────────────────────────────────────────────────────
# Deliberately does NOT modify /userdata/system/custom.sh. custom.sh is
# foreign Batocera/user-addon startup state (real-hardware testing showed it
# sources a large number of unrelated startup scripts and is not safe for
# ROMCloud to append to, source, or regenerate). The installed CLI is not on
# PATH by default — call it by its full path, shown below, or add it to your
# own shell profile if you want it on PATH.
echo ""
echo "ROMCloud installed successfully."
echo ""
echo "  CLI:       ${BIN_DIR}/romcloud"
echo "  Config:    ${CONFIG_FILE}"
echo "  Cache:     ${CACHE_ROOT}"
echo "  Catalog:   ${DATA_DIR}/catalog.db"
echo ""
echo "Note: ${BIN_DIR} was NOT added to PATH (ROMCloud never modifies"
echo "      /userdata/system/custom.sh or other Batocera startup files)."
echo "      Call the CLI by its full path, e.g.:"
echo "        ${BIN_DIR}/romcloud healthcheck"
echo ""
echo "Next steps:"
echo "  1. Edit ${CONFIG_FILE} to point at your ROM source."
echo "     (Or run: ${BIN_DIR}/romcloud configure)"
echo "  2. Run:   ${BIN_DIR}/romcloud healthcheck"
echo "  3. Run:   ${BIN_DIR}/romcloud refresh"
echo ""
echo "Batocera / EmulationStation integration:"
echo "  SPIKE — see src/romcloud/integrations/batocera/es_config.py"
echo "  Run: ${BIN_DIR}/romcloud healthcheck  (includes integration status)"
