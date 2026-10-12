#!/usr/bin/env bash
# status.sh — report server status and health.
set -euo pipefail

PID_FILE="${PROTOX_DATA_DIR:-$HOME/.local/share/protocol-x}/server.pid"
if [[ -f "$PID_FILE" ]] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
  echo "running (pid $(cat "$PID_FILE"))"
else
  echo "not running"
fi

URL="http://${PROTOX_HOST:-127.0.0.1}:${PROTOX_PORT:-7894}/health"
if command -v curl >/dev/null 2>&1; then
  curl -fsS --max-time 3 "$URL" 2>/dev/null && echo || echo "health: unreachable ($URL)"
fi
