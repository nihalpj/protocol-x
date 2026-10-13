#!/usr/bin/env python3
"""
Protocol-X Tool MCP — Package Converter Agent (ReAct loop) · PRD F-029
======================================================================

Fully autonomous: NO human hand-off, NO opt-in escalation. At every
failure point the agent uses the best approach available:

  1. deterministic strategies first (findings-keyed mutations),
  2. then the LLM repair tier automatically (Nemotron Ultra — the
     `repairer` seam; the `proposer` seam covers zero-surface packages).

The ONLY terminal failure is a fail-closed rejection: no merge, exit 2,
and a machine-readable `rejection.json` for the audit ledger (INV-6).
Nothing ever waits on a human.

Pipeline:
  1. introspects the installed package (inspect — never PyPI guesses),
  2. proposes a focused tool surface (<= MAX_TOOLS, E-12); if the
     heuristic surface is empty, the LLM proposer takes over automatically,
  3. codegens the adapter via `templates/adapter.py.jinja`,
  4. renders the agent-facing tool-call format via
     `templates/tool_call_format.jinja` and runs a bounded AUTO-FIX loop
     (deterministic, then LLM repair tier; <= MAX_FIX_CYCLES, AC-F029-06),
  5. passes the AI-judge gate with a bounded repair loop on `blocked`
     verdicts (deterministic, then LLM repair tier; <= MAX_JUDGE_CYCLES,
     E-10), then auto-merges (PRD v1.4).

ReAct plumbing (trace persistence, checkpoints) comes from `agents/react.py`:
every step is a Thought -> Action -> Observation triple, persisted to
`<workdir>/react_trace.jsonl` for the audit log (AC-F029-05); an
interrupted run resumes from the last checkpoint with --resume (E-13).
Checkpoints are dual-written to the `checkpoints` SQL table so a resume
survives a process restart (AC-P7-05), and every run opens/closes a
`conversion_runs` row queryable by outcome and reason_code.

Production entry point: `run_convert()` (wired to the CLI as
`protocol-x-tool-mcp convert <package>`). It injects the real seams —
Nemotron judge (`agents/judge.py`), registry merger, LLM repair/propose
tiers — when a Token Factory key is configured; without one the judge
fails closed (E-20) and every conversion ends in rejection.

Standalone demo usage (stub seams, no server):
    python3 agents/converter_agent.py <package> [options]

    --judge {pass,warn,blocked,repair-demo,repair-llm-demo}
                     demo verdict source (real deployments inject the
                     Nemotron judge; omit to FAIL CLOSED = blocked)
    --repairer {llm-demo}
                     demo LLM repair tier (real deployments inject the
                     Nemotron Ultra repairer; omit = deterministic only)
    --proposer {llm-demo}
                     demo LLM proposal fallback for zero-surface packages
    --demo-fault     break the first tool-call-format render so the
                     auto-fix loop visibly repairs it (cycle 1/3)
    --workdir DIR    artifact + checkpoint directory (default: fresh tmp)
    --resume         resume from the checkpoint in --workdir
    --stream         print ReAct triples live as they happen
"""
from __future__ import annotations

import argparse
import importlib
import inspect
import json
import py_compile
import re
import shutil
import sys
import tempfile
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from functools import partial
from pathlib import Path

from jinja2 import FileSystemLoader, select_autoescape
from jinja2.sandbox import SandboxedEnvironment

try:
    from agents.react import ReActTracer
except ImportError:  # direct script execution: python3 agents/converter_agent.py
    from react import ReActTracer  # type: ignore[no-redef]

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
MAX_FIX_CYCLES = 3      # circuit breaker: format auto-fix loop (AC-F029-06)
MAX_JUDGE_CYCLES = 3    # circuit breaker: blocked-verdict repair loop (E-10)
MAX_TOOLS = 10          # tool-surface proposal cap (E-12)
RUBRIC_VERSION = "inv-rubric v1.2"

# Nebius × NVIDIA Hackathon: all agent inference via Nebius Token Factory (PRD v1.6).
TOKEN_FACTORY_URL = "https://api.tokenfactory.nebius.com/v1"
JUDGE_MODEL = "nvidia/nemotron-3-ultra"   # AI-judge + codegen/repair tier
FAST_MODEL = "nvidia/nemotron-3-nano"     # converter / repair fast loops

_SIMPLE_ANNOTATIONS = {str: "str", int: "int", float: "float", bool: "bool"}
_JSON_TYPES = {"str": "string", "int": "integer", "float": "number", "bool": "boolean"}
_STAGES = ["imported", "proposed", "codegen", "format", "merged"]


def _static_gate(source: str) -> list[str]:
    """Full static gate (py_compile + INV-1 lint + contract) when the server
    tree is importable; bare py_compile fallback for standalone demo runs."""
    gate: Callable[[str], list[str]] | None = None
    try:
        from agents.codegen import static_gate as _imported_gate

        gate = _imported_gate
    except ImportError:
        pass
    if gate is not None:
        return gate(source)
    with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as fh:
        fh.write(source)
        tmp = fh.name
    try:
        py_compile.compile(tmp, doraise=True)
    except py_compile.PyCompileError as exc:
        return [f"py_compile: {exc.msg}"]
    return []


class ConversionRejected(Exception):
    """Terminal, fail-closed outcome: every autonomous approach exhausted.

    This is NOT a human hand-off — the run ends here, nothing merges, and
    `rejection.json` carries the machine-readable reason for the audit
    ledger (INV-6)."""


@dataclass
class Arg:
    name: str
    type: str
    description: str
    default: str | None = None   # Python literal (repr), None = required


@dataclass
class ToolProposal:
    tool_name: str
    target: str            # function attribute on the module
    description: str
    args: list[Arg] = field(default_factory=list)

    @property
    def model_name(self) -> str:
        return "".join(p.title() for p in self.tool_name.split("_")) + "In"


@dataclass
class ConversionResult:
    package: str
    adapter_path: Path
    format_path: Path
    tools: list[str]
    fix_cycles: int
    judge_cycles: int
    verdict: str


class ConverterAgent:
    def __init__(self, package: str, focus: str | None = None,
                 demo_fault: bool = False, judge=None, merger=None,
                 repairer=None, proposer=None,
                 workdir: str | Path | None = None, resume: bool = False,
                 stream: bool = False, store=None, run_id: int | None = None,
                 resume_stage: str | None = None):
        self.package = package
        self.focus = focus
        self.demo_fault = demo_fault
        # Default judge FAILS CLOSED: no verdict, no merge — ever (E-5, INV-6).
        self.judge = judge or (lambda path: "blocked")
        self.merger = merger or (lambda path: f"registered {path.name} (simulated merge)")
        # LLM recovery seams (best approach available — automatic, never opt-in).
        # repairer(findings, proposals, context) -> directive dict | None
        # proposer(module) -> list[ToolProposal]
        self.repairer = repairer
        self.proposer = proposer
        # DB seams (optional): conversion_runs row + durable checkpoints (AC-P7-05).
        self.store = store
        self.run_id = run_id
        self.fix_cycles = 0
        self.judge_cycles = 0
        self.network = False
        self.timeout_s = 30
        self.env = SandboxedEnvironment(
            loader=FileSystemLoader(TEMPLATES_DIR),
            autoescape=select_autoescape(default=False),
        )
        self.workdir = (Path(workdir) if workdir
                        else Path(tempfile.mkdtemp(prefix=f"convert-{package}-")))
        self.workdir.mkdir(parents=True, exist_ok=True)
        self.tracer = ReActTracer(
            trace_file=self.workdir / "react_trace.jsonl",
            checkpoint_file=self.workdir / "checkpoint.json",
            stream=stream,
        )
        self._resume_stage = resume_stage or (self.tracer.completed_stage() if resume else None)

    # ------------------------------------------------------- DB checkpoints
    def _checkpoint(self, stage: str, payload: dict | None = None) -> None:
        """Checkpoint to the workdir file AND the checkpoints table (AC-P7-05)."""
        self.tracer.checkpoint(stage, payload)
        if self.store is not None and self.run_id is not None:
            self.store.checkpoints.save(self.run_id, stage, payload or {})

    # ------------------------------------------------------------ pipeline
    def run(self) -> ConversionResult:
        if self._done("codegen") and self._done("format"):
            proposals = self._load_proposals()
            adapter_path = self.workdir / f"{self._mod_name()}.py"
            format_path = self.workdir / "tool_call_format.txt"
            self.tracer.record(
                f"resume from checkpoint '{self._resume_stage}' (E-13)",
                "skip introspect/propose/codegen/format — artifacts on disk",
                f"{len(proposals)} proposals, 2 artifacts reloaded")
        else:
            mod = self.tracer.act(
                f"'{self.package}' must become sandboxed tools; start from the installed package",
                f"importlib.import_module({self.package!r})",
                lambda: importlib.import_module(self.package))
            self._checkpoint("imported")

            proposals = self.tracer.act(
                "propose a focused tool surface — fixed-signature wrappers only (INV-1)",
                f"inspect members, keep simple signatures, cap {MAX_TOOLS} (E-12)",
                lambda: self._propose(mod))
            if not proposals and self.proposer:
                proposals = self.tracer.act(
                    "heuristic surface empty — best approach available: LLM proposal fallback (automatic)",
                    f"proposer({self.package}) via {JUDGE_MODEL} @ Token Factory",
                    lambda: self.proposer(mod))
            if not proposals:
                self._reject("not-convertible",
                             "no wrappable functions found by heuristic introspection or LLM proposal")
            self._save_proposals(proposals)
            self._checkpoint("proposed")

            adapter_path = self.tracer.act(
                f"codegen adapters/{self._mod_name()}.py from adapter.py.jinja",
                "render(SandboxedEnvironment) + py_compile static gate (E-4)",
                lambda: self._codegen_adapter(proposals))
            self._checkpoint("codegen")

            format_path = self.tracer.act(
                "render the agent-facing tool-call format from tool_call_format.jinja",
                "render + validate; auto-fix findings (<=3 cycles, AC-F029-06)",
                lambda: self._render_format_with_fixes(proposals))
            self._checkpoint("format")

        verdict = self._judge_with_repairs(adapter_path, proposals)
        merge_note = self.tracer.act(
            f"verdict {verdict!r} — auto-merge without human action (v1.4)",
            "registry.register(adapter)",
            lambda: self.merger(adapter_path))
        _ = merge_note
        self._checkpoint("merged")
        return ConversionResult(
            package=self.package,
            adapter_path=adapter_path,
            format_path=format_path,
            tools=[p.tool_name for p in proposals],
            fix_cycles=self.fix_cycles,
            judge_cycles=self.judge_cycles,
            verdict=verdict)

    # ------------------------------------------------------- step: propose
    def _propose(self, mod) -> list[ToolProposal]:
        prefix = self._mod_name()[:12]
        out: list[ToolProposal] = []
        for name, fn in inspect.getmembers(mod, inspect.isfunction):
            if name.startswith("_") or fn.__module__ != mod.__name__:
                continue
            args = self._simple_args(fn)
            if args is None:
                continue
            doc = (inspect.getdoc(fn) or f"{name}() from {self.package}").split("\n")[0]
            out.append(ToolProposal(
                tool_name=f"{prefix}_{name}",
                target=name,
                description=self._sanitize(doc)[:120],
                args=args))
            if len(out) >= MAX_TOOLS:
                break
        return out

    @staticmethod
    def _simple_args(fn) -> list[Arg] | None:
        """Keep only simple typed params; anything exotic makes the function ineligible."""
        args: list[Arg] = []
        for p in list(inspect.signature(fn).parameters.values())[:6]:
            if p.kind in (p.VAR_POSITIONAL, p.VAR_KEYWORD):
                return None                       # skip *args / **kwargs (INV-1 shape)
            ptype = _SIMPLE_ANNOTATIONS.get(p.annotation, "str")
            default = None
            if p.default is not inspect.Parameter.empty:
                if not isinstance(p.default, (str, int, float, bool, type(None))):
                    return None
                default = repr(p.default)          # Python literal for codegen
            args.append(Arg(name=p.name, type=ptype,
                            description=f"{p.name} argument", default=default))
        return args

    @staticmethod
    def _sanitize(text: str) -> str:
        # INV-4 / codegen safety: no quotes or template braces leak into generated code
        return re.sub(r'["{}\\]', "", text)

    # ------------------------------------------------------- step: codegen
    def _codegen_adapter(self, proposals: list[ToolProposal]) -> Path:
        tpl = self.env.get_template("adapter.py.jinja")
        code = tpl.render(
            rubric_version=RUBRIC_VERSION,
            module_name=self._mod_name(),
            import_name=self.package,
            package_version=self._version(),
            functions=proposals,
            timeout_s=self.timeout_s,
            network=self.network,
            agent_name=f"{self.package}-agent",
        )
        path = self.workdir / f"{self._mod_name()}.py"
        path.write_text(code)
        problems = _static_gate(code)  # py_compile + INV-1 lint + contract (E-4)
        if problems:
            self._reject("codegen-static-gate",
                         "generated adapter failed the static gate", problems)
        return path

    # ------------------------------------- step: tool-call format + AUTO-FIX
    def _schemas(self, proposals: list[ToolProposal]) -> list[dict]:
        tools = []
        for p in proposals:
            props = {a.name: {"type": _JSON_TYPES.get(a.type, "string")}
                     for a in p.args}
            tools.append({
                "name": p.tool_name,
                "description": p.description,
                "input_schema": {
                    "type": "object",
                    "properties": props,
                    "required": [a.name for a in p.args if a.default is None],
                    "additionalProperties": False,
                },
            })
        return tools

    def _render_format_with_fixes(self, proposals: list[ToolProposal]) -> Path:
        tpl = self.env.get_template("tool_call_format.jinja")
        schemas = self._schemas(proposals)
        for _ in range(MAX_FIX_CYCLES + 1):
            if self.demo_fault and self.fix_cycles == 0:
                for t in schemas:                      # simulated generation defect
                    t["input_schema"].pop("additionalProperties", None)
            rendered = tpl.render(tools=schemas, workspace_root="./workspace")
            findings = self._validate_format(rendered, schemas)
            if not findings:
                path = self.workdir / "tool_call_format.txt"
                path.write_text(rendered)
                return path
            self.fix_cycles += 1
            if self.fix_cycles > MAX_FIX_CYCLES:
                self._reject("format-unhealable",
                             f"auto-fix exhausted ({MAX_FIX_CYCLES} cycles) — rejected (AC-F029-06)",
                             findings)
            applied = self._apply_format_fixes(findings, schemas)
            if not applied and self.repairer:
                findings_copy: list[str] = list(findings)
                directive = self.tracer.act(
                    "no deterministic format fix — best-approach LLM repair tier (automatic)",
                    f"repairer({', '.join(findings)}) via {JUDGE_MODEL}",
                    partial(self.repairer, findings_copy, list(proposals),
                            {"stage": "format", "cycle": self.fix_cycles}))
                if self._apply_directive(directive, proposals):
                    if not proposals:
                        self._reject("not-convertible",
                                     "LLM repair removed every tool from the surface", findings)
                    schemas = self._schemas(proposals)
                    self._save_proposals(proposals)
                    applied = True
            if not applied:
                self._reject("format-unhealable",
                             f"no fix strategy (deterministic or LLM) for findings: {', '.join(findings)}",
                             findings)
            self.tracer.record(
                f"format validation failed: {', '.join(findings)}",
                f"auto-fix cycle {self.fix_cycles}/{MAX_FIX_CYCLES}: patch schemas, re-render",
                "patched — re-validating", kind="fix")
        raise ConversionRejected("unreachable")

    def _apply_format_fixes(self, findings: list[str], schemas: list[dict]) -> bool:
        """Deterministic fix strategies, keyed by finding. Returns True if any applied."""
        applied = False
        for f in findings:
            if f == "MISSING_ADDITIONAL_PROPERTIES":
                for t in schemas:
                    t["input_schema"]["additionalProperties"] = False
                applied = True
            elif f.startswith("BAD_NAME:"):
                bad = f.split(":", 1)[1]
                for t in schemas:
                    if t["name"] == bad:
                        t["name"] = re.sub(r"[^a-z0-9_]", "_", bad.lower())
                        applied = True
        return applied

    @staticmethod
    def _validate_format(rendered: str, schemas: list[dict]) -> list[str]:
        findings: list[str] = []
        if "<tools>" not in rendered or "</tools>" not in rendered:
            findings.append("MISSING_TOOLS_BLOCK")
        for t in schemas:
            if not re.fullmatch(r"[a-z][a-z0-9_]*", t["name"]):
                findings.append(f"BAD_NAME:{t['name']}")
            if t["input_schema"].get("additionalProperties") is not False:
                findings.append("MISSING_ADDITIONAL_PROPERTIES")
        return sorted(set(findings))

    # --------------------------------------- step: judge + bounded repair
    def _judge_with_repairs(self, adapter_path: Path,
                            proposals: list[ToolProposal]) -> str:
        """UF-02 step 6: on `blocked`, repair autonomously — deterministic
        strategies first, then the LLM repair tier — max MAX_JUDGE_CYCLES,
        then reject (E-10). No human hand-off at any point."""
        history: list[str] = []
        for cycle in range(1, MAX_JUDGE_CYCLES + 1):
            self.judge_cycles = cycle
            payload = self.tracer.act(
                "AI-judge gate — the only merge gate; outage means fail closed (INV-6)",
                f"judge({adapter_path.name}) via {JUDGE_MODEL} @ Token Factory "
                f"against {RUBRIC_VERSION} (cycle {cycle}/{MAX_JUDGE_CYCLES})",
                lambda: self.judge(adapter_path))
            verdict, findings = self._normalize_verdict(payload)
            if verdict != "blocked":
                return verdict
            history.extend(findings or ["blocked (no findings)"])
            repaired = False
            if cycle < MAX_JUDGE_CYCLES:
                repaired = self._repair_from_findings(findings, proposals, adapter_path)
                if not repaired and self.repairer:
                    findings_copy: list[str] = list(findings)
                    directive = self.tracer.act(
                        "deterministic repairs exhausted — best-approach LLM repair tier (automatic)",
                        f"repairer({'; '.join(findings) or 'blocked'}) via {JUDGE_MODEL}",
                        partial(self.repairer, findings_copy, list(proposals),
                                {"stage": "judge", "cycle": cycle}))
                    repaired = self._apply_directive(directive, proposals)
                    if repaired:
                        if not proposals:
                            self._reject("not-convertible",
                                         "LLM repair removed every tool from the surface", history)
                        self._codegen_adapter(proposals)
                        self._render_format_with_fixes(proposals)
                        self._save_proposals(proposals)
            self.tracer.record(
                f"judge blocked the adapter (cycle {cycle}/{MAX_JUDGE_CYCLES})"
                + (f": {'; '.join(findings)}" if findings else ""),
                "repair adapter per findings, re-render, re-submit" if repaired
                else ("repair budget exhausted" if cycle >= MAX_JUDGE_CYCLES
                      else "no repair approach (deterministic or LLM) applies"),
                "patched — re-judging" if repaired
                else ("exhausted" if cycle >= MAX_JUDGE_CYCLES else "unrepairable"),
                kind="fix")
            if not repaired:
                if cycle < MAX_JUDGE_CYCLES:
                    self._reject("judge-blocked-unrepairable",
                                 "judge blocked and no repair approach applies — conversion rejected (E-10)",
                                 history)
                break  # final cycle still blocked after repairs -> exhausted
        self._reject("judge-blocked-exhausted",
                     f"judge still blocking after {MAX_JUDGE_CYCLES} cycles — conversion rejected (E-10)",
                     history)
        raise ConversionRejected("unreachable")

    @staticmethod
    def _normalize_verdict(payload) -> tuple[str, list[str]]:
        if isinstance(payload, dict):
            return payload.get("verdict", "blocked"), list(payload.get("findings", []))
        return str(payload), []

    def _repair_from_findings(self, findings: list[str],
                              proposals: list[ToolProposal],
                              adapter_path: Path) -> bool:
        for f in findings:
            low = f.lower()
            m = re.search(r"drop tool ([a-z0-9_]+)", low)
            if m and any(p.tool_name == m.group(1) for p in proposals):
                proposals[:] = [p for p in proposals if p.tool_name != m.group(1)]
                self._codegen_adapter(proposals)
                self._render_format_with_fixes(proposals)
                self._save_proposals(proposals)
                return True
            if "network" in low and self.network:
                self.network = False
                self._codegen_adapter(proposals)
                return True
            if "timeout" in low or "limit" in low:
                self.timeout_s = min(self.timeout_s, 15)
                self._codegen_adapter(proposals)
                return True
        return False

    def _apply_directive(self, directive, proposals: list[ToolProposal]) -> bool:
        """Apply an LLM-repair directive {drop_tools, network, timeout_s}.
        Returns True if it changed anything."""
        if not directive:
            return False
        changed = False
        drops = set(directive.get("drop_tools", []))
        if drops and any(p.tool_name in drops for p in proposals):
            proposals[:] = [p for p in proposals if p.tool_name not in drops]
            changed = True
        if directive.get("network") is False and self.network:
            self.network = False
            changed = True
        if "timeout_s" in directive:
            try:
                t = int(directive["timeout_s"])
            except (TypeError, ValueError):
                t = 0
            if 0 < t < self.timeout_s:
                self.timeout_s = max(5, t)
                changed = True
        return changed

    # --------------------------------------------------------- rejection
    def _reject(self, code: str, detail: str, findings: list[str] | None = None):
        """Terminal fail-closed outcome (INV-6): record, trace, raise.

        Fully autonomous — the record exists for the audit ledger and for
        operators, but the agent never waits on a human decision."""
        record = {
            "reason_code": code,
            "detail": detail,
            "package": self.package,
            "findings_history": findings or [],
            "fix_cycles": self.fix_cycles,
            "judge_cycles": self.judge_cycles,
            "ts": time.time(),
        }
        (self.workdir / "rejection.json").write_text(json.dumps(record, indent=2))
        self.tracer.record(
            f"autonomous rejection: {code}",
            "write rejection.json — terminal, fail-closed (INV-6); no human hand-off",
            detail)
        raise ConversionRejected(f"{code}: {detail}")

    # ------------------------------------------------------------- helpers
    def _mod_name(self) -> str:
        return re.sub(r"[^a-z0-9]+", "_", self.package.lower()).strip("_")

    def _version(self) -> str:
        try:
            return importlib.import_module(self.package).__version__ or "0"
        except Exception:
            return "0"

    def _done(self, stage: str) -> bool:
        return (self._resume_stage is not None
                and _STAGES.index(self._resume_stage) >= _STAGES.index(stage))

    def _save_proposals(self, proposals: list[ToolProposal]) -> None:
        (self.workdir / "proposals.json").write_text(
            json.dumps([asdict(p) for p in proposals], indent=2))

    def _load_proposals(self) -> list[ToolProposal]:
        raw = json.loads((self.workdir / "proposals.json").read_text())
        return [ToolProposal(tool_name=p["tool_name"], target=p["target"],
                             description=p["description"],
                             args=[Arg(**a) for a in p["args"]]) for p in raw]


# ------------------------------------------------------- production seams
def production_judge(settings, module_name: str):
    """Real Nemotron judge seam. No Token Factory key -> None -> the agent's
    fail-closed default (blocked) applies (E-20 degraded mode, INV-6)."""
    if not settings.agent_features_enabled:
        return None
    from agents.judge import Judge, JudgeUnavailable
    from agents.tokenfactory import TokenFactoryClient

    judge = Judge(
        TokenFactoryClient(settings.tf_api_key, settings.tf_endpoint),
        settings.tf_judge_model,
    )

    def _judge(path: Path):
        try:
            verdict = judge.review(path.read_text(), module_name)
        except JudgeUnavailable as exc:
            # Fail closed: an unreachable judge is a blocked verdict (E-5).
            return {"verdict": "blocked", "findings": [f"judge unavailable: {exc}"]}
        return {
            "verdict": verdict.verdict,
            "findings": [str(f.get("message", f)) for f in verdict.findings],
        }

    return _judge


def production_merger(ctx, module_name: str):
    """Merge a judge-passed adapter into the live registry: copy into
    adapters/, record a version row, refresh, mark tools live (PRD v1.4)."""

    def _merge(path: Path) -> str:
        from core.hashing import module_source_hash

        target = ctx.registry.adapters_dir / f"{module_name}.py"
        if target.exists():
            # E-7: never clobber a manually-edited adapter — land as a draft.
            draft = target.with_suffix(".draft.py")
            shutil.copy2(path, draft)
            ctx.audit.record("merge", "converter",
                             {"module": module_name, "state": "draft", "reason": "E-7"})
            return f"wrote {draft.name} (existing adapter preserved, E-7)"
        shutil.copy2(path, target)
        digest = module_source_hash(ctx.registry.adapters_dir, module_name)
        parent = ctx.store.versions.latest(module_name)
        version_id = ctx.store.versions.add(
            module_name, digest, "generated",
            parent_id=parent["id"] if parent else None)
        ctx.registry.refresh_module(module_name)
        tools = [e.name for e in ctx.registry.catalog.values()
                 if e.adapter_module == module_name]
        for name in tools:
            ctx.registry.set_state(name, "live")
            ctx.store.tools.upsert(name, "live", module_name, "python", version_id)
        ctx.audit.record("merge", "converter", {"module": module_name, "tools": tools})
        return f"auto-merged {module_name}: {len(tools)} tools live"

    return _merge


def llm_repairer(settings):
    """LLM repair tier (fast model): findings -> directive dict, or None.

    The model only steers bounded mutations (drop tools, network off, clamp
    timeout) — it never writes adapter code, so INV-1 cannot be reintroduced."""
    if not settings.agent_features_enabled:
        return None
    from agents.tokenfactory import InferenceError, TokenFactoryClient

    client = TokenFactoryClient(settings.tf_api_key, settings.tf_endpoint)

    def repair(findings, proposals, context):
        prompt = (
            "A generated MCP tool adapter was rejected. Findings:\n"
            + "\n".join(f"- {f}" for f in findings)
            + f"\n\nCurrent tools: {', '.join(p.tool_name for p in proposals)}.\n"
            "Respond with ONLY a JSON object with any of: "
            '{"drop_tools": ["tool_name"], "network": false, "timeout_s": 15}'
        )
        try:
            completion = client.chat(
                settings.tf_fast_model,
                [{"role": "user", "content": prompt}],
            )
            text = completion.content.strip()
            if text.startswith("```"):
                text = text.strip("`").removeprefix("json").strip()
            data = json.loads(text)
        except (InferenceError, json.JSONDecodeError, KeyError, TypeError):
            return None
        if not isinstance(data, dict):
            return None
        directive: dict = {}
        if isinstance(data.get("drop_tools"), list):
            directive["drop_tools"] = [str(t) for t in data["drop_tools"]]
        if data.get("network") is False:
            directive["network"] = False
        if "timeout_s" in data:
            directive["timeout_s"] = data["timeout_s"]
        return directive or None

    return repair


def llm_proposer(settings):
    """LLM proposal fallback (fast model) for zero-surface packages.

    The model picks from functions introspected on the installed package —
    it can only select and describe, never invent callables (INV-1)."""
    if not settings.agent_features_enabled:
        return None
    from agents.tokenfactory import InferenceError, TokenFactoryClient

    client = TokenFactoryClient(settings.tf_api_key, settings.tf_endpoint)

    def propose(mod) -> list[ToolProposal]:
        candidates = [
            name for name, fn in inspect.getmembers(mod, inspect.isfunction)
            if not name.startswith("_") and fn.__module__ == mod.__name__
        ]
        if not candidates:
            return []
        prompt = (
            f"Package '{mod.__name__}' exposes: {', '.join(candidates[:50])}.\n"
            f"Pick up to {MAX_TOOLS} functions worth exposing as sandboxed MCP tools. "
            "Respond with ONLY a JSON array: "
            '[{"target": "<function>", "description": "<one line, no quotes>"}]'
        )
        try:
            completion = client.chat(
                settings.tf_fast_model, [{"role": "user", "content": prompt}])
            text = completion.content.strip()
            if text.startswith("```"):
                text = text.strip("`").removeprefix("json").strip()
            data = json.loads(text)
        except (InferenceError, json.JSONDecodeError, TypeError):
            return []
        if not isinstance(data, list):
            return []
        prefix = re.sub(r"[^a-z0-9]+", "_", mod.__name__.lower()).strip("_")[:12]
        out: list[ToolProposal] = []
        for item in data[:MAX_TOOLS]:
            if not isinstance(item, dict) or item.get("target") not in candidates:
                continue
            fn = getattr(mod, item["target"])
            args = ConverterAgent._simple_args(fn)
            if args is None:
                args = []  # simplified wrapper: fixed signature, no exotic params
            doc = ConverterAgent._sanitize(str(item.get("description") or item["target"]))
            out.append(ToolProposal(
                tool_name=f"{prefix}_{item['target']}",
                target=item["target"],
                description=doc[:120],
                args=args))
        return out

    return propose


# --------------------------------------------------------------------- CLI
def _demo_judge(mode: str):
    """Demo verdict sources; real deployments inject the Nemotron judge (P6)."""
    if mode == "pass":
        return lambda path: "pass"
    if mode == "warn":
        return lambda path: {"verdict": "pass-with-warnings",
                             "findings": ["network=False declared — good; keep limits documented"]}
    if mode == "blocked":
        return lambda path: {"verdict": "blocked",
                             "findings": ["INV-1 violation: eval() on line 12"]}
    if mode == "repair-demo":
        state = {"calls": 0}
        def judge(path):
            state["calls"] += 1
            if state["calls"] == 1:
                return {"verdict": "blocked",
                        "findings": ["free-form parameter risk — drop tool textwrap_indent"]}
            return "pass"
        return judge
    if mode == "repair-llm-demo":
        # blocked with a finding no deterministic strategy matches → the LLM
        # repair tier must handle it; passes once the surface has been reduced
        state = {"calls": 0}
        def judge(path):
            state["calls"] += 1
            if state["calls"] == 1:
                return {"verdict": "blocked",
                        "findings": ["INV-1 violation: eval()-shaped wrapper on line 12 — refactor advised"]}
            return "pass"
        return judge
    raise ValueError(mode)


def _demo_repairer(mode: str):
    """Demo LLM repair tier; real deployments inject the Nemotron Ultra repairer."""
    if mode == "llm-demo":
        def repair(findings, proposals, context):
            if not proposals:
                return None
            # simulated Nemotron Ultra: reduce the risk surface per findings —
            # drop the highest-risk (last-proposed) tool and tighten the budget
            return {"drop_tools": [proposals[-1].tool_name],
                    "timeout_s": 15,
                    "note": f"llm-demo repair for {context.get('stage')}: dropped "
                            f"{proposals[-1].tool_name}, clamped timeout"}
        return repair
    raise ValueError(mode)


def _demo_proposer(mode: str):
    """Demo LLM proposal fallback: wraps functions the heuristic rejected —
    keeps the leading simple params, drops *args/**kwargs (best approach)."""
    if mode == "llm-demo":
        def propose(mod) -> list[ToolProposal]:
            prefix = re.sub(r"[^a-z0-9]+", "_", mod.__name__.lower()).strip("_")[:12]
            out: list[ToolProposal] = []
            for name, fn in inspect.getmembers(mod, inspect.isfunction):
                if name.startswith("_") or fn.__module__ != mod.__name__:
                    continue
                leading = []
                for p in inspect.signature(fn).parameters.values():
                    if p.kind in (p.VAR_POSITIONAL, p.VAR_KEYWORD):
                        break                          # simplified wrapper drops the rest
                    leading.append(p)
                args: list[Arg] = []
                ok = bool(leading)
                for p in leading[:4]:
                    ptype = _SIMPLE_ANNOTATIONS.get(p.annotation, "str")
                    default = None
                    if p.default is not inspect.Parameter.empty:
                        if not isinstance(p.default, (str, int, float, bool, type(None))):
                            ok = False
                            break
                        default = repr(p.default)
                    args.append(Arg(name=p.name, type=ptype,
                                    description=f"{p.name} argument", default=default))
                if not ok:
                    continue
                doc = (inspect.getdoc(fn) or f"{name}() from {mod.__name__}").split("\n")[0]
                out.append(ToolProposal(
                    tool_name=f"{prefix}_{name}",
                    target=name,
                    description=re.sub(r'["{}\\]', "", doc)[:120],
                    args=args))
                if len(out) >= MAX_TOOLS:
                    break
            return out
        return propose
    raise ValueError(mode)


def main() -> int:
    ap = argparse.ArgumentParser(description="ReAct package → MCP tools converter (fully autonomous)")
    ap.add_argument("package")
    ap.add_argument("--focus", default=None)
    ap.add_argument("--judge",
                    choices=["pass", "warn", "blocked", "repair-demo", "repair-llm-demo"],
                    default=None,
                    help="demo verdict source; omit = fail-closed (blocked)")
    ap.add_argument("--repairer", choices=["llm-demo"], default=None,
                    help="demo LLM repair tier; omit = deterministic strategies only")
    ap.add_argument("--proposer", choices=["llm-demo"], default=None,
                    help="demo LLM proposal fallback for zero-surface packages")
    ap.add_argument("--demo-fault", action="store_true")
    ap.add_argument("--workdir", default=None)
    ap.add_argument("--resume", action="store_true")
    ap.add_argument("--stream", action="store_true")
    args = ap.parse_args()

    agent = ConverterAgent(
        args.package, focus=args.focus, demo_fault=args.demo_fault,
        judge=_demo_judge(args.judge) if args.judge else None,
        merger=lambda path: f"auto-merged {path.name} → tools live (simulated)",
        repairer=_demo_repairer(args.repairer) if args.repairer else None,
        proposer=_demo_proposer(args.proposer) if args.proposer else None,
        workdir=args.workdir, resume=args.resume, stream=args.stream)
    result = None
    try:
        result = agent.run()
    except ConversionRejected as exc:
        print(f"\nREJECTED: {exc}")
        print(f"rejection record: {agent.workdir / 'rejection.json'}")
    except Exception as exc:                # unexpected — trace still emitted
        print(f"\nERROR: {type(exc).__name__}: {exc}")

    if not args.stream:
        agent.tracer.print_all()
    if result:
        print(f"\nverdict={result.verdict} fix_cycles={result.fix_cycles} "
              f"judge_cycles={result.judge_cycles}")
        print(f"tools: {', '.join(result.tools)}")
        print(f"artifacts: {result.adapter_path} · {result.format_path}")
        print(f"trace: {agent.workdir / 'react_trace.jsonl'}")
        return 0
    print(f"\nno merge — terminal autonomous outcome "
          f"(workdir kept for inspection/resume: {agent.workdir})")
    return 2


if __name__ == "__main__":
    sys.exit(main())
