#!/usr/bin/env bash
#
# backend-adapter — one-line installer (binary only).
#
#   curl -fsSL https://raw.githubusercontent.com/alekseybb197/backend-adapter/main/install.sh | bash
#   curl -fsSL https://raw.githubusercontent.com/alekseybb197/backend-adapter/main/install.sh | bash -s -- --service
#
# Downloads the prebuilt binary for this platform from GitHub Releases and
# installs it to a FIXED per-platform path:
#
#   Linux  -> /usr/local/bin   (uses sudo when the directory is not writable)
#   macOS  -> ~/.local/bin     (per-user path, no sudo)
#
# Windows is not supported by this bash installer: use the PowerShell
# installer (install.ps1 — to be added separately) or run it inside WSL,
# where the platform is detected as Linux.
#
# With --service it additionally installs a persistent service pointed at the
# installed binary:
#
#   Linux  a SYSTEM systemd unit (/etc/systemd/system), run as a dedicated
#          unprivileged user and rooted at /var/lib/backend-adapter, where a
#          ready-to-run adapter.yaml + adapter.env are generated. The backend
#          base URL and token are asked interactively (or taken from the
#          ADAPTER_SERVICE_BACKEND_BASE / ADAPTER_SERVICE_BACKEND_KEY env vars
#          for non-interactive runs). Needs root — the script escalates with
#          sudo when it is not already root.
#   macOS  a launchd agent (current user, ~/Library/LaunchAgents).
#
# With --delete it does the opposite: removes the installed binary and, when
# present, the systemd service — the unit, the state directory (which holds
# the token), the service user and the binary. Asks for confirmation on the
# terminal unless --yes (or ADAPTER_DELETE_YES=1) is given. Linux only for
# now; macOS exits with an explicit "not supported" error. Needs root.
#
# The script only talks to github.com (official releases + unit files of this
# repo). Review it before running: | bash | less
#
# See docs/install.md (section 4.2, "Установка одной строкой") for the guide.
set -euo pipefail

REPO="alekseybb197/backend-adapter"
BINARY_NAME="backend-adapter"
SERVICE_INSTALL="${SERVICE_INSTALL:-0}"
DELETE_INSTALL="${DELETE_INSTALL:-0}"
DELETE_YES="${ADAPTER_DELETE_YES:-0}"

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
    --service) SERVICE_INSTALL=1; shift ;;
    --delete) DELETE_INSTALL=1; shift ;;
    --yes|-y) DELETE_YES=1; shift ;;
    --pip)
      err "--pip (sources install) was removed: only the latest-release binary is supported."
      info "Install from sources manually instead: git clone + venv (see docs/install.md)."
      exit 1
      ;;
    --prefix)
      err "--prefix was removed: the install path is fixed per platform."
      info "Linux -> /usr/local/bin; macOS -> ~/.local/bin."
      exit 1
      ;;
    --help|-h)
      cat <<'EOF'
Usage: install.sh [OPTIONS]

Downloads the prebuilt binary for this platform from the latest GitHub
Release and installs it to a fixed path:
  Linux   /usr/local/bin   (sudo is used when the directory is not writable)
  macOS   ~/.local/bin     (per-user, no sudo)

Windows is not supported by this bash installer — use the PowerShell
installer (install.ps1, added separately) or run inside WSL.

Options:
  --service       Install a persistent service for the binary.
                  Linux: a SYSTEM systemd unit (/etc/systemd/system) run as
                  the dedicated user 'backend-adapter', rooted at
                  /var/lib/backend-adapter with a generated adapter.yaml
                  (provider 'main') and adapter.env. The service is enabled
                  and started immediately. Requires root (sudo is used when
                  needed); the backend base URL and token are asked for
                  interactively unless the env vars below are set.
                  macOS: a launchd agent for the current user.
  --delete        Remove the installed binary and, if present, the systemd
                  service: the unit, the state directory /var/lib/backend-adapter
                  (which holds the token), the service user and the binary.
                  Asks for confirmation unless --yes is given. Linux only for
                  now (macOS is not supported); requires root (sudo is used
                  when needed). Mutually exclusive with --service.
  --yes, -y       Skip the --delete confirmation (for scripts/CI).
  --help          Show this help

Environment:
  SERVICE_INSTALL             Same as --service (1/0)
  DELETE_INSTALL              Same as --delete (1/0)
  ADAPTER_DELETE_YES          Same as --yes (1/0)
  ADAPTER_SERVICE_BACKEND_BASE  Backend base URL for --service (skips the prompt)
  ADAPTER_SERVICE_BACKEND_KEY   Backend API token for --service (skips the prompt)
  ADAPTER_SERVICE_ROOT          State dir (default /var/lib/backend-adapter)
  ADAPTER_SERVICE_USER          Service user (default backend-adapter)

The binary needs the same config as the sources: a YAML file passed via
ADAPTER_BACKEND_CONFIG plus the token env var named in its `key` field
(see docs/samples/sample.adapter.yaml in the repo). Legacy
ADAPTER_BACKEND_BASE/ADAPTER_BACKEND_KEY were removed in v0.7.2.
EOF
      exit 0
      ;;
    *) warn "Unknown option: $1 (ignored)"; shift ;;
  esac
done

# Installing and deleting are opposite modes: combining them is a mistake, not
# a precedence question.
if [[ "$SERVICE_INSTALL" == 1 && "$DELETE_INSTALL" == 1 ]]; then
  err "--service and --delete are mutually exclusive."
  exit 1
fi

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
    # Windows shells that run bash (Git Bash / MSYS2 / Cygwin). Real Windows
    # (cmd/PowerShell) never reaches this script; WSL reports "linux".
    mingw*|msys*|cygwin*) echo "windows" ;;
    *) echo "unsupported" ;;
  esac
}

PLATFORM=$(detect_platform)
case "$PLATFORM" in
  windows)
    err "Windows is not supported by this bash installer."
    info "Use the PowerShell installer (install.ps1 — added separately):"
    info "  irm https://raw.githubusercontent.com/${REPO}/main/install.ps1 | iex"
    info "Or run this installer inside WSL (there it is detected as Linux)."
    exit 1
    ;;
  unsupported)
    err "Unsupported platform: $(uname -s) $(uname -m)"
    exit 1
    ;;
esac

# ── Fixed install dir per platform ─────────────────────────────────────
case "$PLATFORM" in
  macos-*) INSTALL_DIR="$HOME/.local/bin" ;;
  *)       INSTALL_DIR="/usr/local/bin" ;;
esac

# Whether writing to INSTALL_DIR needs sudo (set by ensure_install_dir).
USE_SUDO=0

# ── System service layout (Linux, --service) ───────────────────────────
# Rooted at a single state directory that holds both configs and logs; the
# service runs as a dedicated unprivileged system user. Overridable for tests
# and non-standard layouts.
SERVICE_ROOT="${ADAPTER_SERVICE_ROOT:-/var/lib/backend-adapter}"
SERVICE_USER="${ADAPTER_SERVICE_USER:-backend-adapter}"
SERVICE_UNIT="/etc/systemd/system/backend-adapter.service"
SERVICE_ENV="${SERVICE_ROOT}/adapter.env"
SERVICE_YAML="${SERVICE_ROOT}/adapter.yaml"
SERVICE_LOGS="${SERVICE_ROOT}/logs"

# Backend base URL / token for the generated config. When empty, --service
# asks for them interactively (see collect_service_config).
SERVICE_BASE="${ADAPTER_SERVICE_BACKEND_BASE:-}"
SERVICE_KEY="${ADAPTER_SERVICE_BACKEND_KEY:-}"

# Run a command with root privileges: directly when already root, via sudo
# otherwise. Used only by the --service path (it writes to /etc, /var/lib,
# creates a user and calls systemctl). set -e aborts on a failed sudo.
as_root() {
  if [[ $EUID -eq 0 ]]; then
    "$@"
  else
    sudo "$@"
  fi
}

# Make sure INSTALL_DIR exists and is writable; escalate to sudo on Linux
# when it is not (the path stays exactly /usr/local/bin either way).
ensure_install_dir() {
  if [[ -d "$INSTALL_DIR" && -w "$INSTALL_DIR" ]]; then
    return
  fi
  # Parent writable (or the dir absent and creatable) — no escalation needed.
  if mkdir -p "$INSTALL_DIR" 2>/dev/null; then
    return
  fi
  if [[ "$PLATFORM" == macos-* ]]; then
    # ~/.local/bin is per-user: failure here is not a permission problem.
    err "Cannot create ${INSTALL_DIR}"
    exit 1
  fi
  if ! command -v sudo &>/dev/null; then
    err "No write access to ${INSTALL_DIR} and sudo is not available."
    info "Run the installer as root instead: sudo bash install.sh"
    exit 1
  fi
  warn "${INSTALL_DIR} is not writable — using sudo"
  sudo mkdir -p "$INSTALL_DIR"
  USE_SUDO=1
}

# Deletion must not (re)create the install dir it may be removing — and it
# never downloads anything, so it needs no writable INSTALL_DIR up front.
if [[ "$DELETE_INSTALL" != 1 ]]; then
  ensure_install_dir
fi

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
  if [[ "$USE_SUDO" == 1 ]]; then
    sudo mv "${tmpdir}/${BINARY_NAME}" "${INSTALL_DIR}/${BINARY_NAME}"
  else
    mv "${tmpdir}/${BINARY_NAME}" "${INSTALL_DIR}/${BINARY_NAME}"
  fi
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
    #
    # The capture and the grep must be separate statements: the binary exits
    # 1 by design, so `OUTPUT=$(...) && grep ...` short-circuits and the grep
    # never runs (the healthy branch would be unreachable). `|| true` keeps
    # the standalone assignment from tripping `set -e` on that rc 1.
    OUTPUT=$("$bin_path" 2>&1 || true)
    if grep -q "ADAPTER_BACKEND_CONFIG is not set" <<<"$OUTPUT"; then
      ok "Binary starts and reaches its own [FATAL] early-exit (healthy)"
    else
      warn "Binary did not produce the expected startup output"
      printf '%s\n' "$OUTPUT" >&2
    fi
  else
    warn "$bin_path is not executable"
  fi
}

# ── Collect the backend URL + token for the generated config ───────────
# Non-interactive callers (CI, molecule, scripted installs) pass both via
# ADAPTER_SERVICE_BACKEND_BASE / ADAPTER_SERVICE_BACKEND_KEY. Otherwise ask on
# the controlling terminal: under `curl | bash` stdin is the piped script, so
# reading stdin would consume the installer itself.
collect_service_config() {
  if [[ -z "$SERVICE_BASE" || -z "$SERVICE_KEY" ]]; then
    if [[ ! -r /dev/tty ]]; then
      err "--service needs the backend URL and token."
      info "Set ADAPTER_SERVICE_BACKEND_BASE and ADAPTER_SERVICE_BACKEND_KEY,"
      info "or run the installer from an interactive terminal."
      exit 1
    fi
    while [[ -z "$SERVICE_BASE" ]]; do
      if ! read -r -p "Backend base URL (e.g. https://llm.example.com): " SERVICE_BASE </dev/tty; then
        err "No input on the terminal — aborted."
        exit 1
      fi
    done
    while [[ -z "$SERVICE_KEY" ]]; do
      # -s: do not echo the token; the newline is swallowed by -s, so add one.
      if ! read -r -s -p "Backend API token: " SERVICE_KEY </dev/tty; then
        err "No input on the terminal — aborted."
        exit 1
      fi
      echo >/dev/tty
    done
  fi
  # A trailing slash would yield '//v1/...' in request URLs.
  SERVICE_BASE="${SERVICE_BASE%/}"
}

# ── Install systemd service (Linux, system) ────────────────────────────
# The repo's docs/samples/backend-adapter.service is a python-source template
# (/usr/bin/python3 %h/backend-adapter/backend-adapter.py, User=username); it
# does not fit a binary install. For the binary we generate a SYSTEM unit
# (/etc/systemd/system, multi-user.target) run as a dedicated unprivileged
# user, with both configs under SERVICE_ROOT so the service is ready to run.
install_systemd() {
  if [[ "$(uname -s)" != "Linux" ]]; then
    return
  fi
  if [[ $EUID -ne 0 ]] && ! command -v sudo &>/dev/null; then
    err "--service installs a system service and needs root, but sudo is not available."
    info "Run the installer as root instead: sudo bash install.sh --service"
    exit 1
  fi
  if ! command -v systemctl &>/dev/null; then
    err "systemctl not found — cannot install a systemd service on this host."
    exit 1
  fi

  collect_service_config

  info "Installing systemd system service (user '${SERVICE_USER}', root '${SERVICE_ROOT}') ..."
  as_root mkdir -p "$SERVICE_ROOT" "$SERVICE_LOGS"

  if ! id -u "$SERVICE_USER" &>/dev/null; then
    # --no-create-home: the state dir is SERVICE_ROOT, not a home directory.
    local nologin=/usr/sbin/nologin
    [[ -x "$nologin" ]] || nologin=/bin/false
    as_root useradd --system --no-create-home \
      --home-dir "$SERVICE_ROOT" --shell "$nologin" "$SERVICE_USER"
  fi

  # Backend YAML: one provider 'main'; the token itself lives in the env file
  # (key: is the *name* of the env var holding it — see docs/environment.md).
  as_root tee "$SERVICE_YAML" >/dev/null <<EOF
# backend-adapter config (generated by install.sh --service)
backend:
  - name: main
    base: ${SERVICE_BASE}
    key: ADAPTER_BACKEND_KEY_MAIN
EOF

  # Env file: minimal working set. ADAPTER_DETACH_ENABLE=0 is mandatory —
  # detach (double fork) is incompatible with systemd supervision.
  as_root tee "$SERVICE_ENV" >/dev/null <<EOF
# backend-adapter env (generated by install.sh --service)
ADAPTER_BACKEND_CONFIG=${SERVICE_YAML}
ADAPTER_BACKEND_KEY_MAIN=${SERVICE_KEY}
ADAPTER_PROXY_PORT=9999
ADAPTER_ENDPOINT_HOST=127.0.0.1
ADAPTER_DEBUG_ENABLE=0
ADAPTER_DEBUG_LOGPATH=${SERVICE_LOGS}
ADAPTER_DETACH_ENABLE=0
EOF

  # Ownership: the service user owns its config and log dir; the env file with
  # the token stays root-only (systemd reads EnvironmentFile as root, the
  # process itself never needs it).
  as_root chown "$SERVICE_USER:$SERVICE_USER" "$SERVICE_YAML" "$SERVICE_LOGS"
  as_root chmod 0640 "$SERVICE_YAML"
  as_root chown root:root "$SERVICE_ENV"
  as_root chmod 0600 "$SERVICE_ENV"

  as_root tee "$SERVICE_UNIT" >/dev/null <<EOF
[Unit]
Description=backend-adapter proxy ([CC] <-> [OI])
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${SERVICE_USER}
Group=${SERVICE_USER}
WorkingDirectory=${SERVICE_ROOT}
EnvironmentFile=${SERVICE_ENV}
ExecStart=${INSTALL_DIR}/${BINARY_NAME}
Restart=on-failure
RestartSec=5
StandardOutput=journal
StandardError=journal
SyslogIdentifier=backend-adapter

[Install]
WantedBy=multi-user.target
EOF

  as_root systemctl daemon-reload
  # enable --now: autostart on boot AND start right away — the config is
  # complete, so the service can come up immediately.
  as_root systemctl enable --now backend-adapter.service
  ok "systemd system unit installed and started: ${SERVICE_UNIT}"
  info "Config:  ${SERVICE_YAML} (provider 'main', base ${SERVICE_BASE})"
  info "Env:     ${SERVICE_ENV} (token, mode 0600)"
  info "Logs:    ${SERVICE_LOGS} (journal: journalctl -u backend-adapter -f)"
  info "Status:  systemctl status backend-adapter"
}

# ── Delete the installed binary and service (Linux) ────────────────────
# The inverse of the install path: stop + remove the systemd unit, the state
# directory (adapter.env holds the token), the binary and the dedicated
# service user. Every step tolerates a missing object, so a second --delete is
# a clean no-op. Linux only for now — on macOS it exits with an explicit error.
delete_install() {
  if [[ "$(uname -s)" != "Linux" ]]; then
    err "--delete is only supported on Linux for now."
    info "On macOS remove the launchd agent manually (docs/install.md §9.2):"
    info "  launchctl unload ~/Library/LaunchAgents/com.user.backend-adapter.plist"
    exit 1
  fi
  if [[ $EUID -ne 0 ]] && ! command -v sudo &>/dev/null; then
    err "--delete removes a system service and needs root, but sudo is not available."
    info "Run the installer as root instead: sudo bash install.sh --delete"
    exit 1
  fi

  # Confirmation: destructive and irreversible, so ask unless --yes /
  # ADAPTER_DELETE_YES=1 was given. Read from /dev/tty (stdin may be the piped
  # script under `curl | bash`); the `if ! read` guard keeps set -e happy on EOF.
  if [[ "$DELETE_YES" != 1 ]]; then
    if [[ ! -r /dev/tty ]]; then
      err "--delete needs confirmation but there is no terminal."
      info "Re-run with --yes (or ADAPTER_DELETE_YES=1) for non-interactive use."
      exit 1
    fi
    local answer=""
    if ! read -r -p "Delete backend-adapter and its service? [y/N] " answer </dev/tty; then
      err "No input on the terminal — aborted."
      exit 1
    fi
    case "$answer" in
      y|Y|yes|YES|Yes) ;;
      *)
        info "Aborted — nothing was removed."
        exit 0
        ;;
    esac
  fi

  info "Removing backend-adapter (state '${SERVICE_ROOT}', user '${SERVICE_USER}') ..."

  # 1. Stop and disable first: a still-running unit could otherwise be restarted
  #    by Restart=on-failure while its files are being removed.
  if [[ -f "$SERVICE_UNIT" ]]; then
    as_root systemctl disable --now backend-adapter.service 2>/dev/null || true
    # 2. Drop the unit and refresh systemd's view.
    as_root rm -f "$SERVICE_UNIT"
    as_root systemctl daemon-reload 2>/dev/null || true
    ok "Removed systemd unit: ${SERVICE_UNIT}"
  else
    info "No systemd unit at ${SERVICE_UNIT} — skipped."
  fi

  # 3. State directory (adapter.yaml, adapter.env with the token, logs).
  if [[ -e "$SERVICE_ROOT" ]]; then
    as_root rm -rf "$SERVICE_ROOT"
    ok "Removed state directory: ${SERVICE_ROOT}"
  else
    info "No state directory at ${SERVICE_ROOT} — skipped."
  fi

  # 4. The binary itself.
  if [[ -e "${INSTALL_DIR}/${BINARY_NAME}" ]]; then
    as_root rm -f "${INSTALL_DIR}/${BINARY_NAME}"
    ok "Removed binary: ${INSTALL_DIR}/${BINARY_NAME}"
  else
    info "No binary at ${INSTALL_DIR}/${BINARY_NAME} — skipped."
  fi

  # 5. The dedicated service user (and its same-named group). Best-effort: a
  #    user still owning running processes should not block the rest.
  if id -u "$SERVICE_USER" &>/dev/null; then
    if as_root userdel "$SERVICE_USER" 2>/dev/null; then
      ok "Removed service user: ${SERVICE_USER}"
    else
      warn "Could not remove user ${SERVICE_USER} (still in use?) — remove it manually"
    fi
    as_root groupdel "$SERVICE_USER" 2>/dev/null || true
  else
    info "No service user '${SERVICE_USER}' — skipped."
  fi

  ok "Uninstall complete."
}

# ── Install launchd service (macOS, current user) ──────────────────────
# Same rationale as systemd: the repo plist (docs/samples/) targets the
# python-source layout (~/backend-adapter/backend-adapter.py). We generate
# a plist that runs the installed binary. launchd does not read
# EnvironmentFile, so the env vars are inlined in the plist; the env file
# written next to it is a copy-paste reference for editing the plist
# (single source of truth for the documented defaults).
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
ADAPTER_DEBUG_ENABLE=0
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
        <string>0</string>
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

# Delete is the inverse of install: it must not touch the network, the
# install dir or the service install path.
if [[ "$DELETE_INSTALL" == 1 ]]; then
  info "Mode:      delete"
  delete_install
  exit 0
fi

info "Install:   $INSTALL_DIR"
info "Service:   $([[ $SERVICE_INSTALL == 1 ]] && echo yes || echo no)"

install_binary

verify

if [[ "$SERVICE_INSTALL" == 1 ]]; then
  # A failed system-service install must be visible (non-zero exit), unlike the
  # launchd agent, which is best-effort.
  install_systemd
  install_launchd || true
fi

echo ""
ok "Installation complete!"
echo ""
if [[ "$SERVICE_INSTALL" == 1 && "$PLATFORM" == linux-* ]]; then
  echo "The service is enabled and already running. Point [CC] to the proxy:"
  echo "  export ANTHROPIC_BASE_URL=http://localhost:9999"
  echo "  claude"
  echo ""
  echo "Manage it with:"
  echo "  systemctl status backend-adapter"
  echo "  journalctl -u backend-adapter -f"
  echo ""
else
  echo "Next steps:"
  echo "  1. Create the backend config (YAML) and point to it:"
  echo "     export ADAPTER_BACKEND_CONFIG=/path/to/adapter.yaml"
  echo "     # example: docs/samples/sample.adapter.yaml in the repo"
  echo "     # (structure backend: name/base/key; key names the token env var)"
  echo ""
  echo "  2. Run the adapter:"
  echo "     backend-adapter"
  echo ""
  echo "  3. Point [CC] to the proxy:"
  echo "     export ANTHROPIC_BASE_URL=http://localhost:9999"
  echo "     claude"
  echo ""
fi
