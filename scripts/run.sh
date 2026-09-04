#!/usr/bin/env bash
# Launch backend-adapter: ensure venv, install deps, check config, run.
# Exit on first error.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$SCRIPT_DIR"

# ─── 1. Virtual environment ───────────────────────────────────────────────

if [[ -z "${VIRTUAL_ENV:-}" ]]; then
    if [[ ! -d "venv" ]]; then
        echo "Creating virtual environment in ./venv ..."
        python3 -m venv venv
    fi
    echo "Sourcing venv/bin/activate ..."
    # shellcheck disable=SC1091
    source venv/bin/activate
else
    echo "Virtual environment already active: ${VIRTUAL_ENV}"
fi

# ─── 2. Install requirements ──────────────────────────────────────────────

echo "Checking packages from requirements.txt ..."
# python -m pip ensures we use the venv's pip; --quiet suppresses progress.
if ! python -m pip install -q -r requirements.txt 2>&1 >/dev/null; then
    echo "Failed to install requirements. Make sure python3 is available." >&2
    exit 1
fi
echo "Packages OK."

# ─── 3. Configuration files ───────────────────────────────────────────────

for cfg in adapter.env adapter.yaml; do
    if [[ ! -f "$cfg" ]]; then
        echo "[ERROR] $cfg not found in ${SCRIPT_DIR}" >&2
        echo "Copy sample.adapter.env -> adapter.env and sample.adapter.yaml -> adapter.yaml" >&2
        exit 1
    fi
done

source adapter.env
echo "Configuration loaded from adapter.env."

# ─── 4. Run ───────────────────────────────────────────────────────────────

exec python3 backend-adapter.py "$@"
