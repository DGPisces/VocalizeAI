#!/usr/bin/env bash
set -euo pipefail
IFS=$'\n\t'

INSTALL_DIR="${PWD}/VocalizeAI"
ARTIFACT=""
CHECKSUMS=""
YES=false
DRY_RUN=false

verify_artifact_checksum() {
    local checksums="$1"
    local artifact="$2"

    if [[ ! -f "$checksums" ]]; then
        echo "ERROR: checksum file not found: ${checksums}" >&2
        exit 1
    fi

    local checksum_dir artifact_dir artifact_name tmp_file
    checksum_dir="$(cd "$(dirname "$checksums")" && pwd)"
    artifact_dir="$(cd "$(dirname "$artifact")" && pwd)"
    artifact_name="$(basename "$artifact")"
    if [[ "$artifact_dir" != "$checksum_dir" ]]; then
        echo "ERROR: artifact must be in the same directory as SHA256SUMS: ${artifact}" >&2
        exit 1
    fi

    tmp_file="$(mktemp)"
    if ! awk -v name="$artifact_name" '$2 == name { print }' "$checksums" > "$tmp_file"; then
        rm -f "$tmp_file"
        echo "ERROR: failed to read ${checksums}" >&2
        exit 1
    fi
    if [[ ! -s "$tmp_file" ]]; then
        rm -f "$tmp_file"
        echo "ERROR: checksum entry not found for ${artifact_name}" >&2
        exit 1
    fi

    (cd "$checksum_dir" && shasum -a 256 -c "$tmp_file")
    rm -f "$tmp_file"
}

usage() {
    cat <<'USAGE'
Usage: install/install.sh --artifact RELEASE.zip [options]

Install VocalizeAI into ./VocalizeAI by default.

Options:
  --artifact PATH       Local VocalizeAI macOS release zip.
  --checksums PATH      SHA256SUMS file used to verify the artifact.
  --install-dir PATH    Destination directory. Default: ./VocalizeAI.
  --yes                Do not prompt before installing.
  --dry-run            Print actions without changing files.
USAGE
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --artifact)
            ARTIFACT="$2"
            shift 2
            ;;
        --checksums)
            CHECKSUMS="$2"
            shift 2
            ;;
        --install-dir)
            INSTALL_DIR="$2"
            shift 2
            ;;
        --yes)
            YES=true
            shift
            ;;
        --dry-run)
            DRY_RUN=true
            shift
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            echo "Unknown option: $1" >&2
            usage >&2
            exit 2
            ;;
    esac
done

if [[ -z "$ARTIFACT" ]]; then
    echo "ERROR: --artifact is required" >&2
    usage >&2
    exit 2
fi

if [[ ! -f "$ARTIFACT" ]]; then
    echo "ERROR: artifact not found: ${ARTIFACT}" >&2
    exit 1
fi

if [[ -e "$INSTALL_DIR" ]]; then
    if [[ ! -f "${INSTALL_DIR}/.vocalize-install-root" ]]; then
        echo "ERROR: destination exists but is not a VocalizeAI install: ${INSTALL_DIR}" >&2
        exit 1
    fi
    if [[ -n "$(find "$INSTALL_DIR" -mindepth 1 -maxdepth 1 ! -name config ! -name logs ! -name cache ! -name .vocalize-install-root -print -quit)" ]]; then
        echo "ERROR: destination already contains an install. Use ./vocalize update instead." >&2
        exit 1
    fi
fi

if [[ "$YES" != true ]]; then
    printf "Install VocalizeAI into %s? Type 'yes' to continue: " "$INSTALL_DIR"
    read -r answer
    normalized="$(printf '%s' "$answer" | tr '[:upper:]' '[:lower:]')"
    if [[ "$normalized" != "yes" ]]; then
        echo "Cancelled"
        exit 1
    fi
fi

if [[ -n "$CHECKSUMS" ]]; then
    if [[ "$DRY_RUN" == true ]]; then
        echo "[DRY] verify ${ARTIFACT} against ${CHECKSUMS}"
    else
        verify_artifact_checksum "$CHECKSUMS" "$ARTIFACT"
    fi
fi

TMP_DIR="$(mktemp -d)"
cleanup() {
    rm -rf "$TMP_DIR"
}
trap cleanup EXIT

if [[ "$DRY_RUN" == true ]]; then
    echo "[DRY] unzip ${ARTIFACT}"
    echo "[DRY] install into ${INSTALL_DIR}"
    exit 0
fi

unzip -q "$ARTIFACT" -d "$TMP_DIR"
bundle_count="$(
    find "$TMP_DIR" -mindepth 1 -maxdepth 1 -type d ! -name __MACOSX | wc -l | tr -d ' '
)"
if [[ "$bundle_count" != "1" ]]; then
    echo "ERROR: release artifact must contain exactly one top-level directory" >&2
    exit 1
fi
BUNDLE_DIR="$(find "$TMP_DIR" -mindepth 1 -maxdepth 1 -type d ! -name __MACOSX | head -1)"

mkdir -p "$INSTALL_DIR"
rsync -a --delete \
    --exclude config/.env \
    --exclude config/preferences.json \
    --exclude config/install.json \
    --exclude logs/ \
    --exclude cache/ \
    "${BUNDLE_DIR}/" "${INSTALL_DIR}/"

mkdir -p "${INSTALL_DIR}/config" "${INSTALL_DIR}/logs" "${INSTALL_DIR}/cache"
printf 'VocalizeAI local install\n' > "${INSTALL_DIR}/.vocalize-install-root"
chmod 755 "${INSTALL_DIR}/vocalize" "${INSTALL_DIR}/bin/vocalize" "${INSTALL_DIR}/uninstall.sh"

echo "Installed: ${INSTALL_DIR}"
echo ""
echo "Next:"
echo "  cd '${INSTALL_DIR}'"
echo "  ./vocalize setup"
echo "  ./vocalize doctor"
echo "  ./vocalize start"
