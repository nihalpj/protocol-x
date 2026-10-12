#!/usr/bin/env bash
# doctor.sh — Protocol-X Tool MCP diagnostics (F-035). Full mode + --quick.
# Exit non-zero when any check is red.
set -euo pipefail
cd "$(dirname "$0")/.."

MODE="full"
if [[ "${1:-}" == "--quick" ]]; then
  MODE="quick"
fi

if [[ "$MODE" == "quick" ]]; then
  exec uv run python -m server.doctor --quick
else
  exec uv run python -m server.doctor
fi
