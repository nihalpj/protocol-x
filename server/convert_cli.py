"""CLI composition root for the converter (F-029): `protocol-x-tool-mcp convert`.

Lives in server/ because it composes AppContext + the agent layer
(import-linter layering: server -> agents -> core -> adapters). The ReAct
machinery itself is agents/converter_agent.py.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable
from pathlib import Path

from agents.converter_agent import (
    ConversionRejected,
    ConverterAgent,
    llm_fixer,
    llm_proposer,
    llm_repairer,
    llm_tester,
    production_judge,
    production_merger,
)


def run_convert(package: str, settings, *, focus: str | None = None,
                resume: bool = False, stream: bool = False,
                workdir: str | Path | None = None, ctx=None,
                on_run_opened: Callable[[int], None] | None = None) -> int:
    """Production entry point (CLI `convert`): DB-tracked, registry-merging run.

    Returns 0 on merge, 2 on rejection/error. Degraded mode (no Token
    Factory key) is announced explicitly and fails closed (E-20).
    `on_run_opened` (if given) is invoked with the run_id as soon as the
    conversion_runs row is known — used by the MCP operator tools to hand
    the id back to the caller while the run continues in the background."""
    if ctx is None:
        from server.app import AppContext

        ctx = AppContext(settings)
    store = ctx.store
    mod_name = re.sub(r"[^a-z0-9]+", "_", package.lower()).strip("_")

    run_id: int | None = None
    resume_stage: str | None = None
    if resume:
        row = store.runs.latest_open(package)
        if row:
            run_id = int(row["id"])
            workdir = workdir or row["workdir"]
            ckpt = store.checkpoints.load(run_id)
            resume_stage = ckpt["stage"] if ckpt else None
            print(f"resuming run {run_id} from checkpoint "
                  f"'{resume_stage}' (workdir {workdir})")
        else:
            print(f"no open conversion run for '{package}' — starting fresh")
            resume = False
    if run_id is None:
        if workdir is None:
            workdir = settings.data_dir / "conversions" / f"{mod_name}-{int(time.time())}"
        workdir = Path(workdir)
        workdir.mkdir(parents=True, exist_ok=True)
        run_id = store.runs.open(
            package, str(workdir), str(workdir / "react_trace.jsonl"))
    if on_run_opened is not None:
        on_run_opened(run_id)

    if not settings.agent_features_enabled:
        print("degraded mode (E-20): PROTOX_TF_API_KEY not set — the AI-judge "
              "fails closed, so this conversion will be rejected after its "
              "bounded repair cycles")

    agent = ConverterAgent(
        package, focus=focus,
        judge=production_judge(settings, mod_name),
        merger=production_merger(ctx, mod_name),
        repairer=llm_repairer(settings),
        proposer=llm_proposer(settings),
        tester=llm_tester(settings) if settings.testing_enabled else None,
        fixer=llm_fixer(settings),
        workdir=workdir, resume=resume, stream=stream,
        store=store, run_id=run_id, resume_stage=resume_stage,
        max_test_cycles=settings.testing_max_cycles)
    try:
        result = agent.run()
    except ConversionRejected as exc:
        code = str(exc).split(":", 1)[0]
        store.runs.close(run_id, "rejected", reason_code=code,
                         fix_cycles=agent.fix_cycles, judge_cycles=agent.judge_cycles,
                         test_cycles=agent.test_cycles)
        print(f"\nREJECTED: {exc}")
        print(f"rejection record: {agent.workdir / 'rejection.json'} (run {run_id})")
        return 2
    except Exception as exc:
        # reason_code vocabulary is rejection-only (m003); unexpected errors
        # close with NULL — the exception detail lives in the workdir trace.
        store.runs.close(run_id, "error",
                         fix_cycles=agent.fix_cycles, judge_cycles=agent.judge_cycles,
                         test_cycles=agent.test_cycles)
        print(f"\nERROR: {type(exc).__name__}: {exc}")
        print(f"workdir kept for inspection/resume: {agent.workdir} (run {run_id})")
        return 2

    store.runs.close(run_id, "merged",
                     fix_cycles=result.fix_cycles, judge_cycles=result.judge_cycles,
                     test_cycles=result.test_cycles)
    if not stream:
        agent.tracer.print_all()
    print(f"\nverdict={result.verdict} fix_cycles={result.fix_cycles} "
          f"judge_cycles={result.judge_cycles} test_cycles={result.test_cycles}")
    print(f"tools: {', '.join(result.tools)}")
    print(f"trace: {agent.workdir / 'react_trace.jsonl'} (run {run_id})")
    return 0
