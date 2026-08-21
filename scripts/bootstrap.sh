#!/usr/bin/env bash
# Thin, git-free ROMCloud bootstrap for Batocera.

set -euo pipefail

REPO="stryph4/romcloud"
USERDATA_DIR="${ROMCLOUD_USERDATA:-/userdata}"
TMP_DIR=""
CHANNEL="stable"

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

while [[ $# -gt 0 ]]; do
    case "$1" in
        --channel)
            [[ $# -ge 2 ]] || fail "--channel requires stable or develop."
            CHANNEL="$2"
            shift 2
            ;;
        --channel=*)
            CHANNEL="${1#*=}"
            shift
            ;;
        --help|-h)
            printf 'Usage: bootstrap.sh [--channel stable|develop]\n'
            exit 0
            ;;
        *)
            fail "unknown argument: $1"
            ;;
    esac
done

case "${CHANNEL}" in
    stable) REF="main" ;;
    develop) REF="develop" ;;
    *) fail "invalid channel '${CHANNEL}'; expected stable or develop." ;;
esac

for dependency in curl tar python3; do
    command -v "${dependency}" >/dev/null 2>&1 || fail "missing required command: ${dependency}"
done

[[ -d "${USERDATA_DIR}" ]] || fail "${USERDATA_DIR} does not exist; this does not look like Batocera."
[[ -w "${USERDATA_DIR}" ]] || fail "${USERDATA_DIR} is not writable."

TMP_DIR="$(mktemp -d "${TMPDIR:-/tmp}/romcloud-bootstrap.XXXXXX")" \
    || fail "could not create a temporary directory."
ARCHIVE="${TMP_DIR}/romcloud.tar.gz"
COMMIT_JSON="${TMP_DIR}/commit.json"
EXTRACT_DIR="${TMP_DIR}/source"
mkdir -p "${EXTRACT_DIR}"

# Resolve once, then download the archive for that immutable commit. The
# installer, support payload, and recorded revision therefore cannot drift.
COMMIT_URL="https://api.github.com/repos/${REPO}/commits/${REF}"
curl -fsSL --retry 2 --connect-timeout 15 --max-time 45 \
    "${COMMIT_URL}" -o "${COMMIT_JSON}" \
    || fail "could not resolve the ${CHANNEL} update channel."
COMMIT="$(python3 -c 'import json, sys; print(json.load(open(sys.argv[1], encoding="utf-8"))["sha"])' "${COMMIT_JSON}")" \
    || fail "invalid update metadata for the ${CHANNEL} channel."
case "${COMMIT}" in
    ""|*[!0-9a-fA-F]*) fail "invalid commit identity for the ${CHANNEL} channel." ;;
esac
[[ ${#COMMIT} -eq 40 ]] || fail "invalid commit identity for the ${CHANNEL} channel."

ARCHIVE_URL="https://github.com/${REPO}/archive/${COMMIT}.tar.gz"
log "Downloading ${REPO} ${CHANNEL}@${COMMIT:0:12} ..."
curl -fsSL --retry 2 --connect-timeout 15 --max-time 180 \
    "${ARCHIVE_URL}" -o "${ARCHIVE}" \
    || fail "download failed for ${REPO} ${CHANNEL}@${COMMIT:0:12}."

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
ROMCLOUD_BUILD_COMMIT="${COMMIT}" \
ROMCLOUD_UPDATE_CHANNEL="${CHANNEL}" \
    bash "${INSTALLER}" --channel "${CHANNEL}"

log "Bootstrap complete."
