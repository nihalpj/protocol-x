#!/usr/bin/env bash
# smoke.sh — F-034: assert the OpenShell cage denies writes outside the workspace.
# Requires: openshell CLI + the built image. Skips green when unavailable.
set -euo pipefail
cd "$(dirname "$0")/../.."

if ! command -v openshell >/dev/null 2>&1; then
  echo "SKIP: openshell not installed on this host (F-034 is optional)"
  exit 0
fi

IMAGE="${1:-protocol-x-tool-mcp:latest}"

echo "1) write inside workspace must succeed"
openshell run --profile policies/openshell/profile.yaml -- "$IMAGE" \
  python -c "open('/data/workspace/probe.txt','w').write('ok'); print('workspace write ok')"

echo "2) write outside workspace must be denied by the kernel"
if openshell run --profile policies/openshell/profile.yaml -- "$IMAGE" \
  python -c "open('/etc/probe.txt','w').write('x')" 2>/dev/null; then
  echo "FAIL: kernel allowed a write outside the workspace"
  exit 1
fi
echo "kernel denied the escape write — cage OK"

echo "3) non-allowlisted egress must be denied"
if openshell run --profile policies/openshell/profile.yaml -- "$IMAGE" \
  python -c "import socket; socket.create_connection(('example.com', 443), timeout=3)" 2>/dev/null; then
  echo "FAIL: egress policy allowed a non-allowlisted destination"
  exit 1
fi
echo "egress policy denies non-allowlisted destinations — cage OK"
