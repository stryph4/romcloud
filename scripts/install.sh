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
# Where Batocera's EmulationStation looks for Ports scripts. Overridable so
# tests can point it at a throwaway directory instead of the real
# /userdata/roms/ports.
ROMCLOUD_PORTS_DIR="${ROMCLOUD_PORTS_DIR:-/userdata/roms/ports}"
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
# Persist build metadata without inventing a commit SHA. Prefer explicit
# installer input, then git when available, then the previously installed
# metadata when this is a same-version reinstall/repair. If none of those
# sources are available, the commit stays unknown.
ROMCLOUD_BUILD_COMMIT="${ROMCLOUD_BUILD_COMMIT:-}"
ROMCLOUD_HOME="${ROMCLOUD_HOME}" PROJECT_DIR="${PROJECT_DIR}" ROMCLOUD_BUILD_COMMIT="${ROMCLOUD_BUILD_COMMIT}" "${VENV_DIR}/bin/python" <<'PY'
import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import romcloud

project_dir = Path(os.environ["PROJECT_DIR"])
romcloud_home = Path(os.environ["ROMCLOUD_HOME"])
explicit_commit = os.environ.get("ROMCLOUD_BUILD_COMMIT") or None
version = romcloud.__version__


def _read_commit(path: Path) -> str | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None

    commit = data.get("commit")
    if isinstance(commit, str) and commit:
        return commit
    return None


def _is_commit_sha(value: str | None) -> bool:
    return bool(value) and len(value) == 40 and all(ch in "0123456789abcdefABCDEF" for ch in value)


def _git_commit() -> str | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(project_dir), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return None

    commit = result.stdout.strip()
    return commit if _is_commit_sha(commit) else None


existing_info = None
existing_path = romcloud_home / "version.json"
if existing_path.exists():
    try:
        existing_info = json.loads(existing_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        existing_info = None

commit = None
source = "installer:unknown"

if _is_commit_sha(explicit_commit):
    commit = explicit_commit
    source = "installer:explicit"
else:
    git_commit = _git_commit()
    if git_commit is not None:
        commit = git_commit
        source = "installer:git"
    else:
        project_commit = _read_commit(project_dir / "version.json")
        if _is_commit_sha(project_commit):
            commit = project_commit
            source = "installer:source-metadata"
        elif isinstance(existing_info, dict) and existing_info.get("version") == version:
            preserved_commit = existing_info.get("commit")
            if _is_commit_sha(preserved_commit):
                commit = preserved_commit
                source = "installer:preserved"

payload = {
    "version": version,
    "commit": commit,
    "commit_short": commit[:12] if commit else None,
    "build_date": datetime.now(timezone.utc).isoformat(),
    "source": source,
}

path = romcloud_home / "version.json"
path.parent.mkdir(parents=True, exist_ok=True)
tmp_path = path.with_name(f".{path.name}.tmp")
tmp_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
tmp_path.replace(path)
PY

# ── reconcile managed runtime artifacts ───────────────────────────────────────
# Writes/refreshes the `romcloud`/`romcloud-run` wrappers and the optional
# graphical Ports UI (`ports_gfx`, `romcloud-ports`, the Batocera Port entry
# script), and — only if already present from a prior install — the
# Batocera mount service script and ROMCloud's EmulationStation override.
# Implemented once in romcloud.lifecycle.install and shared with
# `romcloud update`, so a fresh install and a later self-update always
# produce byte-identical artifacts from the same source revision.
#
# The `romcloud`/`romcloud-run` wrappers are required — this step failing
# fails the whole install. Everything else (Ports UI, mount service, ES
# override) is best-effort and never fails this step: "ROMCloud may fail;
# Batocera must not".
if ! "${VENV_DIR}/bin/python" -m romcloud.cli.main _reconcile-install \
        --romcloud-home "${ROMCLOUD_HOME}" \
        --project-root "${PROJECT_DIR}" \
        --ports-dir "${ROMCLOUD_PORTS_DIR}" \
        --system-python "${ROMCLOUD_SYSTEM_PYTHON:-}"; then
    echo "ERROR: failed to write ROMCloud runtime artifacts (wrappers)" >&2
    exit 1
fi

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
echo ""
if [[ -x "${BIN_DIR}/romcloud-ports" ]]; then
    echo "Graphical Ports UI:"
    echo "  Installed — runs under Batocera's system Python."
    echo "  Launch:    ${BIN_DIR}/romcloud-ports"
else
    echo "Graphical Ports UI: not installed (no system Python with pygame found)."
    echo "  CLI/TUI features are unaffected."
fi
