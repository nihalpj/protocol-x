#!/usr/bin/env bash
# start.sh — start the server.
#   ./start [http]   HTTP transport + console, backgrounded (default)
#   ./start stdio    stdio transport for MCP clients, foreground (exec)
set -euo pipefail
cd "$(dirname "$0")/.."

TRANSPORT="${1:-http}"
case "$TRANSPORT" in
  http|stdio) ;;
  -h|--help)
    echo "usage: $0 [http|stdio]   (default: http)"
    exit 0
    ;;
  *)
    echo "unknown transport: $TRANSPORT (expected 'http' or 'stdio')" >&2
    exit 2
    ;;
esac

if [[ "$TRANSPORT" == "stdio" ]]; then
  # stdio is a foreground protocol channel — the MCP client owns the process.
  exec uv run protocol-x-tool-mcp serve --transport stdio
fi

DATA_DIR="${PROTOX_DATA_DIR:-$HOME/.local/share/protocol-x}"
HOST="${PROTOX_HOST:-127.0.0.1}"
PORT="${PROTOX_PORT:-7894}"
PID_FILE="$DATA_DIR/server.pid"
mkdir -p "$DATA_DIR"

if [[ -f "$PID_FILE" ]] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
  echo "already running (pid $(cat "$PID_FILE"))"
else
  nohup uv run protocol-x-tool-mcp serve --transport http >>"$DATA_DIR/server.log" 2>&1 &
  echo $! >"$PID_FILE"
  echo "started (pid $(cat "$PID_FILE"))"
fi

# Resolve the bearer token: env first, then the wizard's server.yaml.
TOKEN="${PROTOX_TOKENS:-}"
TOKEN="${TOKEN%%,*}"
if [[ -z "$TOKEN" && -f "$DATA_DIR/server.yaml" ]]; then
  TOKEN="$(uv run python - "$DATA_DIR/server.yaml" <<'PY'
import sys, yaml
cfg = yaml.safe_load(open(sys.argv[1])) or {}
tokens = (cfg.get("security") or {}).get("tokens") or []
print(tokens[0] if tokens else "")
PY
)"
fi

if [[ -n "$TOKEN" ]]; then
  URL="http://$HOST:$PORT/?token=$TOKEN"
  echo "bearer token: $TOKEN"
else
  URL="http://$HOST:$PORT"
  echo "no token configured — auth disabled (pure-localhost mode)"
fi
echo "console: $URL"

# Open the console in a desktop browser when one is available.
if [[ -n "${DISPLAY:-}" ]] && command -v xdg-open >/dev/null 2>&1; then
  xdg-open "$URL" >/dev/null 2>&1 &
fi
