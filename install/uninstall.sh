#!/usr/bin/env bash
set -euo pipefail
IFS=$'\n\t'

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
YES=false

while [[ $# -gt 0 ]]; do
    case "$1" in
        --yes)
            YES=true
            shift
            ;;
        -h|--help)
            echo "Usage: ./uninstall.sh [--yes]"
            exit 0
            ;;
        *)
            echo "Unknown option: $1" >&2
            exit 2
            ;;
    esac
done

if [[ ! -f "${ROOT_DIR}/.vocalize-install-root" ]]; then
    echo "ERROR: refusing to uninstall unmarked directory: ${ROOT_DIR}" >&2
    exit 1
fi

if [[ "$YES" != true ]]; then
    printf "Remove %s? Type 'yes' to continue: " "$ROOT_DIR"
    read -r answer
    normalized="$(printf '%s' "$answer" | tr '[:upper:]' '[:lower:]')"
    if [[ "$normalized" != "yes" ]]; then
        echo "Cancelled"
        exit 1
    fi
fi

if [[ -f "${ROOT_DIR}/config/install.json" ]]; then
    symlink="$(
        sed -n 's/.*"global_symlink": "\([^"]*\)".*/\1/p' "${ROOT_DIR}/config/install.json" | head -1
    )"
    if [[ -n "${symlink:-}" && -L "$symlink" ]]; then
        target="$(readlink "$symlink")"
        if [[ "$target" == "${ROOT_DIR}/vocalize" ]]; then
            rm -f "$symlink"
        fi
    fi
fi

cd "$(dirname "$ROOT_DIR")"
rm -rf "$ROOT_DIR"
echo "Removed: ${ROOT_DIR}"
