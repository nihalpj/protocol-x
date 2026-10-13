"""P8 runtime self-repair (F-030): capture, heal, rollback, circuit breaker.

AC-P8-01 seeded fault heals via patch->gate->judge->smoke replay->hot swap ·
AC-P8-02 judge-blocked patch rolls back · AC-P8-03 breaker quarantines after
3 failed repairs/24h (TOOL_DISABLED) · AC-P8-04 VALIDATION_ERROR never
triggers repair · AC-P8-05 breaker state survives a restart · console
re-enable resets the breaker.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from adapters.base import ADAPTER_ERROR, TOOL_DISABLED, VALIDATION_ERROR, ToolError
from agents.judge import JudgeVerdict

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
            content=f"```python\n{self.source}\n```", model=model,
            prompt_hash="stub")


class StubJudge:
    def __init__(self, verdict: str):
        self.verdict = verdict

    def review(self, source, module):
        return JudgeVerdict(verdict=self.verdict, findings=[], model="stub",
                            rubric_version="1.2")


@pytest.fixture()
def rep_ctx(ctx):
    module = ctx.registry.adapters_dir / "fakerep.py"
    module.write_text(BROKEN_SOURCE)
    ctx.registry.refresh_module("fakerep")
    from server.registry import module_source_hash

    digest = module_source_hash(ctx.registry.adapters_dir, "fakerep")
    ctx.store.versions.add("fakerep", digest, "generated")
    yield ctx
    module.unlink(missing_ok=True)


def _seed_failure(agent, sync=True):
    exc = ToolError(ADAPTER_ERROR, "seeded fault")
    return agent.maybe_capture("fakerep_flaky", exc, {"text": "x"}, sync=sync)


def test_seeded_fault_heals_end_to_end(rep_ctx):
    agent = rep_ctx.repairer
    agent._client = StubClient(FIXED_SOURCE)
    agent._judge = StubJudge("pass")

    i1 = _seed_failure(agent)
    assert i1 is not None
    assert rep_ctx.store.incidents.get(i1)["state"] == "healing"  # below threshold

    i2 = _seed_failure(agent)
    assert rep_ctx.store.incidents.get(i2)["state"] == "healed"

    # hot-swapped: the live registry now serves the patched adapter
    out = rep_ctx.dispatch("fakerep_flaky", {"text": "hello"})
    assert out["result"]["data"]["result"] == "fixed:hello"

    # version chain: the patch is a child of the broken version
    latest = rep_ctx.store.versions.latest("fakerep")
    assert latest["parent_id"] is not None and latest["source"] == "generated"


def test_judge_blocked_patch_rolls_back(rep_ctx):
    agent = rep_ctx.repairer
    agent._client = StubClient(FIXED_SOURCE)
    agent._judge = StubJudge("blocked")
    path = rep_ctx.registry.adapters_dir / "fakerep.py"
    original = path.read_text()

    _seed_failure(agent)
    i2 = _seed_failure(agent)
    incident = rep_ctx.store.incidents.get(i2)
    assert incident["state"] == "rolled-back"
    assert incident["attempts"] == 1
    assert path.read_text() == original  # restored byte-for-byte
    assert rep_ctx.registry.entry_state("fakerep_flaky") == "live"


def test_circuit_breaker_quarantines_after_three_failures(rep_ctx, settings):
    store = rep_ctx.store
    for _ in range(3):
        iid = store.incidents.add("fakerep_flaky", ADAPTER_ERROR)
        store.incidents.set_state(iid, "rolled-back", 1)
    iid = store.incidents.add("fakerep_flaky", ADAPTER_ERROR)
    result = rep_ctx.repairer.attempt_repair(iid)
    assert result["state"] == "quarantined"
    assert rep_ctx.registry.entry_state("fakerep_flaky") == "quarantined"
    with pytest.raises(ToolError) as ei:
        rep_ctx.registry.get_spec("fakerep_flaky")
    assert ei.value.code == TOOL_DISABLED

    # AC-P8-05: breaker state is SQL-derived — survives a full restart
    from server.app import AppContext

    ctx2 = AppContext(settings)
    try:
        assert ctx2.repairer._breaker_tripped("fakerep_flaky")
        assert ctx2.registry.entry_state("fakerep_flaky") == "quarantined"
    finally:
        ctx2.store.close()


def test_reenable_resets_breaker(rep_ctx):
    store = rep_ctx.store
    agent = rep_ctx.repairer
    for _ in range(3):
        iid = store.incidents.add("fakerep_flaky", ADAPTER_ERROR)
        store.incidents.set_state(iid, "rolled-back", 1)
    iid = store.incidents.add("fakerep_flaky", ADAPTER_ERROR)
    assert agent.attempt_repair(iid)["state"] == "quarantined"

    agent.reenable("fakerep_flaky")
    assert rep_ctx.registry.entry_state("fakerep_flaky") == "live"
    assert not agent._breaker_tripped("fakerep_flaky")


def test_validation_errors_never_trigger_repair(rep_ctx):
    exc = ToolError(VALIDATION_ERROR, "bad args")
    assert rep_ctx.repairer.maybe_capture("fakerep_flaky", exc, {}) is None
    assert rep_ctx.store.incidents.list(tool="fakerep_flaky") == []


def test_built_in_adapters_never_self_repair(ctx):
    exc = ToolError(ADAPTER_ERROR, "boom")
    assert ctx.repairer.maybe_capture("image_info", exc, {}) is None
    assert ctx.store.incidents.list(tool="image_info") == []


def test_degraded_mode_rolls_back_without_model(rep_ctx):
    # E-20: no Token Factory client at all -> attempt fails closed, file intact
    agent = rep_ctx.repairer
    agent._client = None
    agent._judge = None
    path = rep_ctx.registry.adapters_dir / "fakerep.py"
    original = path.read_text()
    _seed_failure(agent)
    i2 = _seed_failure(agent)
    assert rep_ctx.store.incidents.get(i2)["state"] == "rolled-back"
    assert path.read_text() == original
