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
    MAX_JUDGE_CYCLES,
    MAX_TOOLS,
    Arg,
    ConversionRejected,
    ConverterAgent,
    ToolProposal,
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


def test_judge_exhaustion_rejects_after_budget(workdir):
    agent = ConverterAgent(
        "textwrap", judge=_blocked_repairable_forever,
        merger=lambda p: "must not merge", workdir=workdir)
    with pytest.raises(ConversionRejected, match="judge-blocked-exhausted"):
        agent.run()
    assert agent.judge_cycles == MAX_JUDGE_CYCLES


def test_tool_surface_capped_at_max(workdir):
    mod = types.ModuleType("fakepkg")
    src = "\n".join(
        f"def f{i}(x: str) -> str:\n    return x" for i in range(MAX_TOOLS + 10))
    exec(src, mod.__dict__)
    agent = ConverterAgent("fakepkg", workdir=workdir)
    proposals = agent._propose(mod)
    assert len(proposals) == MAX_TOOLS


def test_reexported_functions_are_proposed(workdir):
    """pandas/idna-style packages re-export submodule functions at top level;
    the __module__ == package filter used to reject all of them."""
    pkg = types.ModuleType("fakepkg")
    sub = types.ModuleType("fakepkg.core")
    exec("def encode(data: str, strict: bool = False) -> str:\n    return data",
         sub.__dict__)
    pkg.encode = sub.encode
    agent = ConverterAgent("fakepkg", workdir=workdir)
    proposals = agent._propose(pkg)
    assert [p.target for p in proposals] == ["encode"]
    assert proposals[0].args[1].type == "bool"


def test_namespace_package_scans_submodules(workdir):
    """Pure-namespace packages (nothing at top level) fall back to a bounded
    submodule scan with dotted targets — packaging.version.parse etc."""
    import packaging

    agent = ConverterAgent("packaging", workdir=workdir)
    proposals = agent._propose(packaging)
    assert proposals
    assert any("." in p.target for p in proposals)


def test_code_shaped_params_screened_at_proposal_time(workdir):
    """INV-1: params that look like free-form code/query strings
    (expr/code/query_string/...) are screened out when the surface is
    proposed — a required one condemns the function, a defaulted one is
    skipped like any other unusable param — so pandas.eval-style tools never
    reach the codegen static gate."""
    mod = types.ModuleType("fakepkg")
    exec("def evaluate(expr: str) -> str:\n    return expr\n"
         "def render(template: str, script: str = '') -> str:\n    return template",
         mod.__dict__)
    agent = ConverterAgent("fakepkg", workdir=workdir)
    proposals = agent._propose(mod)
    assert [p.target for p in proposals] == ["render"]
    assert [a.name for a in proposals[0].args] == ["template"]
    assert proposals[0].skipped == ["script"]


def test_non_literal_default_skipped_not_fatal(workdir):
    """A param with a non-literal default is skipped (package default
    applies) instead of condemning the whole function."""
    mod = types.ModuleType("fakepkg")
    exec("def f(x: str, sentinel=object()) -> str:\n    return x", mod.__dict__)
    agent = ConverterAgent("fakepkg", workdir=workdir)
    proposals = agent._propose(mod)
    assert len(proposals) == 1
    assert [a.name for a in proposals[0].args] == ["x"]
    assert proposals[0].skipped == ["sentinel"]


def test_unannotated_param_type_inferred_from_default(workdir):
    mod = types.ModuleType("fakepkg")
    exec("def f(x: str, verbose=False) -> str:\n    return x", mod.__dict__)
    agent = ConverterAgent("fakepkg", workdir=workdir)
    proposals = agent._propose(mod)
    assert proposals[0].args[1].type == "bool"


def test_dict_annotation_maps_to_object_schema(workdir):
    mod = types.ModuleType("fakepkg")
    exec("def f(fields: dict, note=None) -> str:\n    return ''", mod.__dict__)
    agent = ConverterAgent("fakepkg", workdir=workdir)
    proposals = agent._propose(mod)
    schemas = agent._schemas(proposals)
    props = schemas[0]["input_schema"]["properties"]
    assert props["fields"] == {"type": "object"}
    assert props["note"] == {"type": ["string", "null"]}


def test_usage_ranking_prefers_public_api(workdir):
    """__all__ members (the declared public API = the most-used functions)
    rank ahead of undocumented internals regardless of alphabet."""
    mod = types.ModuleType("fakepkg")
    exec("def aaa_internal(x: str) -> str:\n    return x\n"
         "def zzz_public(x: str) -> str:\n    '''Documented public helper.'''\n    return x",
         mod.__dict__)
    mod.__all__ = ["zzz_public"]
    agent = ConverterAgent("fakepkg", workdir=workdir)
    proposals = agent._propose(mod)
    assert proposals[0].target == "zzz_public"


def test_proposer_selects_surface_when_configured(workdir):
    """With a proposer seam configured, its top-50 selection wins over the
    heuristic surface (it picks the most-used functions, not the first
    introspected ones)."""
    picked = [ToolProposal(tool_name="fakepkg_chosen", target="wrap",
                           description="chosen by the proposer")]
    agent = ConverterAgent(
        "textwrap", judge=_pass_judge, merger=lambda p: "merged",
        proposer=lambda mod: picked, workdir=workdir)
    result = agent.run()
    assert result.tools == ["fakepkg_chosen"]


def test_heuristic_fallback_when_proposer_empty(workdir):
    agent = ConverterAgent(
        "textwrap", judge=_pass_judge, merger=lambda p: "merged",
        proposer=lambda mod: [], workdir=workdir)
    result = agent.run()
    assert result.tools  # usage-ranked heuristic surface took over


def test_static_gate_drops_only_offending_tool(workdir):
    """A tool whose param trips the INV-1 lint (e.g. yarl's query_string) is
    dropped surgically — the rest of the surface still merges."""
    agent = ConverterAgent("textwrap", workdir=workdir)
    proposals = [
        ToolProposal(tool_name="textwrap_wrap", target="wrap",
                     description="wrap text",
                     args=[Arg("text", "str", "text argument")]),
        ToolProposal(tool_name="textwrap_run_query", target="wrap",
                     description="runs a query string",
                     args=[Arg("query_string", "str", "query_string argument")]),
    ]
    path = agent._codegen_adapter(proposals)
    assert [p.tool_name for p in proposals] == ["textwrap_wrap"]
    assert "query_string" not in path.read_text()


def test_static_gate_rejects_when_nothing_survives(workdir):
    agent = ConverterAgent("textwrap", workdir=workdir)
    proposals = [
        ToolProposal(tool_name="textwrap_run_query", target="wrap",
                     description="runs a query string",
                     args=[Arg("query_string", "str", "query_string argument")]),
    ]
    with pytest.raises(ConversionRejected, match="not-convertible"):
        agent._codegen_adapter(proposals)


# ------------------------------------------------------- adapter test stage


def test_tool_testing_passes_clean_surface(workdir):
    seen = []

    def tester(adapter_path, proposals):
        seen.append([p.tool_name for p in proposals])
        return {}  # all tools pass

    agent = ConverterAgent(
        "textwrap", judge=_pass_judge, merger=lambda p: "merged",
        tester=tester, workdir=workdir)
    result = agent.run()
    assert result.verdict == "pass" and result.tools
    assert seen == [result.tools]  # every proposed tool was tested
    trace = (workdir / "react_trace.jsonl").read_text()
    assert '"kind": "test"' in trace
    assert agent.tracer.completed_stage() == "merged"  # 'tested' stage passed


def test_failing_tool_fixed_via_bounded_mutation(workdir):
    calls = {"n": 0}

    def tester(adapter_path, proposals):
        calls["n"] += 1
        if calls["n"] == 1:
            # 'timeout' trips the deterministic repair (budget clamp)
            return {proposals[0].tool_name: "timeout budget exceeded in sample call"}
        return {}

    agent = ConverterAgent(
        "textwrap", judge=_pass_judge, merger=lambda p: "merged",
        tester=tester, workdir=workdir)
    result = agent.run()
    assert result.verdict == "pass"
    assert result.test_cycles == 1 and result.tools  # fixed, nothing dropped
    assert agent.timeout_s == 15


def test_unfixable_tool_marked_failed_and_removed(workdir):
    def tester(adapter_path, proposals):
        # one tool always fails with an error no deterministic fix matches
        return {p.tool_name: "RuntimeError: boom" for p in proposals
                if p.tool_name == "textwrap_indent"}

    agent = ConverterAgent(
        "textwrap", judge=_pass_judge, merger=lambda p: "merged",
        tester=tester, workdir=workdir)
    result = agent.run()
    assert result.verdict == "pass"
    assert "textwrap_indent" not in result.tools
    assert len(result.tools) >= 1
    saved = json.loads((workdir / "proposals.json").read_text())
    assert all(p["tool_name"] != "textwrap_indent" for p in saved)
    trace = (workdir / "react_trace.jsonl").read_text()
    assert "mark failed and remove" in trace


def test_all_tools_fail_rejects_not_convertible(workdir):
    def tester(adapter_path, proposals):
        return {p.tool_name: "RuntimeError: boom" for p in proposals}

    agent = ConverterAgent(
        "textwrap", judge=_pass_judge, merger=lambda p: "must not merge",
        tester=tester, workdir=workdir)
    with pytest.raises(ConversionRejected, match="not-convertible"):
        agent.run()
    record = json.loads((workdir / "rejection.json").read_text())
    assert record["reason_code"] == "not-convertible"


def test_llm_code_fix_accepted_only_if_static_gate_passes(workdir):
    calls = {"n": 0}

    def tester(adapter_path, proposals):
        calls["n"] += 1
        if calls["n"] == 1:
            return {proposals[0].tool_name: "RuntimeError: boom"}
        return {}

    def bad_fixer(source, failures):
        return source + "\neval('1')  # smuggled code execution\n"

    def good_fixer(source, failures):
        return source  # unchanged source passes the gate; re-test then passes

    bad = ConverterAgent(
        "textwrap", judge=_pass_judge, merger=lambda p: "merged",
        tester=tester, fixer=bad_fixer, workdir=workdir / "bad")
    result = bad.run()
    # gate rejected the poisoned patch -> tool marked failed and removed,
    # but the surviving tools still merge
    assert result.verdict == "pass"
    trace = (workdir / "bad" / "react_trace.jsonl").read_text()
    assert "REJECTED by the static gate" in trace

    calls["n"] = 0
    good = ConverterAgent(
        "textwrap", judge=_pass_judge, merger=lambda p: "merged",
        tester=tester, fixer=good_fixer, workdir=workdir / "good")
    result = good.run()
    assert result.verdict == "pass" and result.test_cycles == 1
    trace = (workdir / "good" / "react_trace.jsonl").read_text()
    assert "LLM code fix accepted" in trace


def test_no_tester_skips_stage(workdir):
    agent = ConverterAgent(
        "textwrap", judge=_pass_judge, merger=lambda p: "merged",
        workdir=workdir)
    result = agent.run()
    assert result.verdict == "pass" and result.test_cycles == 0


def test_max_test_cycles_is_configurable(workdir):
    """testing.max_cycles caps the test/fix loop: with a cap of 1 the
    unfixable tool is removed after a single test round."""
    calls = {"n": 0}

    def tester(adapter_path, proposals):
        calls["n"] += 1
        return {p.tool_name: "RuntimeError: boom" for p in proposals
                if p.tool_name == "textwrap_indent"}

    agent = ConverterAgent(
        "textwrap", judge=_pass_judge, merger=lambda p: "merged",
        tester=tester, workdir=workdir, max_test_cycles=1)
    result = agent.run()
    assert result.verdict == "pass"
    assert "textwrap_indent" not in result.tools
    assert calls["n"] == 1 and result.test_cycles == 1


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


def test_run_convert_unexpected_error_closes_run(ctx, settings, workdir,
                                                 monkeypatch):
    """Regression: a crashing agent must close the run within the reason_code
    vocabulary — previously the close itself raised IntegrityError (arbitrary
    exception names are not valid reason codes) and the run stayed open."""
    class Boom(Exception):
        pass

    def boom(self):
        raise Boom("simulated agent crash")

    monkeypatch.setattr(ConverterAgent, "run", boom)
    rc = run_convert("textwrap", settings, ctx=ctx, workdir=workdir)
    assert rc == 2
    rows = ctx.store.runs.query(package="textwrap", outcome="error")
    assert len(rows) == 1 and rows[0]["reason_code"] is None


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
