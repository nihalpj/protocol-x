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

ReAct plumbing (trace persistence, checkpoints) comes from `react.py`:
every step is a Thought -> Action -> Observation triple, persisted to
`<workdir>/react_trace.jsonl` for the audit log (AC-F029-05); an
interrupted run resumes from the last checkpoint with --resume (E-13).

Usage:
    python3 converter_agent.py <package> [options]

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
from dataclasses import asdict, dataclass, field
from pathlib import Path

from jinja2 import FileSystemLoader, select_autoescape
from jinja2.sandbox import SandboxedEnvironment

from react import ReActTracer

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
                 stream: bool = False):
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
        self._resume_stage = self.tracer.completed_stage() if resume else None

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
                f"'{self.package}' must become sandboxed tools; start from the installed package, never PyPI guesses",
                f"importlib.import_module({self.package!r})",
                lambda: importlib.import_module(self.package))
            self.tracer.checkpoint("imported")

            proposals = self.tracer.act(
                "propose a focused tool surface — fixed-signature wrappers only (INV-1)",
                f"inspect members, keep simple signatures, cap {MAX_TOOLS} (E-12)",
                lambda: self._propose(mod))
            if not proposals and self.proposer:
                proposals = self.tracer.act(
                    "heuristic surface empty — use the best approach available: LLM proposal fallback (automatic)",
                    f"proposer({self.package}) via {JUDGE_MODEL} @ Token Factory",
                    lambda: self.proposer(mod))
            if not proposals:
                self._reject("not-convertible",
                             "no wrappable functions found by heuristic introspection or LLM proposal")
            self._save_proposals(proposals)
            self.tracer.checkpoint("proposed")

            adapter_path = self.tracer.act(
                f"codegen adapters/{self._mod_name()}.py from adapter.py.jinja",
                "render(SandboxedEnvironment) + py_compile static gate (E-4)",
                lambda: self._codegen_adapter(proposals))
            self.tracer.checkpoint("codegen")

            format_path = self.tracer.act(
                "render the agent-facing tool-call format from tool_call_format.jinja",
                "render + validate; auto-fix findings (<=3 cycles, AC-F029-06)",
                lambda: self._render_format_with_fixes(proposals))
            self.tracer.checkpoint("format")

        verdict = self._judge_with_repairs(adapter_path, proposals)
        merge_note = self.tracer.act(
            f"verdict {verdict!r} — auto-merge without human action (v1.4)",
            "registry.register(adapter)",
            lambda: self.merger(adapter_path))
        _ = merge_note
        self.tracer.checkpoint("merged")
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
            package_version=self._version(),
            functions=proposals,
            timeout_s=self.timeout_s,
            network=self.network,
            agent_name=f"{self.package}-agent",
        )
        path = self.workdir / f"{self._mod_name()}.py"
        path.write_text(code)
        py_compile.compile(str(path), doraise=True)  # static gate (E-4)
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
                             f"auto-fix exhausted ({MAX_FIX_CYCLES} cycles) — conversion rejected (AC-F029-06)",
                             findings)
            applied = self._apply_format_fixes(findings, schemas)
            if not applied and self.repairer:
                directive = self.tracer.act(
                    "no deterministic format fix — best-approach LLM repair tier (automatic)",
                    f"repairer({', '.join(findings)}) via {JUDGE_MODEL}",
                    lambda: self.repairer(findings, list(proposals),
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
                f"auto-fix cycle {self.fix_cycles}/{MAX_FIX_CYCLES}: patch schemas, re-render via tool_call_format.jinja",
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
                f"judge({adapter_path.name}) via {JUDGE_MODEL} @ Token Factory against {RUBRIC_VERSION} (cycle {cycle}/{MAX_JUDGE_CYCLES})",
                lambda: self.judge(adapter_path))
            verdict, findings = self._normalize_verdict(payload)
            if verdict != "blocked":
                return verdict
            history.extend(findings or ["blocked (no findings)"])
            repaired = False
            if cycle < MAX_JUDGE_CYCLES:
                repaired = self._repair_from_findings(findings, proposals, adapter_path)
                if not repaired and self.repairer:
                    directive = self.tracer.act(
                        "deterministic repairs exhausted — best-approach LLM repair tier (automatic)",
                        f"repairer({'; '.join(findings) or 'blocked'}) via {JUDGE_MODEL}",
                        lambda: self.repairer(findings, list(proposals),
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
                else "no repair approach (deterministic or LLM) applies",
                "patched — re-judging" if repaired else "unrepairable",
                kind="fix")
            if not repaired:
                self._reject("judge-blocked-unrepairable",
                             "judge blocked and no repair approach applies — conversion rejected (E-10)",
                             history)
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
        """Apply an LLM-repair directive {drop_tools, network, timeout_s}. Returns True if it changed anything."""
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
