#!/usr/bin/env bash
set -euo pipefail
IFS=$'\n\t'

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON_BIN="${PYTHON_BIN:-${ROOT_DIR}/.venv/bin/python}"
VERSION=""
SIGNING_MODE="${VOCALIZE_SIGNING_MODE:-skip}"
DIST_DIR="${VOCALIZE_RELEASE_DIST:-${ROOT_DIR}/dist/release}"
WORK_DIR="${VOCALIZE_RELEASE_WORK:-${ROOT_DIR}/build/release}"
SKIP_FRONTEND_BUILD=false
SKIP_SWIFT_BUILD=false

usage() {
    cat <<'USAGE'
Usage: scripts/build-macos-release.sh [options]

Build a self-contained macOS VocalizeAI release artifact.

Options:
  --version VERSION             Override pyproject.toml version.
  --signing-mode MODE           skip | ad-hoc | developer-id. Default: skip.
  --dist-dir DIR                Release asset output directory.
  --work-dir DIR                Temporary build workspace.
  --skip-frontend-build         Reuse existing frontend/dist.
  --skip-swift-build            Reuse existing Swift release helper.

Developer ID mode requires:
  APPLE_DEVELOPER_ID_APPLICATION
  APPLE_ID
  APPLE_TEAM_ID
  APPLE_APP_SPECIFIC_PASSWORD
USAGE
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --version)
            VERSION="$2"
            shift 2
            ;;
        --signing-mode)
            SIGNING_MODE="$2"
            shift 2
            ;;
        --dist-dir)
            DIST_DIR="$2"
            shift 2
            ;;
        --work-dir)
            WORK_DIR="$2"
            shift 2
            ;;
        --skip-frontend-build)
            SKIP_FRONTEND_BUILD=true
            shift
            ;;
        --skip-swift-build)
            SKIP_SWIFT_BUILD=true
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

if [[ "$(uname -s)" != "Darwin" ]]; then
    echo "ERROR: macOS release artifacts must be built on macOS." >&2
    exit 1
fi

case "$SIGNING_MODE" in
    skip|ad-hoc|developer-id) ;;
    *)
        echo "ERROR: --signing-mode must be skip, ad-hoc, or developer-id." >&2
        exit 2
        ;;
esac

if [[ ! -x "$PYTHON_BIN" ]]; then
    echo "ERROR: Python venv not found at ${PYTHON_BIN}." >&2
    echo "Run: bash install/dev-install.sh" >&2
    exit 1
fi

for command_name in npm swift ditto shasum xcrun codesign file; do
    if ! command -v "$command_name" >/dev/null 2>&1; then
        echo "ERROR: required command not found: ${command_name}" >&2
        exit 1
    fi
done

if ! "$PYTHON_BIN" -m PyInstaller --version >/dev/null 2>&1; then
    echo "ERROR: PyInstaller is not installed in the project venv." >&2
    echo "Run inside the venv: python -m pip install 'pyinstaller>=6.10,<7'" >&2
    exit 1
fi

if [[ -z "$VERSION" ]]; then
    VERSION="$(
        "$PYTHON_BIN" -m tools.release.artifacts version \
            --pyproject "${ROOT_DIR}/pyproject.toml"
    )"
fi

if [[ "$SIGNING_MODE" == "developer-id" ]]; then
    : "${APPLE_DEVELOPER_ID_APPLICATION:?developer-id signing requires APPLE_DEVELOPER_ID_APPLICATION}"
    : "${APPLE_ID:?developer-id signing requires APPLE_ID}"
    : "${APPLE_TEAM_ID:?developer-id signing requires APPLE_TEAM_ID}"
    : "${APPLE_APP_SPECIFIC_PASSWORD:?developer-id signing requires APPLE_APP_SPECIFIC_PASSWORD}"
fi

ARCH="$(uname -m)"
ARTIFACT_NAME="VocalizeAI-${VERSION}-macos-${ARCH}"
BUNDLE_DIR="${WORK_DIR}/${ARTIFACT_NAME}"
PYINSTALLER_DIST="${WORK_DIR}/pyinstaller/dist"
PYINSTALLER_WORK="${WORK_DIR}/pyinstaller/work"
HELPER_STAGE="${WORK_DIR}/staged-helper/vocalize-mac-speech-provider"
FRONTEND_DIST="${ROOT_DIR}/frontend/dist"
SWIFT_HELPER="${ROOT_DIR}/macos/VocalizeSpeechProvider/.build/release/VocalizeSpeechProvider"
ZIP_PATH="${DIST_DIR}/${ARTIFACT_NAME}.zip"
CHECKSUM_PATH="${DIST_DIR}/SHA256SUMS"
INSTALLER_ASSET="${DIST_DIR}/install.sh"

echo "=== VocalizeAI macOS release build ==="
echo "Version: ${VERSION}"
echo "Signing: ${SIGNING_MODE}"
echo "Artifact: ${ARTIFACT_NAME}"
echo ""

if [[ "$SKIP_FRONTEND_BUILD" != true ]]; then
    echo "[1/7] Building Vite frontend..."
    (cd "${ROOT_DIR}/frontend" && npm ci && npm run build)
else
    echo "[1/7] Reusing existing Vite frontend build."
fi

if [[ ! -f "${FRONTEND_DIST}/index.html" ]]; then
    echo "ERROR: frontend build missing: ${FRONTEND_DIST}/index.html" >&2
    exit 1
fi

if [[ "$SKIP_SWIFT_BUILD" != true ]]; then
    echo "[2/7] Building macOS speech provider..."
    swift build -c release --package-path "${ROOT_DIR}/macos/VocalizeSpeechProvider"
else
    echo "[2/7] Reusing existing Swift release helper."
fi

if [[ ! -f "$SWIFT_HELPER" ]]; then
    echo "ERROR: Swift helper missing: ${SWIFT_HELPER}" >&2
    exit 1
fi

rm -rf "${WORK_DIR}/staged-helper" "$PYINSTALLER_DIST" "$PYINSTALLER_WORK"
mkdir -p "$(dirname "$HELPER_STAGE")"
cp "$SWIFT_HELPER" "$HELPER_STAGE"
chmod 755 "$HELPER_STAGE"

echo "[3/7] Building PyInstaller one-folder backend..."
VOCALIZE_REPO_ROOT="$ROOT_DIR" \
VOCALIZE_FRONTEND_DIST="$FRONTEND_DIST" \
VOCALIZE_MACOS_HELPER="$HELPER_STAGE" \
    "$PYTHON_BIN" -m PyInstaller \
        --noconfirm \
        --clean \
        --distpath "$PYINSTALLER_DIST" \
        --workpath "$PYINSTALLER_WORK" \
        "${ROOT_DIR}/packaging/pyinstaller/vocalize.spec"

if [[ ! -x "${PYINSTALLER_DIST}/vocalize/vocalize" ]]; then
    echo "ERROR: PyInstaller backend missing: ${PYINSTALLER_DIST}/vocalize/vocalize" >&2
    exit 1
fi

echo "[4/7] Assembling release layout..."
rm -rf "$BUNDLE_DIR"
mkdir -p "${BUNDLE_DIR}/app" "${BUNDLE_DIR}/bin" "${BUNDLE_DIR}/config" \
    "${BUNDLE_DIR}/logs" "${BUNDLE_DIR}/cache"
cp -R "${PYINSTALLER_DIST}/vocalize" "${BUNDLE_DIR}/app/vocalize"
cp "$HELPER_STAGE" "${BUNDLE_DIR}/bin/vocalize-mac-speech-provider"
cp "${ROOT_DIR}/.env.example" "${BUNDLE_DIR}/config/.env.example"
cp "${ROOT_DIR}/install/uninstall.sh" "${BUNDLE_DIR}/uninstall.sh"
cp "${ROOT_DIR}/LICENSE" "${BUNDLE_DIR}/LICENSE"
cp "${ROOT_DIR}/README.md" "${BUNDLE_DIR}/README.md"
printf '%s\n' "$VERSION" > "${BUNDLE_DIR}/VERSION"
printf 'VocalizeAI local install\n' > "${BUNDLE_DIR}/.vocalize-install-root"

cat > "${BUNDLE_DIR}/bin/vocalize" <<'SH'
#!/usr/bin/env bash
set -euo pipefail
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export VOCALIZE_HOST="${VOCALIZE_HOST:-127.0.0.1}"
export VOCALIZE_PORT="${VOCALIZE_PORT:-8080}"
export VOCALIZE_INSTALL_ROOT="${VOCALIZE_INSTALL_ROOT:-${ROOT_DIR}}"
if [ -f "${ROOT_DIR}/config/.env" ]; then
  export VOCALIZE_ENV_FILE="${VOCALIZE_ENV_FILE:-${ROOT_DIR}/config/.env}"
fi
export VOCALIZE_STT_PROVIDER_URL="${VOCALIZE_STT_PROVIDER_URL:-http://127.0.0.1:8765}"
export VOCALIZE_TTS_PROVIDER_URL="${VOCALIZE_TTS_PROVIDER_URL:-http://127.0.0.1:8765}"
export VOCALIZE_SPEECH_PROVIDER_AUTO_START="${VOCALIZE_SPEECH_PROVIDER_AUTO_START:-1}"
export VOCALIZE_SPEECH_PROVIDER_COMMAND="${VOCALIZE_SPEECH_PROVIDER_COMMAND:-${ROOT_DIR}/bin/vocalize-mac-speech-provider}"
export VOCALIZE_FRONTEND_DIST="${VOCALIZE_FRONTEND_DIST:-${ROOT_DIR}/app/vocalize/_internal/vocalize_runtime/frontend}"
exec "${ROOT_DIR}/app/vocalize/vocalize" "$@"
SH
chmod 755 "${BUNDLE_DIR}/bin/vocalize"
chmod 755 "${BUNDLE_DIR}/uninstall.sh"

cat > "${BUNDLE_DIR}/vocalize" <<'SH'
#!/usr/bin/env bash
set -euo pipefail
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec "${ROOT_DIR}/bin/vocalize" "$@"
SH
chmod 755 "${BUNDLE_DIR}/vocalize"

"$PYTHON_BIN" -m tools.release.artifacts manifest \
    --output "${BUNDLE_DIR}/manifest.json" \
    --version "$VERSION" \
    --artifact-name "$ARTIFACT_NAME" \
    --arch "$ARCH" \
    --signing-mode "$SIGNING_MODE" \
    --entrypoint "bin/vocalize" \
    --backend-executable "app/vocalize/vocalize" \
    --frontend-dist "app/vocalize/_internal/vocalize_runtime/frontend" \
    --speech-provider "bin/vocalize-mac-speech-provider"

sign_executable() {
    local target="$1"
    if [[ "$SIGNING_MODE" == "skip" ]]; then
        return 0
    fi
    if [[ "$SIGNING_MODE" == "ad-hoc" ]]; then
        codesign --force --sign - "$target"
    else
        codesign --force --options runtime --timestamp \
            --sign "$APPLE_DEVELOPER_ID_APPLICATION" "$target"
    fi
}

if [[ "$SIGNING_MODE" != "skip" ]]; then
    echo "[5/7] Codesigning executables..."
    while IFS= read -r -d '' candidate; do
        if file "$candidate" | grep -q "Mach-O"; then
            sign_executable "$candidate"
        fi
    done < <(find "$BUNDLE_DIR" -type f -print0)
else
    echo "[5/7] Skipping codesign. This artifact is not public-release ready."
fi

echo "[6/7] Creating GitHub Release zip..."
mkdir -p "$DIST_DIR"
rm -f "$ZIP_PATH" "$CHECKSUM_PATH" "$INSTALLER_ASSET"
(cd "$WORK_DIR" && ditto -c -k --norsrc --noextattr --keepParent "$ARTIFACT_NAME" "$ZIP_PATH")
cp "${ROOT_DIR}/install/install.sh" "$INSTALLER_ASSET"
chmod 755 "$INSTALLER_ASSET"

if [[ "$SIGNING_MODE" == "developer-id" ]]; then
    echo "[6/7] Submitting zip for notarization..."
    xcrun notarytool submit "$ZIP_PATH" \
        --apple-id "$APPLE_ID" \
        --team-id "$APPLE_TEAM_ID" \
        --password "$APPLE_APP_SPECIFIC_PASSWORD" \
        --wait
fi

echo "[7/7] Writing SHA256SUMS..."
"$PYTHON_BIN" -m tools.release.artifacts sha256 \
    --output "$CHECKSUM_PATH" \
    "$ZIP_PATH" \
    "$INSTALLER_ASSET"

echo ""
echo "=== Release artifact ready ==="
echo "Asset: ${ZIP_PATH}"
echo "Installer: ${INSTALLER_ASSET}"
echo "Checksums: ${CHECKSUM_PATH}"
echo ""
echo "Verify:"
echo "  bash install/verify-release.sh '${CHECKSUM_PATH}' '${ZIP_PATH}'"
