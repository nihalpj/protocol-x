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
  1. introspects the installed package (inspect — never PyPI guesses):
     top-level members, re-exports, and a bounded submodule scan for
     pure-namespace packages,
  2. proposes a focused tool surface (<= MAX_TOOLS) — the package's
     TOP-50 MOST-USED functions: the LLM proposer picks them when a Token
     Factory key is configured; otherwise a usage-ranked heuristic
     (__all__ public API first, then documented, non-deprecated),
  3. codegens the adapter via `templates/adapter.py.jinja`,
  4. renders the agent-facing tool-call format via
     `templates/tool_call_format.jinja` and runs a bounded AUTO-FIX loop
     (deterministic, then LLM repair tier; <= MAX_FIX_CYCLES, AC-F029-06),
  5. TESTS the adapter: the test model (settings.tf_test_model) generates
     sample arguments per tool and every handler is executed; failures go
     through a bounded fix loop (deterministic mutations, LLM directive,
     LLM code patch re-gated by the static gate; <= max_test_cycles, config
     testing.max_cycles, default MAX_TEST_CYCLES) —
     tools still failing are marked failed and REMOVED from the surface,
  6. passes the AI-judge gate (one structured-output request for the whole
     package) with a bounded repair loop on `blocked` verdicts
     (<= MAX_JUDGE_CYCLES, E-10), then auto-merges (PRD v1.4).

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
    --tester {llm-demo}
                     demo adapter tester: sample-arg execution of every tool;
                     omit = skip the test stage
    --demo-fault     break the first tool-call-format render so the
                     auto-fix loop visibly repairs it (cycle 1/3)
    --workdir DIR    artifact + checkpoint directory (default: fresh tmp)
    --resume         resume from the checkpoint in --workdir
    --stream         print ReAct triples live as they happen
"""
from __future__ import annotations

import argparse
import importlib
import importlib.metadata
import importlib.util
import inspect
import itertools
import json
import pkgutil
import py_compile
import re
import shutil
import sys
import tempfile
import time
from collections.abc import Callable, Iterator
from dataclasses import asdict, dataclass, field
from functools import partial
from pathlib import Path

from jinja2 import FileSystemLoader, select_autoescape
from jinja2.sandbox import SandboxedEnvironment

try:
    from agents.react import ReActTracer
except ImportError:  # direct script execution: python3 agents/converter_agent.py
    from react import ReActTracer  # type: ignore[no-redef]

try:
    # Honest annotation/default projection shared with the builder (F-019),
    # plus docstring mining for detailed tool/parameter descriptions (F-040).
    from agents.codegen import (
        ARG_DESC_MAX,
        DESC_MAX,
        literal_default,
        parse_docstring,
        py_schema_type,
        sanitize_description,
    )
    # Code-shaped param names the INV-1 lint blocks (expr, code, ...); shared
    # so the proposal surface is screened with the same vocabulary the gate
    # enforces — single source of truth, no drift.
    from agents.lint import _CODE_PARAM_NAMES
except ImportError:  # standalone demo runs without the server tree
    _CODE_PARAM_NAMES = {"code", "expression", "expr", "query_string",
                         "script", "program", "source_code"}


    def py_schema_type(annotation) -> str | None:
        if annotation is inspect.Parameter.empty:
            return "str"
        return _SIMPLE_ANNOTATIONS.get(annotation)

    def literal_default(value) -> str | None:
        if isinstance(value, (str, int, float, bool, type(None))):
            return repr(value)
        return None

    DESC_MAX = 500
    ARG_DESC_MAX = 200

    def sanitize_description(text: str) -> str:
        return re.sub(r"\s+", " ", re.sub(r'["{}\\]', "", text)).strip()

    def parse_docstring(doc: str) -> tuple[str, dict[str, str]]:
        return (doc.split("\n")[0] if doc else ""), {}

from core.logging import fmt_obj, get_logger

log = get_logger("converter")


def resolve_import_name(package: str) -> str:
    """Importable top-level module for a PyPI project name.

    Project names are not import names (pyjwt -> jwt, pyyaml -> yaml,
    python-dotenv -> dotenv) and hyphens are never importable, so resolve
    through the installed distribution's top_level.txt, falling back to the
    normalized name."""
    norm = re.sub(r"[^a-z0-9]+", "_", package.lower()).strip("_")
    for cand in (package, norm):
        if not cand.isidentifier():
            continue
        try:
            if importlib.util.find_spec(cand) is not None:
                return cand
        except (ImportError, ValueError):
            continue
    try:
        top = importlib.metadata.distribution(package).read_text("top_level.txt")
    except Exception:
        top = None
    names = [n.strip() for n in (top or "").splitlines() if n.strip()]
    if not names:
        # No top_level.txt (e.g. protobuf -> google.protobuf): derive the
        # import roots from the distribution's recorded file list.
        try:
            files = importlib.metadata.files(package) or []
        except Exception:
            files = []
        names = sorted({f.parts[0] for f in files
                        if f.parts and f.parts[0].isidentifier()})
    if norm in names:
        return norm
    for cand in names:  # skip private/extension modules like pyyaml's _yaml
        if cand.startswith("_") or not cand.isidentifier():
            continue
        try:
            if importlib.util.find_spec(cand) is None:
                continue
        except (ImportError, ValueError):
            continue
        if norm != cand:
            # Namespace roots hide the real package one level down
            # (protobuf -> google.protobuf).
            try:
                if importlib.util.find_spec(f"{cand}.{norm}") is not None:
                    return f"{cand}.{norm}"
            except (ImportError, ValueError):
                pass
        return cand
    return names[0] if names else norm

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
MAX_FIX_CYCLES = 3      # circuit breaker: format auto-fix loop (AC-F029-06)
MAX_JUDGE_CYCLES = 2    # circuit breaker: blocked-verdict repair loop (E-10)
MAX_TEST_CYCLES = 3     # circuit breaker: adapter test + fix loop
MAX_TOOLS = 50          # tool-surface proposal cap (top-50 most-used functions)
MAX_SUBMODULES = 20     # submodule scan cap for pure-namespace packages
MAX_CANDIDATES = 200    # introspection cap before usage ranking
RUBRIC_VERSION = "inv-rubric v1.2"

# Nebius × NVIDIA Hackathon: all agent inference via Nebius Token Factory (PRD v1.6).
TOKEN_FACTORY_URL = "https://api.tokenfactory.nebius.com/v1"
JUDGE_MODEL = "nvidia/Nemotron-3-Ultra-550b-a55b"   # AI-judge + codegen/repair tier
FAST_MODEL = "nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B"  # converter / repair fast loops

_SIMPLE_ANNOTATIONS = {str: "str", int: "int", float: "float", bool: "bool"}
_STAGES = ["imported", "proposed", "codegen", "format", "tested", "merged"]


def _json_prop(py_type: str) -> dict:
    """JSON-schema fragment for a projected Python type (INV-3: honest types).

    'X | None' becomes a nullable schema; 'Any'/unknown leaves the schema
    unconstrained rather than claiming a false scalar type."""
    nullable = py_type.endswith(" | None")
    base = py_type[: -len(" | None")] if nullable else py_type
    jtype = {"str": "string", "int": "integer", "float": "number",
             "bool": "boolean", "dict": "object", "list": "array",
             "bytes": "string"}.get(base)
    if jtype is None:
        return {}                        # Any: unconstrained, not "string"
    return {"type": [jtype, "null"]} if nullable else {"type": jtype}


def _iter_targets(mod, max_submodules: int = MAX_SUBMODULES) -> Iterator:
    """Yield (target, callable) pairs reachable as attributes of the package.

    Top-level members come first — including functions the package re-exports
    from its own submodules (pandas/idna/click-style __init__ re-exports) and
    C builtins. When the top level exposes nothing (pure-namespace packages
    such as `packaging`), immediate submodules are imported (bounded by
    max_submodules) and their own functions are yielded with dotted targets
    ('version.parse') so generated adapters can call them via the package.
    """
    top = [
        (name, member)
        for name, member in inspect.getmembers(mod)
        if not name.startswith("_")
        and (inspect.isfunction(member) or inspect.isbuiltin(member))
    ]
    if top:
        yield from top
        return
    path = getattr(mod, "__path__", None)
    if path is None:
        return
    scanned = 0
    for info in pkgutil.iter_modules(path):
        if scanned >= max_submodules:
            break
        if info.name.startswith("_"):
            continue
        scanned += 1
        try:
            sub = importlib.import_module(f"{mod.__name__}.{info.name}")
        except Exception:
            continue                       # unimportable submodule: skip it
        for name, member in inspect.getmembers(sub, inspect.isfunction):
            if name.startswith("_") or member.__module__ != sub.__name__:
                continue
            yield f"{info.name}.{name}", member


def _resolve(mod, target: str):
    """Resolve a (possibly dotted) target against the package."""
    obj = mod
    for part in target.split("."):
        obj = getattr(obj, part)
    return obj


def _usage_score(mod, target: str, fn) -> tuple:
    """Rank a candidate by likely real-world usage (higher sorts first).

    The package's __all__ (its declared public API) is the strongest signal,
    then having documentation, then not being deprecated. Ties keep the
    alphabetical order inspect.getmembers returns (stable sort)."""
    base = target.rsplit(".", 1)[-1]
    public = base in (getattr(mod, "__all__", None) or ())
    doc = inspect.getdoc(fn)
    deprecated = bool(doc) and "deprecat" in doc.lower()
    return (public, bool(doc), not deprecated)


def _ranked_targets(mod, cap: int = MAX_CANDIDATES) -> list:
    """Introspected (target, callable) pairs, usage-ranked — the 'top uses of
    the package' ordering the tool surface is selected from."""
    candidates = list(itertools.islice(_iter_targets(mod), cap))
    return sorted(candidates,
                  key=lambda t: _usage_score(mod, t[0], t[1]), reverse=True)


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
    positional_only: bool = False


@dataclass
class ToolProposal:
    tool_name: str
    target: str            # function attribute on the module (may be dotted)
    description: str
    args: list[Arg] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)  # params left to package defaults

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
    test_cycles: int = 0


class ConverterAgent:
    def __init__(self, package: str, focus: str | None = None,
                 demo_fault: bool = False, judge=None, merger=None,
                 repairer=None, proposer=None, tester=None, fixer=None,
                 workdir: str | Path | None = None, resume: bool = False,
                 stream: bool = False, store=None, run_id: int | None = None,
                 resume_stage: str | None = None,
                 max_test_cycles: int = MAX_TEST_CYCLES):
        self.package = package
        self.focus = focus
        self.demo_fault = demo_fault
        # Default judge FAILS CLOSED: no verdict, no merge — ever (E-5, INV-6).
        self.judge = judge or (lambda path: "blocked")
        self.merger = merger or (lambda path: f"registered {path.name} (simulated merge)")
        # LLM recovery seams (best approach available — automatic, never opt-in).
        # repairer(findings, proposals, context) -> directive dict | None
        # proposer(module) -> list[ToolProposal]
        # tester(adapter_path, proposals) -> {tool_name: error} (failures only)
        # fixer(adapter_source, failures) -> patched source | None (re-gated on accept)
        self.repairer = repairer
        self.proposer = proposer
        self.tester = tester
        self.fixer = fixer
        self.max_test_cycles = max_test_cycles
        # DB seams (optional): conversion_runs row + durable checkpoints (AC-P7-05).
        self.store = store
        self.run_id = run_id
        self.fix_cycles = 0
        self.judge_cycles = 0
        self.test_cycles = 0
        self._resolved_import: str | None = None
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
        log.info("conversion started: package=%s workdir=%s", self.package, self.workdir)
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
                f"importlib.import_module({self._import_name()!r})",
                lambda: importlib.import_module(self._import_name()))
            self._checkpoint("imported")

            proposals: list[ToolProposal] = []
            if self.proposer:
                proposals = self.tracer.act(
                    "select the package's top-50 most-used functions for the tool surface",
                    f"proposer({self.package}) via {JUDGE_MODEL} @ Token Factory",
                    lambda: self.proposer(mod))
            if not proposals:
                proposals = self.tracer.act(
                    "propose a focused tool surface — fixed-signature wrappers only (INV-1)",
                    f"usage-ranked heuristic (public API first), honest types, "
                    f"cap {MAX_TOOLS} (E-12)",
                    lambda: self._propose(mod))
            if not proposals:
                self._reject("not-convertible",
                             "no wrappable functions found by LLM proposal or "
                             "usage-ranked introspection")
            log.info("converter: %d tool proposals for %s", len(proposals), self.package)
            log.debug("proposals: %s", fmt_obj([p.tool_name for p in proposals]))
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

        if self.tester and not self._done("tested"):
            self._test_with_fixes(adapter_path, proposals)
            self._checkpoint("tested")

        verdict = self._judge_with_repairs(adapter_path, proposals)
        log.info("converter: final verdict for %s: %s", self.package, verdict)
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
            verdict=verdict,
            test_cycles=self.test_cycles)

    # ------------------------------------------------------- step: propose
    def _propose(self, mod) -> list[ToolProposal]:
        """Usage-ranked heuristic surface: the top MAX_TOOLS most-used
        functions of the package (public API first), projected to fixed
        signatures."""
        prefix = self._mod_name()[:12]
        out: list[ToolProposal] = []
        for target, fn in _ranked_targets(mod):
            projected = self._simple_args(fn)
            if projected is None:
                continue
            args, skipped = projected
            summary, _param_docs = parse_docstring(inspect.getdoc(fn) or "")
            description = (
                sanitize_description(summary)[:DESC_MAX]
                or f"{target}() from {self.package}"
            )
            out.append(ToolProposal(
                tool_name=f"{prefix}_{target.replace('.', '_')}",
                target=target,
                description=description,
                args=args,
                skipped=skipped))
            if len(out) >= MAX_TOOLS:
                break
        return out

    @staticmethod
    def _simple_args(fn) -> tuple[list[Arg], list[str]] | None:
        """Fixed-signature projection of one callable (INV-1, INV-3).

        Params that cannot be typed honestly (rich object annotations) or
        carry non-literal defaults are *skipped* — the package's own defaults
        apply — instead of condemning the whole function; the schema type of
        an unannotated param is inferred from its literal default (a bool
        default maps to bool, never "str"). Params named like free-form
        code/query strings (`expr`, `code`, `query_string`, … — the lint's
        `_CODE_PARAM_NAMES`) are screened out for INV-1: skipped when
        defaulted, function-condemning when required. Returns None only when
        a required param cannot be typed honestly or is code-shaped, or when
        skipping an earlier param would shift positional-only args out of
        order.
        """
        try:
            params = list(inspect.signature(fn).parameters.values())
        except (ValueError, TypeError):
            return None
        _summary, param_docs = parse_docstring(inspect.getdoc(fn) or "")
        args: list[Arg] = []
        skipped: list[str] = []
        for p in params:
            if p.kind in (p.VAR_POSITIONAL, p.VAR_KEYWORD):
                skipped.append(p.name)            # no pass-through (INV-1 shape)
                continue
            if len(args) >= 6:
                skipped.append(p.name)
                continue
            type_name = py_schema_type(p.annotation)
            default: str | None = None
            has_default = p.default is not inspect.Parameter.empty
            if p.name.lower() in _CODE_PARAM_NAMES:
                # INV-1: free-form code/query strings can never become tool
                # args (pandas.eval-style). A defaulted one is skipped like
                # any other unusable param; a required one condemns the
                # function — better than proposing it and letting the codegen
                # gate drop the whole tool.
                if has_default:
                    skipped.append(p.name)
                    continue
                return None
            if has_default:
                default = literal_default(p.default)
            if type_name is None or (has_default and default is None):
                if type_name is None and not has_default:
                    return None                   # required param, no honest type
                skipped.append(p.name)
                continue
            if (type_name == "str" and p.annotation is inspect.Parameter.empty
                    and p.default is not None):
                # infer from the literal default instead of guessing str (INV-3)
                type_name = _SIMPLE_ANNOTATIONS.get(type(p.default), type_name)
            if default == "None":
                type_name = f"{type_name} | None"
            if p.kind is p.POSITIONAL_ONLY and skipped:
                return None                       # positional order would break
            arg_doc = sanitize_description(
                param_docs.get(p.name) or f"{p.name} argument"
            )[:ARG_DESC_MAX]
            args.append(Arg(name=p.name, type=type_name,
                            description=arg_doc, default=default,
                            positional_only=p.kind is p.POSITIONAL_ONLY))
        return args, skipped

    @staticmethod
    def _sanitize(text: str) -> str:
        # INV-4 / codegen safety: no quotes or template braces leak into generated code
        return re.sub(r'["{}\\]', "", text)

    # ------------------------------------------------------- step: codegen
    def _codegen_adapter(self, proposals: list[ToolProposal]) -> Path:
        path = self.workdir / f"{self._mod_name()}.py"
        problems: list[str] = []
        for _ in range(MAX_FIX_CYCLES + 1):
            tpl = self.env.get_template("adapter.py.jinja")
            code = tpl.render(
                rubric_version=RUBRIC_VERSION,
                module_name=self._mod_name(),
                import_name=self._import_name(),
                package_version=self._version(),
                functions=proposals,
                submodule_imports=sorted(
                    {p.target.split(".", 1)[0] for p in proposals if "." in p.target}),
                timeout_s=self.timeout_s,
                network=self.network,
                agent_name=f"{self.package}-agent",
            )
            problems = _static_gate(code)  # py_compile + INV-1 lint + contract (E-4)
            if not problems:
                path.write_text(code)
                return path
            offenders = self._gate_offenders(code, problems, proposals)
            if not offenders or not proposals:
                break                        # not attributable to tools: reject
            self.tracer.record(
                f"codegen static gate: {'; '.join(problems)}",
                f"drop offending tools {sorted(offenders)}, regenerate "
                f"(bounded surgical fix, <= {MAX_FIX_CYCLES})",
                "regenerating without offenders", kind="fix")
            proposals[:] = [p for p in proposals if p.tool_name not in offenders]
            if not proposals:
                self._reject("not-convertible",
                             "static gate dropped every proposed tool", problems)
        self._reject("codegen-static-gate",
                     "generated adapter failed the static gate", problems)

    @staticmethod
    def _gate_offenders(code: str, problems: list[str],
                        proposals: list[ToolProposal]) -> set[str]:
        """Map 'line N [...]' gate findings to the tools whose generated block
        contains them. Returns an empty set when any finding cannot be
        attributed (e.g. py_compile errors) — no surgical drop then."""
        lines = code.splitlines()
        starts: list[tuple[int, str]] = []
        for p in proposals:
            idxs = [i for i, line in enumerate(lines, 1)
                    if line.startswith(f"class {p.model_name}(")
                    or line.startswith(f"def {p.tool_name}(")]
            if idxs:
                starts.append((min(idxs), p.tool_name))
        starts.sort()
        offenders: set[str] = set()
        for prob in problems:
            m = re.match(r"line (\d+)[ :]", prob)
            if not m:
                return set()
            ln = int(m.group(1))
            owner = None
            for start, name in starts:
                if start <= ln:
                    owner = name
                else:
                    break
            if owner is None:
                return set()
            offenders.add(owner)
        return offenders

    # ------------------------------------- step: tool-call format + AUTO-FIX
    def _schemas(self, proposals: list[ToolProposal]) -> list[dict]:
        tools = []
        for p in proposals:
            props = {a.name: _json_prop(a.type) for a in p.args}
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

    # ------------------------------------- step: adapter test + fix loop
    def _test_with_fixes(self, adapter_path: Path,
                         proposals: list[ToolProposal]) -> None:
        """Test every tool with generated sample calls (test model), fix the
        adapter and re-test in a bounded loop (<= self.max_test_cycles, config
        testing.max_cycles). Tools still failing when the loop ends are marked
        failed and removed from the surface — they never reach the judge or
        the registry."""
        failures: dict[str, str] = {}
        for cycle in range(1, self.max_test_cycles + 1):
            failures = dict(self.tracer.act(
                "test every tool with generated sample calls",
                f"tester({adapter_path.name}) via test model "
                f"(cycle {cycle}/{self.max_test_cycles})",
                lambda: self.tester(adapter_path, list(proposals))) or {})
            if not failures:
                self.tracer.record(
                    f"adapter testing cycle {cycle}/{self.max_test_cycles}",
                    "execute sample calls for all tools",
                    f"all {len(proposals)} tools pass", kind="test")
                return
            self.test_cycles = cycle
            if cycle < self.max_test_cycles and self._fix_adapter(
                    adapter_path, proposals, failures):
                continue                            # fixed something — re-test
            break                                   # loop exhausted / no fix applies
        self.tracer.record(
            f"{len(failures)} tool(s) still failing after {self.test_cycles} "
            f"test cycle(s)",
            "mark failed and remove from the surface (terminal for those tools)",
            f"removed: {', '.join(sorted(failures))}", kind="fix")
        proposals[:] = [p for p in proposals if p.tool_name not in failures]
        if not proposals:
            self._reject("not-convertible",
                         "every tool failed adapter testing",
                         [f"{t}: {e}" for t, e in failures.items()])
        self._codegen_adapter(proposals)
        self._render_format_with_fixes(proposals)
        self._save_proposals(proposals)

    def _fix_adapter(self, adapter_path: Path, proposals: list[ToolProposal],
                     failures: dict[str, str]) -> bool:
        """Bounded fix strategies for failing tools — deterministic mutations,
        then the LLM directive tier, then an LLM code patch. A code patch is
        accepted ONLY if it re-passes the static gate (py_compile + INV-1 lint
        + contract), so an LLM-written fix cannot weaken INV-1. Returns True
        if the adapter changed and a re-test is worthwhile."""
        findings = [f"tool {name} failed its sample call: {err}"
                    for name, err in failures.items()]
        if self._repair_from_findings(findings, proposals, adapter_path):
            return True
        if self.repairer:
            directive = self.tracer.act(
                "deterministic fixes exhausted — LLM repair tier (automatic)",
                f"repairer({'; '.join(findings)}) via {JUDGE_MODEL}",
                partial(self.repairer, findings, list(proposals),
                        {"stage": "test", "cycle": self.test_cycles}))
            if self._apply_directive(directive, proposals):
                if not proposals:
                    self._reject("not-convertible",
                                 "LLM repair removed every tool from the surface",
                                 findings)
                self._codegen_adapter(proposals)
                self._render_format_with_fixes(proposals)
                self._save_proposals(proposals)
                return True
        if self.fixer:
            patched = self.tracer.act(
                "LLM code fix for the failing wrappers",
                f"fixer({adapter_path.name}) via test model",
                lambda: self.fixer(adapter_path.read_text(), failures))
            if patched:
                problems = _static_gate(patched)
                if not problems:
                    adapter_path.write_text(patched)
                    self.tracer.record(
                        "LLM code fix accepted",
                        "static gate re-run on the patched source",
                        "gate clean — re-testing", kind="fix")
                    return True
                self.tracer.record(
                    "LLM code fix REJECTED by the static gate",
                    "patched source must pass py_compile + INV-1 lint + contract",
                    "; ".join(problems), kind="fix")
        return False

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
        log.error(
            "conversion of %s rejected [%s]: %s", self.package, code, detail
        )
        raise ConversionRejected(f"{code}: {detail}")

    # ------------------------------------------------------------- helpers
    def _mod_name(self) -> str:
        return re.sub(r"[^a-z0-9]+", "_", self.package.lower()).strip("_")

    def _import_name(self) -> str:
        if self._resolved_import is None:
            self._resolved_import = resolve_import_name(self.package)
        return self._resolved_import

    def _version(self) -> str:
        try:
            return importlib.metadata.version(self.package)
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
                             args=[Arg(**a) for a in p["args"]],
                             skipped=p.get("skipped", [])) for p in raw]


# ------------------------------------------------------- production seams
def production_judge(settings, module_name: str):
    """Real Nemotron judge seam. No Token Factory key -> None -> the agent's
    fail-closed default (blocked) applies (E-20 degraded mode, INV-6)."""
    if not settings.agent_features_enabled:
        return None
    from agents.judge import Judge, JudgeUnavailable
    from agents.tokenfactory import TokenFactoryClient

    judge = Judge(
        TokenFactoryClient(settings.tf_api_key, settings.tf_endpoint,
                           timeout_s=settings.tf_timeout_s),
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
        ctx.kick_search_sync()  # F-040: insert the new tools into tool_embeddings
        return f"auto-merged {module_name}: {len(tools)} tools live"

    return _merge


def llm_repairer(settings):
    """LLM repair tier (fast model): findings -> directive dict, or None.

    The model only steers bounded mutations (drop tools, network off, clamp
    timeout) — it never writes adapter code, so INV-1 cannot be reintroduced."""
    if not settings.agent_features_enabled:
        return None
    from agents.tokenfactory import InferenceError, TokenFactoryClient

    client = TokenFactoryClient(settings.tf_api_key, settings.tf_endpoint,
                                timeout_s=settings.tf_timeout_s)

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


def llm_tester(settings):
    """Adapter tool-testing seam (test model): for every proposed tool, the
    model generates realistic sample arguments, the generated adapter is
    imported from the workdir, args are validated against the tool's pydantic
    input model, and the handler is executed. Returns {tool_name: error} for
    the FAILING tools only (empty dict = all pass)."""
    if not settings.agent_features_enabled:
        return None
    from agents.tokenfactory import TokenFactoryClient

    client = TokenFactoryClient(settings.tf_api_key, settings.tf_endpoint,
                                timeout_s=settings.tf_timeout_s)

    def test(adapter_path: Path, proposals: list[ToolProposal]) -> dict[str, str]:
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            f"adapter_test_{adapter_path.stem}", adapter_path)
        module = importlib.util.module_from_spec(spec)
        try:
            spec.loader.exec_module(module)
        except Exception as exc:
            return {p.tool_name: f"adapter import failed: {type(exc).__name__}: {exc}"
                    for p in proposals}
        failures: dict[str, str] = {}
        for p in proposals:
            handler = getattr(module, p.tool_name, None)
            if handler is None:
                failures[p.tool_name] = "handler missing from generated adapter"
                continue
            sample = _sample_args(client, settings.tf_test_model, p)
            if sample is None:
                failures[p.tool_name] = "sample-argument generation failed"
                continue
            model = getattr(module, p.model_name, None)
            if model is not None:
                try:
                    sample = model(**sample).model_dump()
                except Exception as exc:
                    failures[p.tool_name] = (
                        f"input validation failed: {type(exc).__name__}: "
                        f"{str(exc)[:200]}")
                    continue
            try:
                handler(None, **sample)
            except Exception as exc:
                failures[p.tool_name] = f"{type(exc).__name__}: {str(exc)[:200]}"
        return failures

    return test


def _sample_args(client, model: str, proposal: ToolProposal) -> dict | None:
    """Test model generates one realistic sample-argument set for a tool."""
    from agents.tokenfactory import InferenceError

    if not proposal.args:
        return {}
    spec = ", ".join(
        f"{a.name}: {a.type}"
        + (f" = {a.default}" if a.default is not None else " (required)")
        for a in proposal.args)
    prompt = (
        f"Generate realistic test arguments for '{proposal.tool_name}' — "
        f"{proposal.description}\n"
        f"Arguments: {spec}\n"
        "Respond with ONLY a JSON object mapping argument names to values."
    )
    try:
        completion = client.chat(model, [{"role": "user", "content": prompt}],
                                 max_tokens=2048)
        text = completion.content.strip()
        if text.startswith("```"):
            text = text.strip("`").removeprefix("json").strip()
        data = json.loads(text)
    except (InferenceError, json.JSONDecodeError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    known = {a.name for a in proposal.args}
    return {k: v for k, v in data.items() if k in known}


def llm_fixer(settings):
    """LLM adapter code-fix seam (test model): current adapter source +
    failures -> patched source. The caller re-runs the static gate on the
    patch before accepting it (INV-1 cannot be weakened by a fix)."""
    if not settings.agent_features_enabled:
        return None
    from agents.tokenfactory import InferenceError, TokenFactoryClient

    client = TokenFactoryClient(settings.tf_api_key, settings.tf_endpoint,
                                timeout_s=settings.tf_timeout_s)

    def fix(source: str, failures: dict[str, str]) -> str | None:
        prompt = (
            "This generated MCP tool adapter has failing tools. Fix the wrapper "
            "code without changing the overall structure or removing tools.\n\n"
            "FAILURES:\n"
            + "\n".join(f"- {tool}: {err}" for tool, err in failures.items())
            + f"\n\nADAPTER SOURCE:\n```python\n{source}\n```\n\n"
            "Respond with ONLY the complete corrected Python source."
        )
        try:
            completion = client.chat(
                settings.tf_test_model, [{"role": "user", "content": prompt}],
                max_tokens=32768)
            text = completion.content.strip()
            if text.startswith("```"):
                text = text.strip("`").removeprefix("python").strip()
        except InferenceError:
            return None
        return text or None

    return fix


def llm_proposer(settings):
    """LLM surface selector (fast model): picks the package's top-50
    most-used functions from the introspected candidates.

    The model picks from functions introspected on the installed package —
    top-level members, re-exports, and (for pure-namespace packages)
    submodule functions — it can only select and describe, never invent
    callables (INV-1)."""
    if not settings.agent_features_enabled:
        return None
    from agents.tokenfactory import InferenceError, TokenFactoryClient

    client = TokenFactoryClient(settings.tf_api_key, settings.tf_endpoint,
                                timeout_s=settings.tf_timeout_s)

    def propose(mod) -> list[ToolProposal]:
        candidates = [target for target, _ in _ranked_targets(mod)][:100]
        if not candidates:
            return []
        prompt = (
            f"Package '{mod.__name__}' exposes: {', '.join(candidates)}.\n"
            f"Pick the {MAX_TOOLS} MOST COMMONLY USED functions in real-world "
            "code, worth exposing as sandboxed MCP tools. "
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
            try:
                fn = _resolve(mod, item["target"])
            except AttributeError:
                continue
            projected = ConverterAgent._simple_args(fn)
            if projected is None:
                args, skipped = [], []  # simplified wrapper: fixed signature, no params
            else:
                args, skipped = projected
            doc = ConverterAgent._sanitize(str(item.get("description") or item["target"]))
            out.append(ToolProposal(
                tool_name=f"{prefix}_{item['target'].replace('.', '_')}",
                target=item["target"],
                description=doc[:120],
                args=args,
                skipped=skipped))
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
    """Demo LLM proposal fallback: usage-ranked top-50 surface (public API
    first) — stands in for the LLM 'most commonly used' pick."""
    if mode == "llm-demo":
        def propose(mod) -> list[ToolProposal]:
            prefix = re.sub(r"[^a-z0-9]+", "_", mod.__name__.lower()).strip("_")[:12]
            out: list[ToolProposal] = []
            for target, fn in _ranked_targets(mod):
                projected = ConverterAgent._simple_args(fn)
                if projected is None:
                    continue
                args, skipped = projected
                doc = (inspect.getdoc(fn) or f"{target}() from {mod.__name__}").split("\n")[0]
                out.append(ToolProposal(
                    tool_name=f"{prefix}_{target.replace('.', '_')}",
                    target=target,
                    description=re.sub(r'["{}\\]', "", doc)[:120],
                    args=args,
                    skipped=skipped))
                if len(out) >= MAX_TOOLS:
                    break
            return out
        return propose
    raise ValueError(mode)


def _demo_tester(mode: str):
    """Demo adapter tester: trivial sample args per declared type (str→"x",
    int→1, ...) for required params, then executes each handler in-process.
    Real deployments inject the test-model seam (llm_tester)."""
    if mode == "llm-demo":
        samples = {"str": "x", "int": 1, "float": 1.0, "bool": True,
                   "dict": {}, "list": []}

        def test(adapter_path, proposals) -> dict[str, str]:
            import importlib.util

            spec = importlib.util.spec_from_file_location(
                f"demo_adapter_{adapter_path.stem}", adapter_path)
            module = importlib.util.module_from_spec(spec)
            try:
                spec.loader.exec_module(module)
            except Exception as exc:
                return {p.tool_name: f"adapter import failed: {type(exc).__name__}"
                        for p in proposals}
            failures: dict[str, str] = {}
            for p in proposals:
                handler = getattr(module, p.tool_name, None)
                if handler is None:
                    failures[p.tool_name] = "handler missing from generated adapter"
                    continue
                args = {
                    a.name: samples.get(a.type.replace(" | None", ""), "x")
                    for a in p.args if a.default is None
                }
                try:
                    handler(None, **args)
                except Exception as exc:
                    failures[p.tool_name] = f"{type(exc).__name__}: {str(exc)[:200]}"
            return failures
        return test
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
    ap.add_argument("--tester", choices=["llm-demo"], default=None,
                    help="demo adapter tester (sample-arg execution); omit = skip "
                         "the test stage")
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
        tester=_demo_tester(args.tester) if args.tester else None,
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
              f"judge_cycles={result.judge_cycles} test_cycles={result.test_cycles}")
        print(f"tools: {', '.join(result.tools)}")
        print(f"artifacts: {result.adapter_path} · {result.format_path}")
        print(f"trace: {agent.workdir / 'react_trace.jsonl'}")
        return 0
    print(f"\nno merge — terminal autonomous outcome "
          f"(workdir kept for inspection/resume: {agent.workdir})")
    return 2


if __name__ == "__main__":
    sys.exit(main())
