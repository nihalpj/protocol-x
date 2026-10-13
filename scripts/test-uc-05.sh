#!/usr/bin/env bash
# test-uc-05.sh — UC-05: Sam, security audit battery (INV-1…8, F-008, F-033).
# Runs the pytest security + adversarial suites, then live checks: the SSRF
# guard blocks loopback and non-http schemes, workspace path escapes are
# rejected, and sandbox workers spawn keyless (no configured secret reaches
# the worker environment).
set -euo pipefail
cd "$(dirname "$0")/.."

PROTOX_DATA_DIR="${PROTOX_DATA_DIR:-$(mktemp -d /tmp/protox-uc05-data.XXXXXX)}"
PROTOX_WORKSPACE="${PROTOX_WORKSPACE:-$(mktemp -d /tmp/protox-uc05-ws.XXXXXX)}"
export PROTOX_DATA_DIR PROTOX_WORKSPACE PROTOX_TF_API_KEY=""
export PROTOX_SECRETS='{"UC05_TEST_SECRET": "uc05-super-secret-value"}'

pass() { printf 'PASS: %s\n' "$*"; }
fail() { printf 'FAIL: %s\n' "$*" >&2; exit 1; }

echo "== UC-05: security audit (SSRF battery / workspace confinement / keyless workers)"

uv run pytest -q tests/security tests/adversarial
pass "pytest tests/security + tests/adversarial (41 tests: SSRF, presets/secrets, judge corpus)"

uv run python - <<'EOF'
from adapters.base import SSRF_BLOCKED, VALIDATION_ERROR, ToolError
from core.sandbox.runner import SubprocessRunner
from server.app import AppContext
from server.config import Settings


def expect_tool_error(fn, code, msg):
    try:
        fn()
    except ToolError as exc:
        if exc.code != code:
            raise SystemExit(f"FAIL: {msg} — expected {code}, got {exc.code}: {exc.message}")
        print(f"PASS: {msg} ({code})")
        return
    raise SystemExit(f"FAIL: {msg} — call unexpectedly succeeded")


ctx = AppContext(Settings())
try:
    expect_tool_error(
        lambda: ctx.dispatch("http_get", {"url": "http://127.0.0.1/", "timeout_s": 5}),
        SSRF_BLOCKED, "SSRF guard blocks http_get to 127.0.0.1")
    expect_tool_error(
        lambda: ctx.dispatch("http_get", {"url": "file:///etc/passwd"}),
        SSRF_BLOCKED, "SSRF guard rejects non-http schemes (file://)")
    expect_tool_error(
        lambda: ctx.dispatch("csv_info", {"path": "../outside.csv"}),
        VALIDATION_ERROR, "workspace rejects '..' path escapes")
    expect_tool_error(
        lambda: ctx.dispatch("csv_info", {"path": "/etc/passwd"}),
        VALIDATION_ERROR, "workspace rejects absolute paths outside the root")
finally:
    ctx.store.close()

env = SubprocessRunner().worker_env()
if "UC05_TEST_SECRET" in env or "uc05-super-secret-value" in env.values():
    raise SystemExit("FAIL: a configured secret leaks into the worker environment")
if "PROTOX_TF_API_KEY" in env:
    raise SystemExit("FAIL: PROTOX_TF_API_KEY leaks into the worker environment")
print(f"PASS: worker env is keyless ({len(env)} scrubbed vars, no configured secrets, AC-F033-01)")
EOF

echo "UC-05 PASS"
