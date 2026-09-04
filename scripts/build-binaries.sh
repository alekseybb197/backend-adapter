#!/usr/bin/env bash
# Build a standalone binary for the current platform with PyInstaller.
# Usage: ./scripts/build-binaries.sh            (auto-detect platform)
#        ./scripts/build-binaries.sh [target]   (linux-x64|macos-arm64|macos-x64|windows-x64)
# Result: dist/binaries/<target>/backend-adapter[.exe]
#
# PyInstaller does not cross-compile: the binary must be built on the target
# OS/arch. CI builds all four artifacts on tag pushes (see
# .github/workflows/release-binaries.yml).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(dirname "$SCRIPT_DIR")"
cd "$ROOT_DIR"

# ─── 1. Resolve target ─────────────────────────────────────────────────────

if [ $# -eq 0 ]; then
    OS="$(uname -s | tr '[:upper:]' '[:lower:]')"
    ARCH="$(uname -m)"
    case "$OS" in
        linux)
            case "$ARCH" in
                x86_64|amd64)  TARGET="linux-x64" ;;
                aarch64|arm64) TARGET="linux-arm64" ;;
                *) echo "Unsupported Linux arch: $ARCH" >&2; exit 1 ;;
            esac
            ;;
        darwin)
            case "$ARCH" in
                arm64|aarch64) TARGET="macos-arm64" ;;
                x86_64)        TARGET="macos-x64" ;;
                *) echo "Unsupported macOS arch: $ARCH" >&2; exit 1 ;;
            esac
            ;;
        *) echo "Unsupported OS: $OS" >&2; exit 1 ;;
    esac
else
    TARGET="$1"
fi

echo "Building backend-adapter for target: $TARGET"

# ─── 2. Verify PyInstaller, install if missing ─────────────────────────────

if ! python -c "import PyInstaller" >/dev/null 2>&1; then
    echo "PyInstaller is missing; installing into the current environment ..."
    pip install pyinstaller
fi

# ─── 3. Build ──────────────────────────────────────────────────────────────

# specpath keeps backend-adapter.spec out of the repo root (it would be
# overwritten on each platform build otherwise); build/ and dist/ are
# git-ignored already.
DIST_DIR="dist/binaries/$TARGET"
rm -rf "$DIST_DIR" "build/$TARGET"
pyinstaller \
    --onefile \
    --name backend-adapter \
    --distpath "$DIST_DIR" \
    --workpath "build/$TARGET" \
    --specpath "build/$TARGET" \
    --hidden-import yaml \
    --hidden-import yaml.emitter \
    --clean \
    backend-adapter.py

BIN="$DIST_DIR/backend-adapter"
echo ""
echo "Built: $BIN"
echo ""

# ─── 4. Self-check: the binary must start far enough to reach the adapter's
# ─── own startup (no PyInstaller import/runtime failure). The adapter has no
# ─── --help flag — with an empty ADAPTER_BACKEND_CONFIG it prints
# ─── "[FATAL] ADAPTER_BACKEND_CONFIG is not set" and exits 1. That healthy
# ─── early-exit is exactly what we assert; a broken PyInstaller build would
# ─── fail differently (import error / non-1 exit / no [FATAL] line).
OUTPUT="$("$BIN" 2>&1)"
STATUS=$?
if [ "$STATUS" -eq 1 ] && printf '%s' "$OUTPUT" | grep -q "ADAPTER_BACKEND_CONFIG is not set"; then
    echo "Self-check OK: binary starts, reaches its own [FATAL] early-exit (exit 1)."
else
    echo "Self-check FAILED: expected [FATAL] ADAPTER_BACKEND_CONFIG is not set and exit 1." >&2
    echo "--- binary output ---" >&2
    printf '%s\n' "$OUTPUT" >&2
    echo "----------------------" >&2
    exit 1
fi
