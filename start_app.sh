#!/usr/bin/env bash
# Starts both halves of the app with one command: wsl_app (STT + claude CLI
# orchestration) in the background here in WSL, then windows_app (audio
# capture + GUI) via WSL->Windows interop, using each side's own venv.
#
# Run it from a WSL terminal:
#   ./start_app.sh
#
# Closing the windows_app window (or Ctrl+C here) stops wsl_app too.

set -e
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# wsl_app spawns the `claude` CLI via `subprocess.Popen(["claude", ...])`,
# which needs `claude` on PATH. `claude` lives under nvm's per-version bin
# directory, which normally gets onto PATH via ~/.bashrc -- but the Day 28
# double-click launcher runs this script through `bash -lc`, a
# non-interactive login shell, and Ubuntu's default ~/.bashrc bails out
# early for non-interactive shells before it ever reaches the nvm lines.
# Without this, `claude` silently isn't on PATH and wsl_app fails to start
# a session with a "Claude not found" error. Loading nvm directly here
# works regardless of how this script itself was invoked.
export NVM_DIR="$HOME/.nvm"
[ -s "$NVM_DIR/nvm.sh" ] && . "$NVM_DIR/nvm.sh"

IPC_PORT="$(python3 -c 'import json; print(json.load(open("ipc_config.json"))["port"])')"

# A previous run that was killed uncleanly (closed terminal, dropped WSL
# session, etc.) can leave an orphaned wsl_app still holding the port --
# happened the first time this script ran. Clean it up automatically
# instead of failing with "address already in use" and requiring manual
# intervention every time.
STALE_PID="$(ss -ltnp 2>/dev/null | awk -v port=":$IPC_PORT" '$4 ~ port {print $0}' | grep -oP 'pid=\K[0-9]+' || true)"
if [ -n "$STALE_PID" ]; then
    echo "Port $IPC_PORT already in use by a leftover process (pid $STALE_PID) -- stopping it first."
    kill "$STALE_PID" 2>/dev/null || true
    sleep 1
fi

echo "Starting wsl_app..."
wsl_app/.venv/bin/python -u wsl_app/main.py &
WSL_APP_PID=$!

cleanup() {
    if kill -0 "$WSL_APP_PID" 2>/dev/null; then
        echo "Stopping wsl_app..."
        kill "$WSL_APP_PID" 2>/dev/null || true
    fi
}
trap cleanup EXIT

# Give wsl_app a moment to load the Whisper model and start listening
# before windows_app tries to connect.
sleep 2

echo "Starting windows_app..."
windows_app/.venv/Scripts/python.exe windows_app/main.py
