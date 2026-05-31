#!/usr/bin/env bash
set -euo pipefail
IFS=$'\n\t'

usage() {
    cat <<'USAGE'
Usage: install/verify-release.sh SHA256SUMS ARTIFACT [ARTIFACT ...]

Verify one or more downloaded release artifacts against a SHA256SUMS file before
unpacking or installing them.
USAGE
}

if [[ $# -lt 2 ]]; then
    usage >&2
    exit 2
fi

CHECKSUM_FILE="$1"
shift

if [[ ! -f "$CHECKSUM_FILE" ]]; then
    echo "ERROR: checksum file not found: ${CHECKSUM_FILE}" >&2
    exit 1
fi

CHECKSUM_DIR="$(cd "$(dirname "$CHECKSUM_FILE")" && pwd)"
TMP_FILE="$(mktemp)"
trap 'rm -f "$TMP_FILE"' EXIT

for artifact in "$@"; do
    if [[ ! -f "$artifact" ]]; then
        echo "ERROR: artifact not found: ${artifact}" >&2
        exit 1
    fi

    artifact_dir="$(cd "$(dirname "$artifact")" && pwd)"
    artifact_name="$(basename "$artifact")"
    if [[ "$artifact_dir" != "$CHECKSUM_DIR" ]]; then
        echo "ERROR: artifact must be in the same directory as SHA256SUMS: ${artifact}" >&2
        exit 1
    fi

    if ! awk -v name="$artifact_name" '$2 == name { print }' "$CHECKSUM_FILE" >> "$TMP_FILE"; then
        echo "ERROR: failed to read ${CHECKSUM_FILE}" >&2
        exit 1
    fi
    if ! awk -v name="$artifact_name" '$2 == name { found = 1 } END { exit found ? 0 : 1 }' "$CHECKSUM_FILE"; then
        echo "ERROR: checksum entry not found for ${artifact_name}" >&2
        exit 1
    fi
done

(cd "$CHECKSUM_DIR" && shasum -a 256 -c "$TMP_FILE")
