#!/usr/bin/env bash
#
# backend-adapter — one-line installer.
#
#   curl -fsSL https://raw.githubusercontent.com/alekseybb197/backend-adapter/main/install.sh | bash
#   curl -fsSL https://raw.githubusercontent.com/alekseybb197/backend-adapter/main/install.sh | bash -s -- --service
#
# Default path: download the prebuilt binary for this platform from GitHub
# Releases and put it into /usr/local/bin (or ~/.local/bin if not writable).
# With --service it additionally generates and enables a per-user service:
# a systemd user unit (Linux, systemctl --user) or a launchd agent (macOS,
# current user) — both pointed at the installed binary.
#
# The script only talks to github.com (official releases + unit files of this
# repo). Review it before running: | bash | less
#
# See docs/install.md (section 4.4, "Однострочный установщик") for the guide.
set -euo pipefail

REPO="alekseybb197/backend-adapter"
BINARY_NAME="backend-adapter"
INSTALL_DIR="${INSTALL_DIR:-/usr/local/bin}"
SERVICE_INSTALL="${SERVICE_INSTALL:-0}"
USE_PIP="${USE_PIP:-0}"

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

info() { echo -e "${BLUE}[INFO]${NC} $*"; }
ok()   { echo -e "${GREEN}[OK]${NC}   $*"; }
warn() { echo -e "${YELLOW}[WARN]${NC} $*"; }
err()  { echo -e "${RED}[ERR]${NC}  $*" >&2; }

# ── Parse args ─────────────────────────────────────────────────────────
while [[ $# -gt 0 ]]; do
  case "$1" in
    --prefix) INSTALL_DIR="$2"; shift 2 ;;
    --service) SERVICE_INSTALL=1; shift ;;
    --pip) USE_PIP=1; shift ;;
    --help|-h)
      cat <<'EOF'
Usage: install.sh [OPTIONS]

Options:
  --prefix DIR    Install directory (default: /usr/local/bin)
  --service       Generate and enable a per-user service: systemd user unit
                  (Linux, systemctl --user) / launchd agent (macOS). Writes a
                  fresh env file with an empty ADAPTER_BACKEND_CONFIG; fill it
                  in, then start the service manually (systemd: the unit is
                  enabled but not started; launchd: RunAtLoad=false).
  --pip           Install from sources via git clone + venv instead of binary
  --help          Show this help

Environment:
  INSTALL_DIR     Same as --prefix
  SERVICE_INSTALL Same as --service (1/0)
  USE_PIP         Same as --pip (1/0)

The binary needs the same config as the sources: a YAML file passed via
ADAPTER_BACKEND_CONFIG plus the token env var named in its `key` field
(see sample.adapter.yaml). Legacy ADAPTER_BACKEND_BASE/ADAPTER_BACKEND_KEY
were removed in v0.7.2.
EOF
      exit 0
      ;;
    *) warn "Unknown option: $1 (ignored)"; shift ;;
  esac
done

# ── Detect platform ────────────────────────────────────────────────────
detect_platform() {
  local os arch
  os=$(uname -s | tr '[:upper:]' '[:lower:]')
  arch=$(uname -m)

  case "$os" in
    linux)
      case "$arch" in
        x86_64|amd64)  echo "linux-x64" ;;
        aarch64|arm64) echo "linux-arm64" ;;
        *)             echo "unsupported" ;;
      esac
      ;;
    darwin)
      case "$arch" in
        x86_64)        echo "macos-x64" ;;
        arm64|aarch64) echo "macos-arm64" ;;
        *)             echo "unsupported" ;;
      esac
      ;;
    *) echo "unsupported" ;;
  esac
}

PLATFORM=$(detect_platform)
if [[ "$PLATFORM" == "unsupported" ]]; then
  err "Unsupported platform: $(uname -s) $(uname -m)"
  if [[ "$USE_PIP" != 1 ]]; then
    err "Falling back to --pip (sources install)..."
    USE_PIP=1
  fi
fi

# ── Find writable install dir ──────────────────────────────────────────
find_install_dir() {
  local dir="$1"
  if [[ -w "$dir" ]] || mkdir -p "$dir" 2>/dev/null; then
    echo "$dir"
    return
  fi
  # Fallback to user-local
  local user_bin="$HOME/.local/bin"
  mkdir -p "$user_bin" 2>/dev/null || true
  warn "No write access to $dir — installing to $user_bin"
  echo "$user_bin"
}

INSTALL_DIR=$(find_install_dir "$INSTALL_DIR")

# ── Install via pip (sources) ──────────────────────────────────────────
# `pip install backend-adapter`/`pip install .` (wheel) НЕ поддерживается:
# в wheel входит только пакет backend_adapter/ — консольная команда
# backend-adapter (cli.py) исполняет соседний backend-adapter.py через
# runpy, которого в wheel нет. Поэтому --pip клонирует исходники и ставит
# их в venv в editable-режиме (backend-adapter.py остаётся рядом).
install_pip() {
  if ! command -v python3 &>/dev/null; then
    err "python3 not found. Please install Python 3.10+ first."
    exit 1
  fi
  if ! command -v git &>/dev/null; then
    err "git not found. Please install git first (--pip clones the repository)."
    exit 1
  fi

  local src_dir="${INSTALL_DIR}/src"
  info "Cloning ${REPO} into ${src_dir} ..."
  rm -rf "$src_dir"
  git clone --depth 1 "https://github.com/${REPO}.git" "$src_dir"

  info "Creating venv and installing dependencies ..."
  python3 -m venv "${src_dir}/venv"
  # shellcheck disable=SC1091
  source "${src_dir}/venv/bin/activate"
  python -m pip install --upgrade pip
  pip install -e "$src_dir"

  ok "Sources installed into ${src_dir}/venv"
  info "Run: ${src_dir}/venv/bin/backend-adapter (add it to your PATH)"
  info "Config: cp ${src_dir}/sample.adapter.yaml -> adapter.yaml, then:"
  info "  export ADAPTER_BACKEND_CONFIG=${src_dir}/adapter.yaml"
}

# ── Install binary ─────────────────────────────────────────────────────
install_binary() {
  local asset="backend-adapter-${PLATFORM}"
  local url="https://github.com/${REPO}/releases/latest/download/${asset}"
  local tmpdir
  tmpdir=$(mktemp -d)
  trap "rm -rf $tmpdir" EXIT

  info "Downloading ${asset} from GitHub Releases ..."
  info "URL: ${url}"

  if command -v curl &>/dev/null; then
    curl -fsSL --progress-bar "$url" -o "${tmpdir}/${BINARY_NAME}"
  elif command -v wget &>/dev/null; then
    wget -q --show-progress "$url" -O "${tmpdir}/${BINARY_NAME}"
  else
    err "Neither curl nor wget found. Please install one of them."
    exit 1
  fi

  chmod +x "${tmpdir}/${BINARY_NAME}"
  mv "${tmpdir}/${BINARY_NAME}" "${INSTALL_DIR}/${BINARY_NAME}"
  ok "Installed binary to ${INSTALL_DIR}/${BINARY_NAME}"

  # macOS Gatekeeper: files downloaded via curl get the
  # com.apple.quarantine attribute; the OS then blocks the first run
  # ("damaged" / "developer cannot be verified"). Remove it from the
  # installed copy (best-effort — files without the attribute make
  # xattr fail with "No such xattr", which is fine).
  if [[ "$(uname -s)" == "Darwin" ]]; then
    if xattr -d com.apple.quarantine "${INSTALL_DIR}/${BINARY_NAME}" 2>/dev/null; then
      ok "Removed com.apple.quarantine from ${INSTALL_DIR}/${BINARY_NAME}"
    fi
  fi
}

# ── Verify installation ────────────────────────────────────────────────
verify() {
  local bin_path="${INSTALL_DIR}/${BINARY_NAME}"
  if [[ -x "$bin_path" ]]; then
    if [[ ":$PATH:" == *":${INSTALL_DIR}:"* ]]; then
      ok "backend-adapter is available in PATH"
    else
      warn "${INSTALL_DIR} is not in your PATH"
      info "Add this to your shell profile:"
      info "  export PATH=\"${INSTALL_DIR}:\$PATH\""
    fi
    # The binary has no --version flag: with an empty ADAPTER_BACKEND_CONFIG
    # it prints its banner and "[FATAL] ADAPTER_BACKEND_CONFIG is not set",
    # exiting 1 — that healthy early-exit proves the binary is alive.
    if OUTPUT=$("$bin_path" 2>&1) && grep -q "ADAPTER_BACKEND_CONFIG is not set" <<<"$OUTPUT"; then
      ok "Binary starts and reaches its own [FATAL] early-exit (healthy)"
    else
      warn "Binary did not produce the expected startup output"
      printf '%s\n' "$OUTPUT" >&2
    fi
  else
    warn "$bin_path is not executable"
  fi
}

# ── Install systemd service (Linux, user-level) ────────────────────────
# The repo's backend-adapter.service is a python-source template
# (/usr/bin/python3 %h/backend-adapter/backend-adapter.py, User=username);
# it does not fit a binary install. For the binary we generate a
# user-level unit (systemctl --user) pointing at the installed binary and
# an env file created next to it.
install_systemd() {
  if [[ "$(uname -s)" != "Linux" ]]; then
    return
  fi
  local unit_dir="$HOME/.config/systemd/user"
  local env_file="$HOME/.config/backend-adapter/backend-adapter.env"
  local unit_file="$unit_dir/backend-adapter.service"

  info "Installing systemd user unit ..."
  mkdir -p "$unit_dir" "$(dirname "$env_file")"

  # Fresh env file: ADAPTER_BACKEND_CONFIG is required, the rest are the
  # documented defaults. The user edits this file after install.
  cat > "$env_file" <<EOF
# backend-adapter env (generated by install.sh --service)
# Fill in ADAPTER_BACKEND_CONFIG with your backend YAML path, then start
# the unit (the installer only enabled it — the empty config would crash
# the service on boot):
#   systemctl --user daemon-reload
#   systemctl --user start backend-adapter.service
ADAPTER_BACKEND_CONFIG=
ADAPTER_PROXY_PORT=9999
ADAPTER_ENDPOINT_HOST=127.0.0.1
ADAPTER_DEBUG_ENABLE=1
ADAPTER_DETACH_ENABLE=0
EOF

  cat > "$unit_file" <<EOF
[Unit]
Description=backend-adapter proxy ([CC] <-> [OI])
After=network.target

[Service]
Type=simple
WorkingDirectory=$INSTALL_DIR
EnvironmentFile=$env_file
ExecStart=$INSTALL_DIR/backend-adapter
Restart=on-failure
RestartSec=5
StandardOutput=journal
StandardError=journal
SyslogIdentifier=backend-adapter

[Install]
WantedBy=default.target
EOF

  systemctl --user daemon-reload
  # enable only — no --now / no start: ADAPTER_BACKEND_CONFIG is still empty
  # in the env file, the user fills it in before the first start.
  systemctl --user enable backend-adapter.service
  ok "systemd user unit installed: ${unit_file}"
  info "Env file: ${env_file} (set ADAPTER_BACKEND_CONFIG to your YAML)"
  info "Then start: systemctl --user start backend-adapter.service"
  info "Check status: systemctl --user status backend-adapter.service"
}

# ── Install launchd service (macOS, current user) ──────────────────────
# Same rationale as systemd: the repo plist targets the python-source
# layout (~/backend-adapter/backend-adapter.py). We generate a plist that
# runs the installed binary. launchd does not read EnvironmentFile, so the
# env vars are inlined in the plist; the env file written next to it is a
# copy-paste reference for editing the plist (single source of truth for
# the documented defaults).
install_launchd() {
  if [[ "$(uname -s)" != "Darwin" ]]; then
    return
  fi
  local plist="$HOME/Library/LaunchAgents/com.user.backend-adapter.plist"
  local env_file="$HOME/.config/backend-adapter/backend-adapter.env"
  local log_dir="$HOME/Library/Logs/backend-adapter"

  info "Installing launchd agent ..."
  mkdir -p "$HOME/Library/LaunchAgents" "$(dirname "$env_file")" "$log_dir"

  cat > "$env_file" <<EOF
# backend-adapter env (generated by install.sh --service)
# launchd does not read env files: edit the variables directly inside
# $plist (ADAPTER_BACKEND_CONFIG is required, the rest are the
# documented defaults), then reload:
#   launchctl unload $plist && launchctl load $plist
ADAPTER_BACKEND_CONFIG=
ADAPTER_PROXY_PORT=9999
ADAPTER_ENDPOINT_HOST=127.0.0.1
ADAPTER_DEBUG_ENABLE=1
ADAPTER_DETACH_ENABLE=0
EOF

  # Variables are inlined because launchd ignores EnvironmentFile.
  # ADAPTER_BACKEND_CONFIG is intentionally empty until the user edits the
  # plist after install (it is the path to their backend YAML).
  cat > "$plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "https://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.user.backend-adapter</string>

    <key>ProgramArguments</key>
    <array>
        <string>$INSTALL_DIR/backend-adapter</string>
    </array>

    <key>WorkingDirectory</key>
    <string>$INSTALL_DIR</string>

    <key>EnvironmentVariables</key>
    <dict>
        <key>ADAPTER_BACKEND_CONFIG</key>
        <string></string>
        <key>ADAPTER_PROXY_PORT</key>
        <string>9999</string>
        <key>ADAPTER_ENDPOINT_HOST</key>
        <string>127.0.0.1</string>
        <key>ADAPTER_DEBUG_ENABLE</key>
        <string>1</string>
        <key>ADAPTER_DETACH_ENABLE</key>
        <string>0</string>
    </dict>

    <key>RunAtLoad</key>
    <false/>

    <key>KeepAlive</key>
    <false/>

    <key>StandardOutPath</key>
    <string>$log_dir/adapter.log</string>

    <key>StandardErrorPath</key>
    <string>$log_dir/adapter.log</string>

    <key>ProcessType</key>
    <string>Background</string>
</dict>
</plist>
EOF

  ok "launchd agent installed: ${plist}"
  info "Edit ${plist}: set ADAPTER_BACKEND_CONFIG to your backend YAML path"
  info "Then load: launchctl load ${plist}"
  info "Logs: ${log_dir}/adapter.log"
  info "Check status: launchctl list | grep backend-adapter"
}

# ── Main ───────────────────────────────────────────────────────────────
echo ""
echo "╔══════════════════════════════════════════════════════════════╗"
echo "║     backend-adapter installer                                ║"
echo "║     [CC] <-> [OI] backend proxy adapter                     ║"
echo "╚══════════════════════════════════════════════════════════════╝"
echo ""

info "Platform:  $PLATFORM"
info "Install:   $INSTALL_DIR"
info "Service:   $([[ $SERVICE_INSTALL == 1 ]] && echo yes || echo no)"

if [[ "$USE_PIP" == 1 ]]; then
  install_pip
else
  install_binary
fi

verify

if [[ "$SERVICE_INSTALL" == 1 ]]; then
  install_systemd || true
  install_launchd || true
fi

echo ""
ok "Installation complete!"
echo ""
echo "Next steps:"
echo "  1. Create the backend config (YAML) and point to it:"
echo "     export ADAPTER_BACKEND_CONFIG=/path/to/adapter.yaml"
echo "     # example: sample.adapter.yaml in the repo (structure backend: name/base/key)"
echo ""
echo "  2. Run the adapter:"
echo "     backend-adapter"
echo ""
echo "  3. Point [CC] to the proxy:"
echo "     export ANTHROPIC_BASE_URL=http://localhost:9999"
echo "     claude"
echo ""
