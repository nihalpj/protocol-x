#!/usr/bin/env bash
# stop.sh — stop a server started by start.sh.
set -euo pipefail

PID_FILE="${PROTOX_DATA_DIR:-$HOME/.local/share/protocol-x}/server.pid"
if [[ ! -f "$PID_FILE" ]]; then
  echo "not running (no pid file)"
  exit 0
fi
PID="$(cat "$PID_FILE")"
if kill -0 "$PID" 2>/dev/null; then
  kill "$PID"
  echo "stopped (pid $PID)"
else
  echo "not running (stale pid file)"
fi
rm -f "$PID_FILE"
