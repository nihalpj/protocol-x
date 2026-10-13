#!/usr/bin/env bash
# test-uc-04.sh — UC-04: Petra, team deploy (F-014, F-032, F-035, F-038).
# Bearer tokens hashed with per-tool ACLs (analysts never reach http_get),
# the strict policy preset disabling the http_* tools at the registry, a
# green pre-flight doctor, and env-override sizing for the host profile.
set -euo pipefail
cd "$(dirname "$0")/.."

PROTOX_DATA_DIR="${PROTOX_DATA_DIR:-$(mktemp -d /tmp/protox-uc04-data.XXXXXX)}"
PROTOX_WORKSPACE="${PROTOX_WORKSPACE:-$(mktemp -d /tmp/protox-uc04-ws.XXXXXX)}"
export PROTOX_DATA_DIR PROTOX_WORKSPACE PROTOX_TF_API_KEY=""
export PROTOX_TOKENS="uc04-analyst,uc04-admin"
export PROTOX_TOOL_ACL='{"uc04-analyst": ["csv_info", "csv_read", "csv_filter", "csv_aggregate"]}'

pass() { printf 'PASS: %s\n' "$*"; }
fail() { printf 'FAIL: %s\n' "$*" >&2; exit 1; }

echo "== UC-04: team deploy (tokens + ACL + presets + sizing)"

uv run python - <<'EOF'
from starlette.testclient import TestClient

from adapters.base import TOOL_NOT_PERMITTED, ToolError
from server.app import AppContext, build_http_app
from server.auth import TokenStore, current_token
from server.config import Settings


def check(cond, msg):
    if not cond:
        raise SystemExit(f"FAIL: {msg}")
    print(f"PASS: {msg}")


settings = Settings()
check(len(settings.tokens) == 2, "PROTOX_TOKENS parsed (2 tokens)")
check(settings.tool_acl.get("uc04-analyst")
      == ["csv_info", "csv_read", "csv_filter", "csv_aggregate"],
      "PROTOX_TOOL_ACL parsed (analyst allowlist)")

store = TokenStore(settings.tokens, settings.tool_acl)
check(store.enabled, "TokenStore enabled (tokens stored hashed)")
store.check_acl("uc04-analyst", "csv_info")
print("PASS: analyst token permitted on csv_info")
try:
    store.check_acl("uc04-analyst", "http_get")
    raise SystemExit("FAIL: analyst token reached http_get — ACL not enforced")
except ToolError as exc:
    check(exc.code == TOOL_NOT_PERMITTED,
          "analyst token blocked from http_get with TOOL_NOT_PERMITTED")

ctx = AppContext(settings)
try:
    token = current_token.set("uc04-analyst")
    try:
        ctx.dispatch("http_get", {"url": "http://example.com/", "timeout_s": 5})
        raise SystemExit("FAIL: dispatch allowed a tool outside the token ACL")
    except ToolError as exc:
        check(exc.code == TOOL_NOT_PERMITTED,
              "dispatcher enforces the ACL before any work (TOOL_NOT_PERMITTED)")
    finally:
        current_token.reset(token)

    http = TestClient(build_http_app(ctx))
    resp = http.post("/mcp", json={})
    check(resp.status_code == 401,
          f"HTTP transport rejects tokenless /mcp with 401 (got {resp.status_code})")
finally:
    ctx.store.close()
EOF

PROTOX_POLICY_PRESET=strict uv run python - <<'EOF'
from server.app import AppContext
from server.config import Settings


def check(cond, msg):
    if not cond:
        raise SystemExit(f"FAIL: {msg}")
    print(f"PASS: {msg}")


ctx = AppContext(Settings())
try:
    names = {e.name for e in ctx.registry.list_tools()}
    http_tools = {"http_get", "http_post", "http_head", "http_download_file"}
    check(not (http_tools & names), "strict preset disables http_* tools (F-032 per-tool overrides)")
    print(f"PASS: strict registry size = {len(names)} tools (network-egress ships 34)")
finally:
    ctx.store.close()
EOF

uv run protocol-x-tool-mcp doctor --quick
pass "doctor --quick green with tokens + ACL configured"

PROTOX_CPUS=4 PROTOX_MEMORY=8g PROTOX_WORKER_POOL=3 uv run python - <<'EOF'
from core.sizing import profile_for_settings
from server.config import Settings

profile = profile_for_settings(Settings())
if profile.cpus != 4.0 or profile.memory_mb != 8192 or profile.worker_pool != 3:
    raise SystemExit(
        "FAIL: env overrides not applied: "
        f"cpus={profile.cpus} memory={profile.memory_mb} pool={profile.worker_pool}")
expected = {"PROTOX_CPUS": "4", "PROTOX_MEMORY": "8g", "PROTOX_WORKER_POOL": "3"}
if profile.overrides != expected:
    raise SystemExit(f"FAIL: overrides not reported: {profile.overrides}")
print(f"PASS: sizing honors env overrides (cpus=4 memory=8192MB pool=3, tier={profile.tier})")
EOF

echo "UC-04 PASS"
