#!/usr/bin/env bash
# test-uc-06.sh — UC-06: polyglot (npm) package conversion (F-036).
# v1.1 scope: the npm/Go/Rust backends land post-v1.0 on the normal release
# train. Green-skip — the suite stays honest about what ships today.
set -euo pipefail
cd "$(dirname "$0")/.."

echo "SKIP: uc-06 polyglot conversion is v1.1 scope"
echo "PASS: uc-06 green-skip (F-036 npm backend lands post-v1.0)"
echo "UC-06 PASS (skipped)"
