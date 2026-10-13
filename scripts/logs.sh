#!/usr/bin/env bash
# logs.sh — view server logs.
#   ./logs.sh              follow the human-readable app log (default)
#   ./logs.sh calls        follow the structured tool-call log (JSONL, pretty)
#   ./logs.sh audit        follow the audit log (JSONL, pretty)
#   ./logs.sh paths        just print the log file locations
#   -n N                   start from the last N lines (default 50)
#   --no-follow            print and exit instead of following
set -euo pipefail

DATA_DIR="${PROTOX_DATA_DIR:-$HOME/.local/share/protocol-x}"
APP_LOG="$DATA_DIR/logs/server.log"
CALLS_LOG="$DATA_DIR/logs/tool-calls.jsonl"
AUDIT_LOG="$DATA_DIR/audit.jsonl"
# start.sh also captures raw stderr/stdout here (uvicorn, tracebacks):
RAW_LOG="$DATA_DIR/server.log"

WHICH="app"
N=50
FOLLOW=1
while [[ $# -gt 0 ]]; do
  case "$1" in
    app|server) WHICH="app" ;;
    calls|tools) WHICH="calls" ;;
    audit) WHICH="audit" ;;
    paths) WHICH="paths" ;;
    -n) N="$2"; shift ;;
    --no-follow) FOLLOW=0 ;;
    -h|--help)
      sed -n '2,9p' "$0"
      exit 0
      ;;
    *)
      echo "unknown argument: $1 (try --help)" >&2
      exit 2
      ;;
  esac
  shift
done

if [[ "$WHICH" == "paths" ]]; then
  echo "app log:        $APP_LOG"
  echo "raw stderr:     $RAW_LOG"
  echo "tool calls:     $CALLS_LOG"
  echo "audit:          $AUDIT_LOG"
  exit 0
fi

case "$WHICH" in
  app) FILE="$APP_LOG" ;;
  calls) FILE="$CALLS_LOG" ;;
  audit) FILE="$AUDIT_LOG" ;;
esac

if [[ ! -f "$FILE" ]]; then
  echo "no log yet: $FILE (is the server running? try scripts/status.sh)" >&2
  exit 1
fi

TAIL_ARGS=(-n "$N")
if [[ "$FOLLOW" == "1" ]]; then
  TAIL_ARGS+=(-f)
fi

pretty() {
  # Compact one-JSON-per-line pretty print when a python is available.
  if command -v python3 >/dev/null 2>&1; then
    python3 -c '
import json, sys
for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    try:
        print(json.dumps(json.loads(line), indent=None, sort_keys=False))
    except Exception:
        print(line)
'
  else
    cat
  fi
}

case "$WHICH" in
  app) tail "${TAIL_ARGS[@]}" "$FILE" ;;
  calls|audit) tail "${TAIL_ARGS[@]}" "$FILE" | pretty ;;
esac
