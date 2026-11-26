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
# v0.9.1: --bootloader-ignore-signals — без него bootloader onefile форвардит
# Ctrl-C группе: SIGINT приходит и bootloader'у, и дочернему python;
# задвоенный сигнал в микроокне между первым raise KeyboardInterrupt и
# переустановкой хэндлеров в graceful_shutdown уходил непойманным →
# «[EXIT] Bye» + traceback + [PYI-7290]. С флагом bootloader сигналы
# игнорирует — их получает только дочерний python (единый хэндлер
# shutdown._first_signal закрывает остальные окна). Флаг + единый хэндлер
# = чистый выход по Ctrl-C (см. backend_adapter/shutdown.py).
pyinstaller \
    --onefile \
    --name backend-adapter \
    --distpath "$DIST_DIR" \
    --workpath "build/$TARGET" \
    --specpath "build/$TARGET" \
    --hidden-import yaml \
    --hidden-import yaml.emitter \
    --bootloader-ignore-signals \
    --clean \
    backend-adapter.py

BIN="$DIST_DIR/backend-adapter"
echo ""
echo "Built: $BIN"
echo ""

# ─── 4. Self-check: the binary must start far enough to reach the adapter's
# ─── own startup (no PyInstaller import/runtime failure). With no config it
# ─── prints "[FATAL] ADAPTER_BACKEND_CONFIG is not set" and exits 1. That
# ─── healthy early-exit is exactly what we assert; a broken PyInstaller build
# ─── would fail differently (import error / non-1 exit / no [FATAL] line).
# ───
# ─── The run MUST be isolated (v1.0.3): since v0.9.13 the adapter
# ─── auto-discovers a home when --root is absent (current dir, then ~/.ba).
# ─── Started from the repo with a real ~/.ba present, the check would find
# ─── that home, boot the server fully, bind the port and never exit — the
# ─── build script hung on "$(...)" waiting for EOF, and the stray process
# ─── rewrote the PID/state files of a live installation. So: throwaway cwd +
# ─── empty HOME, no inherited ADAPTER_* (env -i). A watchdog caps the run —
# ─── a onefile bootloader can otherwise linger; the whole process group is
# ─── signalled so its python child dies too.
CHECK_DIR="$(mktemp -d)"
CHECK_OUT="$CHECK_DIR/output.txt"
CHECK_TIMEOUT="${SELF_CHECK_TIMEOUT:-60}"

# The check runs in a throwaway cwd, so a relative $BIN would no longer
# resolve — pin it to an absolute path first.
CHECK_BIN="$(cd "$(dirname "$BIN")" && pwd)/$(basename "$BIN")"

# Job control gives the background subshell its own process group, so the
# timeout can signal the group (-PID) rather than just the bootloader.
set -m
( cd "$CHECK_DIR" && env -i HOME="$CHECK_DIR" PATH="$PATH" "$CHECK_BIN" >"$CHECK_OUT" 2>&1 ) &
CHECK_PID=$!
set +m

CHECK_WAITED=0
while kill -0 "$CHECK_PID" 2>/dev/null; do
    if [ "$CHECK_WAITED" -ge "$CHECK_TIMEOUT" ]; then
        kill -TERM -"$CHECK_PID" 2>/dev/null || true
        sleep 1
        kill -KILL -"$CHECK_PID" 2>/dev/null || true
        wait "$CHECK_PID" 2>/dev/null || true
        echo "Self-check FAILED: binary did not exit within ${CHECK_TIMEOUT}s." >&2
        echo "--- binary output ---" >&2
        cat "$CHECK_OUT" >&2
        echo "----------------------" >&2
        rm -rf "$CHECK_DIR"
        exit 1
    fi
    sleep 1
    CHECK_WAITED=$((CHECK_WAITED + 1))
done

STATUS=0
wait "$CHECK_PID" || STATUS=$?
OUTPUT="$(cat "$CHECK_OUT")"
rm -rf "$CHECK_DIR"

if [ "$STATUS" -eq 1 ] && printf '%s' "$OUTPUT" | grep -q "ADAPTER_BACKEND_CONFIG is not set"; then
    echo "Self-check OK: binary starts, reaches its own [FATAL] early-exit (exit 1)."
else
    echo "Self-check FAILED: expected [FATAL] ADAPTER_BACKEND_CONFIG is not set and exit 1." >&2
    echo "--- binary output ---" >&2
    printf '%s\n' "$OUTPUT" >&2
    echo "----------------------" >&2
    exit 1
fi
