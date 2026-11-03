#!/usr/bin/env bash
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

echo "=========================================================="
echo "      WeChat-AGY Single-Process Bridge Startup Script     "
echo "=========================================================="

# 1. Check or create virtual environment
VENV_DIR="$SCRIPT_DIR/venv"
if [ ! -d "$VENV_DIR" ]; then
    echo "⚙️ Creating virtual environment at $VENV_DIR..."
    python3 -m venv "$VENV_DIR"
    echo "📦 Installing dependencies from requirements.txt..."
    "$VENV_DIR/bin/pip" install --upgrade pip
    "$VENV_DIR/bin/pip" install -r "$SCRIPT_DIR/requirements.txt"
fi

# 2. Check .env configuration
if [ ! -f "$SCRIPT_DIR/.env" ]; then
    echo "⚠️ .env file not found. Initializing from .env.example..."
    cp "$SCRIPT_DIR/.env.example" "$SCRIPT_DIR/.env"
    echo "📝 Please review and update $SCRIPT_DIR/.env as needed."
fi

# 3. Check agy binary location
AGY_BIN_PATH=$(which agy 2>/dev/null || echo "")
if [ -z "$AGY_BIN_PATH" ] && [ -f "$HOME/.local/bin/agy" ]; then
    AGY_BIN_PATH="$HOME/.local/bin/agy"
fi

if [ -n "$AGY_BIN_PATH" ]; then
    echo "✅ Found agy binary at: $AGY_BIN_PATH"
else
    echo "⚠️ Warning: 'agy' binary not found in standard PATH."
    echo "   Ensure AGY_BINARY in .env points to your agy CLI executable."
fi

# 4. Load environment variables for server startup
if [ -f "$SCRIPT_DIR/.env" ]; then
    export $(grep -v '^#' "$SCRIPT_DIR/.env" | xargs -d '\n' 2>/dev/null || true)
fi

HOST="${SERVICE_HOST:-0.0.0.0}"
PORT="${SERVICE_PORT:-8000}"

echo "🚀 Starting FastAPI server on http://$HOST:$PORT ..."
exec "$VENV_DIR/bin/uvicorn" app.main:app --host "$HOST" --port "$PORT" --lifespan on
