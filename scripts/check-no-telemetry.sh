#!/usr/bin/env bash
# Telemetry gate (INV/privacy): Protocol-X never phones home. This CI check
# fails the build if first-party code references any telemetry/analytics
# service or tracking endpoint. Docs and tests may mention the absence.
set -euo pipefail
cd "$(dirname "$0")/.."

PATTERN='posthog|segment\.io|mixpanel|sentry\.io|google-analytics|googletagmanager|telemetry\.nebius|analytics\.[a-z]+\.(com|net|io)'

if grep -rEn "$PATTERN" server/ agents/ core/ adapters/ console/src/ 2>/dev/null; then
  echo "FAIL: telemetry/analytics reference found in first-party code" >&2
  exit 1
fi
echo "PASS: no telemetry or analytics references in first-party code"
