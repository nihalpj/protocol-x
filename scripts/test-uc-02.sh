#!/usr/bin/env bash
# test-uc-02.sh — UC-02: Dev watches the runtime self-heal (UF-03, F-030).
# Runs the dedicated pytest module (heal / rollback / circuit breaker /
# degraded-mode rollback), then drives one heal end-to-end with a stub Token
# Factory client + stub judge against a throwaway data dir, asserting the
# repair_incidents rows, the hot swap, and the audit trail.
set -euo pipefail
cd "$(dirname "$0")/.."

PROTOX_DATA_DIR="${PROTOX_DATA_DIR:-$(mktemp -d /tmp/protox-uc02-data.XXXXXX)}"
PROTOX_WORKSPACE="${PROTOX_WORKSPACE:-$(mktemp -d /tmp/protox-uc02-ws.XXXXXX)}"
export PROTOX_DATA_DIR PROTOX_WORKSPACE PROTOX_TF_API_KEY=""

pass() { printf 'PASS: %s\n' "$*"; }
fail() { printf 'FAIL: %s\n' "$*" >&2; exit 1; }

echo "== UC-02: runtime self-repair (capture -> heal -> hot swap)"

uv run pytest -q tests/integration/test_repair.py
pass "pytest tests/integration/test_repair.py (7 tests: heal/rollback/breaker/degraded)"

uv run python - <<'EOF'
"""Drive one seeded fault through the heal loop with stub seams (no network)."""
from types import SimpleNamespace

from adapters.base import ADAPTER_ERROR, ToolError
from agents.judge import JudgeVerdict
from server.app import AppContext
from server.config import Settings
from server.registry import module_source_hash

BROKEN_SOURCE = '''from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from adapters.base import ADAPTER_ERROR, ToolError, ToolSpec


class FakerepFlakyIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str = Field(description="input text")


def fakerep_flaky(ws, text: str) -> dict[str, Any]:
    raise ToolError(ADAPTER_ERROR, "seeded fault", {})


TOOLS = [
    ToolSpec(
        name="fakerep_flaky",
        description="always-failing generated tool",
        input_model=FakerepFlakyIn,
        handler=fakerep_flaky,
        timeout_s=30,
        network=False,
    ),
]
'''
FIXED_SOURCE = BROKEN_SOURCE.replace(
    'raise ToolError(ADAPTER_ERROR, "seeded fault", {})',
    'return {"result": f"fixed:{text}"}',
)


class StubClient:
    def __init__(self, source: str):
        self.source = source

    def chat(self, model, messages, temperature=0.0):
        return SimpleNamespace(
            content=f"```python\n{self.source}\n```", model=model, prompt_hash="stub")


class StubJudge:
    def review(self, source, module):
        return JudgeVerdict(verdict="pass", findings=[], model="stub", rubric_version="1.2")


def check(cond, msg):
    if not cond:
        raise SystemExit(f"FAIL: {msg}")
    print(f"PASS: {msg}")


ctx = AppContext(Settings())
module = ctx.registry.adapters_dir / "fakerep.py"
module.write_text(BROKEN_SOURCE)
try:
    ctx.registry.refresh_module("fakerep")
    ctx.store.versions.add(
        "fakerep", module_source_hash(ctx.registry.adapters_dir, "fakerep"), "generated")

    agent = ctx.repairer
    agent._client = StubClient(FIXED_SOURCE)
    agent._judge = StubJudge()

    exc = ToolError(ADAPTER_ERROR, "seeded fault")
    i1 = agent.maybe_capture("fakerep_flaky", exc, {"text": "x"}, sync=True)
    check(ctx.store.incidents.get(i1)["state"] == "healing",
          "first failure captured as repair_incident state=healing (below threshold)")
    i2 = agent.maybe_capture("fakerep_flaky", exc, {"text": "x"}, sync=True)
    check(ctx.store.incidents.get(i2)["state"] == "healed",
          "threshold reached -> repair attempt heals; incident state=healed")

    out = ctx.dispatch("fakerep_flaky", {"text": "hello"})
    check(out["result"]["data"]["result"] == "fixed:hello",
          "hot swap: the patched adapter serves the call")

    states = {r["state"] for r in ctx.store.incidents.list(tool="fakerep_flaky")}
    check(states == {"healing", "healed"},
          f"repair_incidents rows end healthy (states={sorted(states)})")
finally:
    module.unlink(missing_ok=True)
    ctx.store.close()
EOF

grep -q '"event": "captured"' "$PROTOX_DATA_DIR/audit.jsonl" \
  || fail "audit.jsonl missing the repair 'captured' event"
grep -q '"event": "healed"' "$PROTOX_DATA_DIR/audit.jsonl" \
  || fail "audit.jsonl missing the repair 'healed' event"
pass "audit trail records captured + healed repair events"

echo "UC-02 PASS"
