#!/usr/bin/env bash
# test-uc-07.sh — UC-07: degraded mode without a Token Factory key (E-20, F-035).
# doctor exits 0 with the token-factory check WARN (not FAIL), the 34 launch-
# core tools stay fully live, and the builder judge fails closed (no key ->
# no judge seam -> verdict blocked, nothing can merge).
set -euo pipefail
cd "$(dirname "$0")/.."

PROTOX_DATA_DIR="${PROTOX_DATA_DIR:-$(mktemp -d /tmp/protox-uc07-data.XXXXXX)}"
PROTOX_WORKSPACE="${PROTOX_WORKSPACE:-$(mktemp -d /tmp/protox-uc07-ws.XXXXXX)}"
export PROTOX_DATA_DIR PROTOX_WORKSPACE PROTOX_TF_API_KEY=""

pass() { printf 'PASS: %s\n' "$*"; }
fail() { printf 'FAIL: %s\n' "$*" >&2; exit 1; }

echo "== UC-07: degraded mode, no Token Factory key"

DOCTOR_LOG="$(mktemp /tmp/protox-uc07-doctor.XXXXXX.log)"
set +e
uv run protocol-x-tool-mcp doctor >"$DOCTOR_LOG" 2>&1
rc=$?
set -e
cat "$DOCTOR_LOG"
[[ $rc -eq 0 ]] || fail "doctor must exit 0 in degraded mode (yellow is not red), got $rc"
grep -q "WARN] token-factory: no Token Factory key — degraded mode" "$DOCTOR_LOG" \
  || fail "doctor must WARN the token-factory check with degraded-mode messaging"
pass "doctor exits 0; token-factory check is WARN with degraded-mode messaging (E-20)"

uv run python - <<'EOF'
from agents.converter_agent import ConverterAgent, production_judge
from server.app import AppContext
from server.config import Settings


def check(cond, msg):
    if not cond:
        raise SystemExit(f"FAIL: {msg}")
    print(f"PASS: {msg}")


settings = Settings()
ctx = AppContext(settings)
try:
    tools = ctx.registry.list_tools()
    check(len(tools) >= 34,
          f"{len(tools)} core tools live in degraded mode (launch core = 34)")
finally:
    ctx.store.close()

check(settings.agent_features_enabled is False,
      "agent features disabled without a key (E-20)")
check(production_judge(settings, "anything") is None,
      "builder judge seam is absent without a key — the fail-closed default applies")

agent = ConverterAgent("textwrap")  # standalone demo constructor (stub seams)
verdict = agent.judge("adapter.py")
if verdict != "blocked":
    raise SystemExit(f"FAIL: fail-closed judge returned {verdict!r}, expected 'blocked'")
print("PASS: builder judge fails closed (verdict=blocked — no merge possible)")
EOF

echo "UC-07 PASS"
