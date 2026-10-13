#!/usr/bin/env bash
# Protocol-X Tool MCP — one-command install (F-031). No sudo, no system changes:
# everything lands in the project venv and ~/.local/share/protocol-x.
set -euo pipefail
cd "$(dirname "$0")/.."

echo "Protocol-X Tool MCP installer"

if ! command -v uv >/dev/null 2>&1; then
  echo "uv not found — installing the standalone uv binary (no sudo)..."
  curl -LsSf https://astral.sh/uv/install.sh | sh
  export PATH="$HOME/.local/bin:$PATH"
fi

uv sync

echo
echo "installed. Next:"
echo "  uv run protocol-x-tool-mcp setup    # guided first-run wizard"
echo "  uv run protocol-x-tool-mcp doctor   # verify the environment"
echo "  uv run protocol-x-tool-mcp serve    # stdio MCP transport"
echo
echo "Docker profile (optional, hardened): see deploy/docker/docker-compose.yml"
