"""P7 converter agent: ReAct loop, judge gate, resume, DB persistence (F-029).

AC-P7-01 happy path · AC-P7-02 format auto-fix · AC-P7-03 judge-blocked
rejection · AC-P7-04 tool cap · AC-P7-05 restart-resume via DB checkpoint ·
AC-P7-06 production merge into the live registry · E-20 fail-closed degraded
mode.
"""

from __future__ import annotations

import json
import types

import pytest

from agents.converter_agent import (
    MAX_TOOLS,
    ConversionRejected,
    ConverterAgent,
    production_merger,
)
from server.convert_cli import run_convert


def _pass_judge(path):
    return "pass"


def _blocked_unrepairable(path):
    return {"verdict": "blocked", "findings": ["INV-1 violation: eval() on line 12"]}


def _blocked_repairable_forever(path):
    # "timeout" finding is always deterministically repairable -> the loop
    # repairs every cycle and must exhaust at MAX_JUDGE_CYCLES (E-10).
    return {"verdict": "blocked", "findings": ["timeout budget too high"]}


@pytest.fixture()
def workdir(tmp_path):
    return tmp_path / "convert"


def test_happy_path_stub_judge_merges(workdir):
    agent = ConverterAgent(
        "textwrap", judge=_pass_judge,
        merger=lambda p: "merged (test)", workdir=workdir)
    result = agent.run()
    assert result.verdict == "pass"
    assert 1 <= len(result.tools) <= MAX_TOOLS
    assert result.adapter_path.exists() and result.format_path.exists()
    trace = (workdir / "react_trace.jsonl").read_text().strip().splitlines()
    assert len(trace) >= 4  # introspect, propose, codegen, format, judge, merge


def test_format_fault_healed_within_budget(workdir):
    agent = ConverterAgent(
        "textwrap", demo_fault=True, judge=_pass_judge,
        merger=lambda p: "merged", workdir=workdir)
    result = agent.run()
    assert 1 <= result.fix_cycles <= 3
    assert result.format_path.exists()


def test_permanent_block_rejects_fail_closed(workdir):
    agent = ConverterAgent(
        "textwrap", judge=_blocked_unrepairable,
        merger=lambda p: "must not merge", workdir=workdir)
    with pytest.raises(ConversionRejected, match="judge-blocked-unrepairable"):
        agent.run()
    record = json.loads((workdir / "rejection.json").read_text())
    assert record["reason_code"] == "judge-blocked-unrepairable"
    assert record["package"] == "textwrap"


def test_judge_exhaustion_rejects_after_three_cycles(workdir):
    agent = ConverterAgent(
        "textwrap", judge=_blocked_repairable_forever,
        merger=lambda p: "must not merge", workdir=workdir)
    with pytest.raises(ConversionRejected, match="judge-blocked-exhausted"):
        agent.run()
    assert agent.judge_cycles == 3


def test_tool_surface_capped_at_ten(workdir):
    mod = types.ModuleType("fakepkg")
    src = "\n".join(
        f"def f{i}(x: str) -> str:\n    return x" for i in range(15))
    exec(src, mod.__dict__)
    agent = ConverterAgent("fakepkg", workdir=workdir)
    proposals = agent._propose(mod)
    assert len(proposals) == MAX_TOOLS


def test_restart_resume_from_db_checkpoint(ctx, workdir):
    """AC-P7-05: crash after 'format', resume from the checkpoints table."""
    store = ctx.store
    run_id = store.runs.open(
        "textwrap", str(workdir), str(workdir / "react_trace.jsonl"))

    class CrashAtMerge(Exception):
        pass

    def crashing_merger(path):
        raise CrashAtMerge("simulated process death")

    agent = ConverterAgent(
        "textwrap", judge=_pass_judge, merger=crashing_merger,
        workdir=workdir, store=store, run_id=run_id)
    with pytest.raises(CrashAtMerge):
        agent.run()

    ckpt = store.checkpoints.load(run_id)
    assert ckpt["stage"] == "format"  # interrupted before merge

    resumed = ConverterAgent(
        "textwrap", judge=_pass_judge, merger=lambda p: "merged after resume",
        workdir=workdir, resume=True, store=store, run_id=run_id,
        resume_stage=ckpt["stage"])
    result = resumed.run()
    assert result.verdict == "pass"
    assert store.checkpoints.load(run_id)["stage"] == "merged"
    assert any("resume from checkpoint" in s.thought for s in resumed.tracer.steps)


def test_run_convert_degraded_mode_fails_closed(ctx, settings):
    """E-20: no Token Factory key -> judge fails closed -> rejection, and the
    conversion_runs row is queryable by reason_code."""
    rc = run_convert("textwrap", settings, ctx=ctx,
                     workdir=settings.data_dir / "conversions" / "t1")
    assert rc == 2
    rows = ctx.store.runs.query(reason_code="judge-blocked-unrepairable")
    assert len(rows) == 1
    assert rows[0]["package"] == "textwrap" and rows[0]["outcome"] == "rejected"


def test_production_merger_makes_tools_live(ctx, workdir):
    agent = ConverterAgent(
        "textwrap", judge=_pass_judge,
        merger=production_merger(ctx, "textwrap"), workdir=workdir)
    try:
        result = agent.run()
        assert result.verdict == "pass"
        for name in result.tools:
            assert ctx.registry.entry_state(name) == "live"
            assert ctx.store.tools.get(name)["state"] == "live"
        # merged adapter is dispatchable through the real registry
        spec = ctx.registry.get_spec(result.tools[0])
        assert spec.name in result.tools
        version = ctx.store.versions.latest("textwrap")
        assert version is not None and version["source"] == "generated"
    finally:
        target = ctx.registry.adapters_dir / "textwrap.py"
        if target.exists():
            target.unlink()
