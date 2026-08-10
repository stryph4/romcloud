#!/usr/bin/env bash
# Thin, git-free ROMCloud bootstrap for Batocera.

set -euo pipefail

REPO="stryph4/romcloud"
REF="${ROMCLOUD_REF:-main}"
USERDATA_DIR="${ROMCLOUD_USERDATA:-/userdata}"
TMP_DIR=""

log() {
    printf 'ROMCloud: %s\n' "$*"
}

fail() {
    printf 'ROMCloud: ERROR: %s\n' "$*" >&2
    exit 1
}

cleanup() {
    if [[ -n "${TMP_DIR}" && -d "${TMP_DIR}" ]]; then
        rm -rf -- "${TMP_DIR}"
    fi
}

interrupted() {
    log "Installation interrupted." >&2
    exit 130
}

trap cleanup EXIT
trap interrupted INT HUP TERM

for dependency in curl tar; do
    command -v "${dependency}" >/dev/null 2>&1 || fail "missing required command: ${dependency}"
done

[[ -d "${USERDATA_DIR}" ]] || fail "${USERDATA_DIR} does not exist; this does not look like Batocera."
[[ -w "${USERDATA_DIR}" ]] || fail "${USERDATA_DIR} is not writable."

# GitHub refs may contain slashes, but not traversal components or URL syntax.
case "${REF}" in
    ""|/*|*/|*..*|*[^A-Za-z0-9._/-]*)
        fail "invalid ROMCLOUD_REF: ${REF}"
        ;;
esac

TMP_DIR="$(mktemp -d "${TMPDIR:-/tmp}/romcloud-bootstrap.XXXXXX")" \
    || fail "could not create a temporary directory."
ARCHIVE="${TMP_DIR}/romcloud.tar.gz"
EXTRACT_DIR="${TMP_DIR}/source"
mkdir -p "${EXTRACT_DIR}"

ARCHIVE_URL="https://github.com/${REPO}/archive/${REF}.tar.gz"
log "Downloading ${REPO}@${REF} ..."
curl -fsSL --retry 2 --connect-timeout 15 "${ARCHIVE_URL}" -o "${ARCHIVE}" \
    || fail "download failed for ${REPO}@${REF}."

log "Extracting source ..."
tar -xzf "${ARCHIVE}" -C "${EXTRACT_DIR}" \
    || fail "downloaded archive could not be extracted."

shopt -s nullglob dotglob
entries=("${EXTRACT_DIR}"/*)
shopt -u nullglob dotglob
[[ ${#entries[@]} -eq 1 && -d "${entries[0]}" ]] \
    || fail "unexpected archive layout."
PROJECT_DIR="${entries[0]}"
INSTALLER="${PROJECT_DIR}/scripts/install.sh"
[[ -f "${INSTALLER}" ]] || fail "archive does not contain scripts/install.sh."

log "Running installer ..."
if [[ "${REF}" =~ ^[0-9a-fA-F]{40}$ ]]; then
    ROMCLOUD_BUILD_COMMIT="${REF}" bash "${INSTALLER}"
else
    bash "${INSTALLER}"
fi

log "Bootstrap complete."
