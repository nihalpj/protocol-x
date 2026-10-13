#!/usr/bin/env bash
# test-uc-01.sh — UC-01: Dev wraps a package (UF-02, F-029).
# The AI-judge is the only merge gate; without a Token Factory key it fails
# closed and the conversion ends in a terminal, machine-readable REJECTION
# (reason judge-blocked-unrepairable) — the correct degraded behavior (E-20,
# E-10, INV-6). The standalone demo path (stub judge) succeeds with a
# simulated merge.
set -euo pipefail
cd "$(dirname "$0")/.."

PROTOX_DATA_DIR="${PROTOX_DATA_DIR:-$(mktemp -d /tmp/protox-uc01-data.XXXXXX)}"
PROTOX_WORKSPACE="${PROTOX_WORKSPACE:-$(mktemp -d /tmp/protox-uc01-ws.XXXXXX)}"
export PROTOX_DATA_DIR PROTOX_WORKSPACE PROTOX_TF_API_KEY=""

pass() { printf 'PASS: %s\n' "$*"; }
fail() { printf 'FAIL: %s\n' "$*" >&2; exit 1; }

echo "== UC-01: converter judge gate (fail-closed rejection + stub-judge merge)"

# --- Step 1: production convert without a key -> terminal rejection ---------
CONVERT_LOG="$(mktemp /tmp/protox-uc01-convert.XXXXXX.log)"
set +e
uv run protocol-x-tool-mcp convert textwrap >"$CONVERT_LOG" 2>&1
rc=$?
set -e
cat "$CONVERT_LOG"
[[ $rc -eq 2 ]] || fail "convert textwrap expected exit 2 (rejected), got $rc"
grep -q "REJECTED: judge-blocked-unrepairable" "$CONVERT_LOG" \
  || fail "expected a judge-blocked-unrepairable rejection message"
pass "convert textwrap exits 2 with judge-blocked-unrepairable (fail closed, E-20)"

# --- Step 2: rejection record on disk + conversion_runs row -----------------
REJECTION_JSON="$(ls "$PROTOX_DATA_DIR"/conversions/*/rejection.json 2>/dev/null)" \
  || fail "no rejection.json under $PROTOX_DATA_DIR/conversions"
[[ "$(wc -l <<<"$REJECTION_JSON")" -eq 1 ]] \
  || fail "expected exactly one rejection.json, got: $REJECTION_JSON"

uv run python - "$REJECTION_JSON" <<'EOF'
import json
import sys

record = json.load(open(sys.argv[1]))
if record.get("reason_code") != "judge-blocked-unrepairable":
    raise SystemExit(f"FAIL: rejection.json reason_code={record.get('reason_code')!r}")
print(f"PASS: rejection.json reason_code=judge-blocked-unrepairable ({sys.argv[1]})")
EOF

uv run python - <<'EOF'
from server.app import AppContext
from server.config import Settings

ctx = AppContext(Settings())
rows = ctx.store.runs.query(
    package="textwrap", outcome="rejected", reason_code="judge-blocked-unrepairable"
)
ctx.store.close()
if len(rows) != 1:
    raise SystemExit(f"FAIL: expected exactly 1 conversion_runs row, got {len(rows)}")
print("PASS: conversion_runs row recorded "
      "(outcome=rejected, reason_code=judge-blocked-unrepairable)")
EOF

# --- Step 3: standalone demo path with the stub judge -> simulated merge ----
DEMO_LOG="$(mktemp /tmp/protox-uc01-demo.XXXXXX.log)"
uv run python agents/converter_agent.py textwrap --judge pass >"$DEMO_LOG" 2>&1 \
  || { cat "$DEMO_LOG"; fail "demo convert with --judge pass should exit 0"; }
tail -n 6 "$DEMO_LOG"
grep -q "auto-merged textwrap.py" "$DEMO_LOG" \
  || fail "demo conversion did not report the simulated merge"
grep -q "verdict=pass" "$DEMO_LOG" \
  || fail "demo conversion did not report verdict=pass"
pass "stub-judge demo conversion succeeds with a simulated merge"

echo "UC-01 PASS"
