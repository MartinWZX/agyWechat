#!/usr/bin/env bash
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

echo "=========================================================="
echo "   Autonomous Multi-CLI Agent (agy/pi/claude/omp) Runner  "
echo "=========================================================="

# 1. Check or activate Python virtual environment
VENV_DIR="$SCRIPT_DIR/venv"
if [ -d "$VENV_DIR" ]; then
    PYTHON_BIN="$VENV_DIR/bin/python3"
else
    PYTHON_BIN="$(which python3)"
fi

# 2. Check .env configuration
if [ ! -f "$SCRIPT_DIR/.env" ] && [ -f "$SCRIPT_DIR/.env.example" ]; then
    echo "⚠️ .env file not found. Initializing from .env.example..."
    cp "$SCRIPT_DIR/.env.example" "$SCRIPT_DIR/.env"
fi

# 3. Check CLI binary paths
for bin_name in agy pi claude omp; do
    if which "$bin_name" >/dev/null 2>&1; then
        echo "✅ Detected CLI engine [$bin_name]: $(which "$bin_name")"
    fi
done

echo "🚀 Starting Autonomous Multi-CLI Agent..."
exec "$PYTHON_BIN" "$SCRIPT_DIR/main.py"
