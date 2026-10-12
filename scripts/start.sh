#!/usr/bin/env bash
# start.sh — start the server (HTTP transport + console) in the background.
set -euo pipefail
cd "$(dirname "$0")/.."

PID_FILE="${PROTOX_DATA_DIR:-$HOME/.local/share/protocol-x}/server.pid"
mkdir -p "$(dirname "$PID_FILE")"

if [[ -f "$PID_FILE" ]] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
  echo "already running (pid $(cat "$PID_FILE"))"
  exit 0
fi

nohup uv run protocol-x-tool-mcp serve --transport http >>"${PROTOX_DATA_DIR:-$HOME/.local/share/protocol-x}/server.log" 2>&1 &
echo $! >"$PID_FILE"
echo "started (pid $(cat "$PID_FILE")) — console at http://${PROTOX_HOST:-127.0.0.1}:${PROTOX_PORT:-7894}"
