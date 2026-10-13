"""Agent builder codegen (F-019): server-side introspection of the INSTALLED
package (E-3) + Jinja rendering + static gate + auto-merge.

- Introspection never runs against non-installed packages.
- Static gate: py_compile + registry validation + INV-1 linter (E-4/E-11).
- Auto-merge: pass / pass-with-warnings register automatically (warnings
  audit-logged); blocked never registers; judge outage -> draft stays (E-5).
- Regeneration after manual edits writes <module>.draft.py (E-7).
"""

from __future__ import annotations

import ast
import inspect
import py_compile
import re
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from jinja2 import FileSystemLoader
from jinja2.sandbox import SandboxedEnvironment

from agents.judge import Judge, JudgeUnavailable, JudgeVerdict
from agents.lint import lint_adapter_source
from core.logging import fmt_obj, get_logger

log = get_logger("codegen")

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"

_TYPE_MAP = {
    "str": "str", "int": "int", "float": "float", "bool": "bool",
    "list": "list", "dict": "dict", "tuple": "list", "bytes": "bytes",
}


class IntrospectionError(Exception):
    pass


@dataclass
class FunctionCandidate:
    name: str  # dotted target inside the package, e.g. "get" or "Image.open"
    signature: str
    doc: str
    args: list[dict[str, Any]] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)  # params with non-literal defaults


# Annotation names that mean "mapping of key/value pairs" (httpx.QueryParamTypes,
# HeaderTypes, CookieTypes, typing.Mapping, ...) — schema type dict is honest.
_DICT_ANNOTATIONS = {
    "Mapping", "MutableMapping", "Dict",
    "QueryParamTypes", "HeaderTypes", "CookieTypes",
}

# Annotation names that are string-or-str-like (httpx.URL | str, ProxyTypes, ...).
_STR_ANNOTATIONS = {"URL", "URLTypes", "ProxyTypes", "PathLike"}

# Annotation names for rich object params we cannot expose honestly as schema
# scalars (auth callables, byte streams, request bodies) — drop the parameter.
_DROP_ANNOTATIONS = {
    "AuthTypes", "RequestContent", "RequestData", "RequestFiles",
    "SyncByteStream", "AsyncByteStream", "Callable", "CertTypes",
}


def _annotation_names(annotation: Any) -> set[str]:
    """Candidate type names from an annotation, object or string.

    Packages with `from __future__ import annotations` (e.g. httpx) hand us
    plain strings like 'QueryParamTypes | None'; tokenize those. Real type
    objects contribute their __name__.
    """
    if isinstance(annotation, str):
        names = set()
        for token in annotation.replace("|", ",").split(","):
            token = token.strip().strip("\"'")
            if not token or token == "None":
                continue
            names.add(token.split("[")[0].split(".")[-1].strip())
        return names
    name = getattr(annotation, "__name__", None)
    return {name} if name else set()


def _py_type(annotation: Any) -> str | None:
    """Map a Python annotation to a schema type, or None to drop the parameter.

    Union members that are mapping types (httpx QueryParamTypes & friends)
    map to dict; URL-like unions map to str. Rich object types (auth
    callables, byte streams, request bodies) cannot be represented honestly
    in a JSON schema, so the parameter is dropped (INV-3); anything else
    unknown falls back to Any.
    """
    if annotation is inspect.Parameter.empty:
        return "str"
    names = _annotation_names(annotation)
    if names & _DROP_ANNOTATIONS:
        return None
    if names & _DICT_ANNOTATIONS:
        return "dict"
    if names & _STR_ANNOTATIONS:
        return "str"
    mapped = [_TYPE_MAP[n] for n in names if n in _TYPE_MAP]
    if len(mapped) == 1:
        return mapped[0]
    if not names:
        return _TYPE_MAP.get(str(annotation), "Any")
    return "Any"


def _literal_default(value: Any) -> str | None:
    """repr() of a default only if it round-trips as a Python literal.

    Non-literal defaults (sentinel objects, class instances like httpx.Timeout)
    repr() as '<object object at 0x...>' — emitting that would not compile.
    """
    text = repr(value)
    try:
        ast.literal_eval(text)
    except (ValueError, SyntaxError, MemoryError, RecursionError):
        return None
    return text


# Public aliases for the converter agent (agents/converter_agent.py), which
# projects the same honest typing rules onto its ToolProposal surface.
py_schema_type = _py_type
literal_default = _literal_default


def introspect_package(package: str, max_candidates: int = 100) -> list[FunctionCandidate]:
    """Function surface of the *installed* package only (E-3)."""
    try:
        module = __import__(package)
    except ImportError as exc:
        raise IntrospectionError(
            f"package '{package}' is not installed server-side — install it first"
        ) from exc
    candidates: list[FunctionCandidate] = []
    for name, member in inspect.getmembers(module):
        if name.startswith("_"):
            continue
        target = None
        if inspect.isfunction(member) or inspect.isbuiltin(member):
            target = name
        if target is None:
            continue
        try:
            sig = inspect.signature(member)
        except (ValueError, TypeError):
            continue
        args = []
        skipped: list[str] = []
        for pname, param in sig.parameters.items():
            if param.kind in (param.VAR_POSITIONAL, param.VAR_KEYWORD):
                continue  # no *args/**kwargs pass-through — fixed wrappers only (INV-1)
            type_name = _py_type(param.annotation)
            if type_name is None:
                # Rich object param (auth callable, byte stream, body type) —
                # not representable in a JSON schema; drop it. All such
                # params in supported packages carry defaults, so dropping
                # is runtime-safe.
                skipped.append(pname)
                continue
            default_repr: str | None = None
            if param.default is not inspect.Parameter.empty:
                default_repr = _literal_default(param.default)
                if default_repr is None:
                    # Non-literal default (sentinel/instance) — omitting the
                    # parameter keeps the schema honest (INV-3); the package's
                    # own default applies.
                    skipped.append(pname)
                    continue
                default_type = _TYPE_MAP.get(type(param.default).__name__)
                annotation_mapped = (
                    param.annotation is not inspect.Parameter.empty
                    and getattr(param.annotation, "__name__", str(param.annotation)) in _TYPE_MAP
                )
                if default_type and not annotation_mapped and param.default is not None:
                    # Infer the schema type from the default, not "str" (INV-3).
                    type_name = default_type
            if default_repr == "None":
                type_name = f"{type_name} | None"
            args.append(
                {
                    "name": pname,
                    "type": type_name,
                    "default": default_repr,
                    "positional_only": param.kind is inspect.Parameter.POSITIONAL_ONLY,
                    "description": f"parameter {pname}",
                }
            )
        doc = (inspect.getdoc(member) or "").splitlines()
        candidates.append(
            FunctionCandidate(
                name=target,
                signature=str(sig),
                doc=doc[0] if doc else f"{package}.{target}",
                args=args,
                skipped=skipped,
            )
        )
        if len(candidates) >= max_candidates:
            break
    return candidates


def _safe_name(text: str) -> str:
    cleaned = re.sub(r"[^a-z0-9_]", "_", text.lower())
    return cleaned if cleaned and cleaned[0].isalpha() else f"tool_{cleaned}"


def render_adapter(
    module_name: str,
    import_name: str,
    package_version: str,
    functions: list[dict[str, Any]],
    agent_name: str,
    timeout_s: int = 30,
    network: bool = False,
    rubric_version: str = "1.2",
    templates_dir: Path = TEMPLATES_DIR,
) -> str:
    env = SandboxedEnvironment(loader=FileSystemLoader(str(templates_dir)), autoescape=False)
    template = env.get_template("adapter.py.jinja")
    return template.render(
        module_name=module_name,
        import_name=import_name,
        import_is_stdlib=import_name.split(".")[0] in sys.stdlib_module_names,
        package_version=package_version,
        functions=functions,
        agent_name=agent_name,
        timeout_s=timeout_s,
        network=network,
        rubric_version=rubric_version,
    )


def render_tool_format(
    tools: list[dict[str, Any]], workspace_root: str, templates_dir: Path = TEMPLATES_DIR
) -> str:
    """Agent-facing tool-call format preview (AC-F019-07)."""
    env = SandboxedEnvironment(loader=FileSystemLoader(str(templates_dir)), autoescape=False)
    template = env.get_template("tool_call_format.jinja")
    return template.render(tools=tools, workspace_root=workspace_root)


def static_gate(source: str) -> list[str]:
    """py_compile + INV-1 lint + contract checks. Returns diagnostics (E-4)."""
    problems: list[str] = []
    with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as fh:
        fh.write(source)
        tmp = fh.name
    try:
        py_compile.compile(tmp, doraise=True)
    except py_compile.PyCompileError as exc:
        problems.append(f"py_compile: {exc.msg}")
    for finding in lint_adapter_source(source):
        problems.append(f"line {finding.line} [{finding.rule}] {finding.message}")
    return problems


@dataclass
class MergeResult:
    state: str  # merged | blocked | judge-unavailable | generation-failed
    verdict: JudgeVerdict | None = None
    diagnostics: list[str] = field(default_factory=list)
    tools: list[str] = field(default_factory=list)


class Builder:
    """UF-01 server side: generate -> static gate -> judge -> auto-merge."""

    def __init__(self, ctx):
        self.ctx = ctx  # server.app.AppContext

    def generate(
        self,
        package: str,
        selected: list[str],
        agent_name: str | None = None,
        timeout_s: int = 30,
        network: bool = False,
    ) -> dict[str, Any]:
        """Introspect + render. Writes adapters/<pkg>.py (or .draft.py on E-7)."""
        module_name = _safe_name(package)
        candidates = introspect_package(package)
        chosen = [c for c in candidates if c.name in selected]
        if not chosen:
            raise IntrospectionError(f"none of {selected} found in installed package '{package}'")
        import importlib.metadata

        try:
            version = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            version = "unknown"
        functions = [
            {
                "tool_name": f"{module_name}_{_safe_name(c.name)}"[:64],
                "model_name": (
                    f"{module_name.title().replace('_', '')}"
                    f"{_safe_name(c.name).title().replace('_', '')}In"
                ),
                "target": c.name,
                "description": c.doc[:200],
                "args": c.args,
                "skipped": c.skipped,
            }
            for c in chosen
        ]
        source = render_adapter(
            module_name, package, version, functions, agent_name or f"{module_name}-agent",
            timeout_s=timeout_s, network=network,
        )
        problems = static_gate(source)
        if problems:
            log.warning("generated adapter %s rejected by static gate: %s", module_name, problems)
            return {"state": "generation-failed", "diagnostics": problems}

        target = self.ctx.registry.adapters_dir / f"{module_name}.py"
        if target.exists():
            # E-7: never clobber a manually-edited file — write a draft instead
            draft = target.with_suffix(".draft.py")
            draft.write_text(source)
            written = str(draft)
            log.info("adapter %s regenerated as draft %s (existing file preserved)", module_name, draft)
        else:
            target.write_text(source)
            written = str(target)
            self._register_draft(module_name, functions)
            log.info(
                "generated adapter %s (%d tools, %d chars) at %s",
                module_name, len(functions), len(source), written,
            )
        return {
            "state": "generated",
            "module": module_name,
            "path": written,
            "tools": [f["tool_name"] for f in functions],
        }

    def _register_draft(self, module_name: str, functions: list[dict]) -> None:
        for fn in functions:
            self.ctx.store.tools.upsert(fn["tool_name"], "draft", module_name)

    def judge_and_merge(self, module_name: str) -> MergeResult:
        """Judge the generated file; auto-merge on pass / pass-with-warnings."""
        path = self.ctx.registry.adapters_dir / f"{module_name}.py"
        if not path.exists():
            return MergeResult(state="generation-failed", diagnostics=[f"no adapter file {path}"])
        source = path.read_text()
        problems = static_gate(source)
        if problems:
            return MergeResult(state="generation-failed", diagnostics=problems)

        if not self.ctx.settings.agent_features_enabled:
            return MergeResult(
                state="judge-unavailable", diagnostics=["no Token Factory key (degraded mode E-20)"]
            )

        from agents.tokenfactory import TokenFactoryClient

        client = TokenFactoryClient(
            self.ctx.settings.tf_api_key,
            self.ctx.settings.tf_endpoint,
            timeout_s=self.ctx.settings.tf_timeout_s,
        )
        judge = Judge(client, self.ctx.settings.tf_judge_model)
        try:
            verdict = judge.review(source, module_name)
        except JudgeUnavailable as exc:
            return MergeResult(state="judge-unavailable", diagnostics=[str(exc)])

        self.ctx.audit.record("verdict", "judge", {**verdict.to_dict(), "module": module_name})

        if verdict.verdict == "blocked":
            log.warning("adapter %s blocked by judge: %s", module_name, fmt_obj(verdict.findings))
            return MergeResult(state="blocked", verdict=verdict)

        # pass / pass-with-warnings -> auto-merge (UF-01 step 10a/10b)
        version_id = self._record_generated_version(module_name)
        try:
            self.ctx.registry.refresh_module(module_name)
        except Exception as exc:
            log.error("adapter %s failed registry load after judge pass: %s", module_name, exc)
            return MergeResult(
                state="generation-failed", verdict=verdict, diagnostics=[f"registry load failed: {exc}"]
            )
        tools = [
            e.name for e in self.ctx.registry.catalog.values() if e.adapter_module == module_name
        ]
        for name in tools:
            self.ctx.registry.set_state(name, "live")
            self.ctx.store.tools.upsert(name, "live", module_name, "python", version_id)
        log.info(
            "adapter %s merged live (%s): %s", module_name, verdict.verdict, tools
        )
        self.ctx.audit.record(
            "merge", "server",
            {"module": module_name, "verdict": verdict.verdict, "warnings": verdict.findings, "tools": tools},
        )
        return MergeResult(state="merged", verdict=verdict, tools=tools)

    def _record_generated_version(self, module_name: str) -> int:
        from core.hashing import module_source_hash

        digest = module_source_hash(self.ctx.registry.adapters_dir, module_name)
        parent = self.ctx.store.versions.latest(module_name)
        version_id: int = self.ctx.store.versions.add(
            module_name, digest, "generated", parent_id=parent["id"] if parent else None
        )
        return version_id

    def save_manual_edit(self, module_name: str, source: str) -> MergeResult:
        """In-console edit: mark manual, re-judge mandatory (UF-01 step 11)."""
        problems = static_gate(source)
        if problems:
            return MergeResult(state="generation-failed", diagnostics=problems)
        path = self.ctx.registry.adapters_dir / f"{module_name}.py"
        path.write_text(source)
        from core.hashing import module_source_hash

        digest = module_source_hash(self.ctx.registry.adapters_dir, module_name)
        parent = self.ctx.store.versions.latest(module_name)
        self.ctx.store.versions.add(module_name, digest, "manual", parent_id=parent["id"] if parent else None)
        return self.judge_and_merge(module_name)
