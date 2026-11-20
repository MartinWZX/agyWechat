#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOG_DIR="$PROJECT_DIR/logs"
SUP_PID_FILE="$LOG_DIR/agy_wechat_supervisor.pid"
APP_PID_FILE="$LOG_DIR/agy_wechat_app.pid"
LOG_FILE="$LOG_DIR/agy_wechat_service.log"
PYTHON_BIN="$PROJECT_DIR/venv/bin/python3"

mkdir -p "$LOG_DIR"

_is_sup_running() {
    if [ -r "$SUP_PID_FILE" ]; then
        local pid
        pid=$(cat "$SUP_PID_FILE" 2>/dev/null || true)
        if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
            if tr '\0' ' ' < "/proc/$pid/cmdline" 2>/dev/null | grep -q "wsl_service.sh"; then
                echo "$pid"
                return 0
            fi
        fi
    fi
    return 1
}

_is_app_running() {
    if [ -r "$APP_PID_FILE" ]; then
        local pid
        pid=$(cat "$APP_PID_FILE" 2>/dev/null || true)
        if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
            if tr '\0' ' ' < "/proc/$pid/cmdline" 2>/dev/null | grep -q "main.py"; then
                echo "$pid"
                return 0
            fi
        fi
    fi
    local pg
    pg=$(pgrep -f "$PROJECT_DIR/main.py" 2>/dev/null | head -n1 || true)
    if [ -n "$pg" ]; then
        echo "$pg"
        return 0
    fi
    return 1
}

_run_supervisor() {
    echo "$$" > "$SUP_PID_FILE"
    local child_pid=""
    local stopping=0

    _cleanup() {
        stopping=1
        echo "$(date '+%Y-%m-%d %H:%M:%S') [SUPERVISOR] Stopping agyWechat service..."
        if [ -n "$child_pid" ] && kill -0 "$child_pid" 2>/dev/null; then
            kill -TERM "$child_pid" 2>/dev/null || true
            wait "$child_pid" 2>/dev/null || true
        fi
        rm -f "$APP_PID_FILE" "$SUP_PID_FILE"
        exit 0
    }
    trap _cleanup SIGTERM SIGINT

    while [ "$stopping" -eq 0 ]; do
        if [ -r /etc/profile.d/proxy.sh ]; then
            set +e
            # shellcheck disable=SC1091
            . /etc/profile.d/proxy.sh >/dev/null 2>&1 || true
            set -e
        fi
        export PATH="$HOME/.local/bin:$PROJECT_DIR/venv/bin:$PATH"
        export PYTHONUNBUFFERED=1
        cd "$PROJECT_DIR"

        echo "$(date '+%Y-%m-%d %H:%M:%S') [SUPERVISOR] Launching $PYTHON_BIN $PROJECT_DIR/main.py"
        "$PYTHON_BIN" -u "$PROJECT_DIR/main.py" &
        child_pid=$!
        echo "$child_pid" > "$APP_PID_FILE"

        set +e
        wait "$child_pid"
        rc=$?
        set -e
        rm -f "$APP_PID_FILE"

        if [ "$stopping" -eq 1 ]; then
            break
        fi
        echo "$(date '+%Y-%m-%d %H:%M:%S') [SUPERVISOR] main.py exited with code $rc; restarting in 3s..."
        sleep 3
    done
}

_start() {
    local existing_sup existing_app
    existing_sup=$(_is_sup_running || true)
    existing_app=$(_is_app_running || true)
    if [ -n "$existing_sup" ] && [ -n "$existing_app" ]; then
        echo "agyWechat service already running (supervisor=$existing_sup, app=$existing_app)."
        return 0
    fi
    if [ -n "$existing_app" ] && [ -z "$existing_sup" ]; then
        echo "Stopping unmanaged main.py process ($existing_app) before starting managed service..."
        kill -TERM "$existing_app" 2>/dev/null || true
        sleep 1
    fi

    if [ -r /etc/profile.d/proxy.sh ]; then
        set +e
        # shellcheck disable=SC1091
        . /etc/profile.d/proxy.sh >/dev/null 2>&1 || true
        set -e
    fi
    export PATH="$HOME/.local/bin:$PROJECT_DIR/venv/bin:$PATH"

    nohup setsid "$PROJECT_DIR/scripts/wsl_service.sh" _supervisor </dev/null >>"$LOG_FILE" 2>&1 &

    local i=0
    while [ "$i" -lt 25 ]; do
        if "$PYTHON_BIN" -c "import urllib.request; r = urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=1); print(r.status)" >/dev/null 2>&1; then
            local sp ap
            sp=$(_is_sup_running || echo "?")
            ap=$(_is_app_running || echo "?")
            echo "✅ agyWechat WSL2 service started (supervisor=$sp, app=$ap, http://127.0.0.1:8000/health OK)."
            return 0
        fi
        sleep 0.4
        i=$((i + 1))
    done

    echo "⚠️ agyWechat service spawned; check $LOG_FILE for startup status."
    return 0
}

_stop() {
    local sp ap
    sp=$(_is_sup_running || true)
    ap=$(_is_app_running || true)
    if [ -n "$sp" ]; then
        kill -TERM "$sp" 2>/dev/null || true
    fi
    if [ -n "$ap" ]; then
        kill -TERM "$ap" 2>/dev/null || true
    fi
    local i=0
    while [ "$i" -lt 20 ]; do
        if ! _is_sup_running >/dev/null && ! _is_app_running >/dev/null; then
            break
        fi
        sleep 0.25
        i=$((i + 1))
    done
    sp=$(_is_sup_running || true)
    ap=$(_is_app_running || true)
    [ -n "$sp" ] && kill -KILL "$sp" 2>/dev/null || true
    [ -n "$ap" ] && kill -KILL "$ap" 2>/dev/null || true
    rm -f "$SUP_PID_FILE" "$APP_PID_FILE"
    echo "🛑 agyWechat service stopped."
}

_status() {
    local sp ap
    sp=$(_is_sup_running || echo "stopped")
    ap=$(_is_app_running || echo "stopped")
    echo "supervisor_pid=$sp app_pid=$ap log=$LOG_FILE"
    if "$PYTHON_BIN" -c "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=2).read().decode())" 2>/dev/null; then
        return 0
    else
        echo "health_check=unreachable"
        return 1
    fi
}

case "${1:-status}" in
    start) _start ;;
    stop) _stop ;;
    restart) _stop; _start ;;
    status) _status ;;
    _supervisor) _run_supervisor ;;
    *)
        echo "Usage: $0 {start|stop|restart|status}"
        exit 2
        ;;
esac
