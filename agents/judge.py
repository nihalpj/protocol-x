"""AI-judge (F-019): Nemotron via Token Factory scores generated adapters
against inv-rubric v1.2. Fail closed (INV-6): an unreachable judge or an
unparseable verdict means nothing registers.

The judge is the FINAL gate: adapters reach it after the converter's
tool-testing/fix stage (test model smoke tests, bounded fix loop), and the
whole package adapter is reviewed in ONE structured-output request whenever
the input fits the token budget (MAX_SINGLE_REQUEST_TOKENS).

Every verdict is persisted as an audit_events(kind='verdict') row with the
model, prompt hash, and rubric version (AC-F019-05).
"""

from __future__ import annotations

import ast
import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

import yaml

from agents.lint import LintFinding, lint_adapter_source
from agents.tokenfactory import InferenceError, TokenFactoryClient
from core.logging import fmt_obj, get_logger

log = get_logger("judge")

RUBRIC_PATH = Path(__file__).resolve().parent.parent / "policies" / "inv-rubric-v1.2.yaml"

VERDICTS = ("pass", "pass-with-warnings", "blocked")

# One judge request carries the whole package adapter (all tools together) as
# long as the input stays under this token budget; only adapters beyond it are
# split into per-tool chunks (endpoint context safety valve).
MAX_SINGLE_REQUEST_TOKENS = 950_000

# Structured-output schema the judge is asked to answer with (OpenAI-compatible
# json_schema response format); _parse_verdict still verifies the payload.
_VERDICT_RESPONSE_FORMAT = {
    "type": "json_schema",
    "json_schema": {
        "name": "judge_verdict",
        "strict": True,
        "schema": {
            "type": "object",
            "properties": {
                "verdict": {"type": "string", "enum": list(VERDICTS)},
                "findings": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "tool": {"type": "string"},
                            "line": {"type": "integer"},
                            "rule": {"type": "string"},
                            "message": {"type": "string"},
                        },
                        "required": ["tool", "rule", "message"],
                        "additionalProperties": False,
                    },
                },
            },
            "required": ["verdict", "findings"],
            "additionalProperties": False,
        },
    },
}


def _estimate_tokens(text: str) -> int:
    """Input-token estimate: tiktoken when installed, else ~4 chars/token."""
    try:
        import tiktoken

        return len(tiktoken.get_encoding("o200k_base").encode(text))
    except ImportError:
        return max(1, len(text) // 4)


@dataclass
class JudgeVerdict:
    verdict: str  # pass | pass-with-warnings | blocked
    findings: list[dict[str, Any]] = field(default_factory=list)
    model: str = ""
    prompt_hash: str = ""
    rubric_version: str = ""
    ts: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict,
            "findings": self.findings,
            "model": self.model,
            "prompt_hash": self.prompt_hash,
            "rubric_version": self.rubric_version,
            "ts": self.ts,
        }


class JudgeUnavailable(Exception):
    """Judge endpoint unreachable or verdict unparseable — fail closed (E-5)."""


def load_rubric(path: Path = RUBRIC_PATH) -> dict:
    return cast(dict, yaml.safe_load(path.read_text()))


class Judge:
    def __init__(self, client: TokenFactoryClient, model: str,
                 rubric_path: Path = RUBRIC_PATH,
                 max_single_request_tokens: int = MAX_SINGLE_REQUEST_TOKENS):
        self.client = client
        self.model = model
        self.max_single_request_tokens = max_single_request_tokens
        self.rubric = load_rubric(rubric_path)
        self.rubric_version = str(self.rubric.get("version", "unknown"))

    def review(self, adapter_source: str, module_name: str) -> JudgeVerdict:
        """Static lint first (deterministic blocks), then the model judge.

        The whole package adapter — every generated tool — goes to the judge
        in ONE request with a structured-output schema, as long as the
        estimated input stays under max_single_request_tokens; only beyond
        that budget does review fall back to per-tool chunks."""
        import time

        lint_findings = lint_adapter_source(adapter_source)
        hard_blocks = [f for f in lint_findings if f.rule in ("inv-1", "syntax", "contract")]
        if hard_blocks:
            # Deterministic block — no model call needed; nothing can override INV-1.
            log.warning(
                "adapter %s blocked by static lint: %s",
                module_name, [f"{f.rule}:{f.line}" for f in hard_blocks],
            )
            return JudgeVerdict(
                verdict="blocked",
                findings=[f.__dict__ for f in lint_findings],
                model="static-linter",
                prompt_hash="",
                rubric_version=self.rubric_version,
                ts=time.time(),
            )

        prompt = self._build_prompt(adapter_source, module_name, lint_findings)
        est_tokens = _estimate_tokens(_SYSTEM_PROMPT) + _estimate_tokens(prompt)
        if est_tokens < self.max_single_request_tokens:
            log.info("adapter %s est. %d tokens (< %d) — single judge request "
                     "for all tools", module_name, est_tokens,
                     self.max_single_request_tokens)
            return self._review_one(adapter_source, module_name, lint_findings)

        chunks = _split_into_tool_chunks(adapter_source)
        if len(chunks) <= 1:
            log.warning("adapter %s est. %d tokens but not splittable — "
                        "single request anyway (endpoint may reject)",
                        module_name, est_tokens)
            return self._review_one(adapter_source, module_name, lint_findings)

        # Oversized adapter: judge each tool chunk independently; the module
        # verdict is the worst chunk verdict (fail closed, INV-6).
        log.info("adapter %s est. %d tokens (>= %d) — judging %d per-tool chunks",
                 module_name, est_tokens, self.max_single_request_tokens, len(chunks))
        chunk_verdicts: list[JudgeVerdict] = []
        for i, (label, chunk) in enumerate(chunks, 1):
            chunk_verdicts.append(
                self._review_one(chunk, f"{module_name}:{label} (part {i}/{len(chunks)})",
                                 lint_findings)
            )
        severity = {v: i for i, v in enumerate(VERDICTS)}  # pass < warnings < blocked
        worst = max(chunk_verdicts, key=lambda v: severity[v.verdict])
        findings = [f for v in chunk_verdicts for f in v.findings]
        combined_hash = hashlib.sha256(
            "".join(v.prompt_hash for v in chunk_verdicts).encode()
        ).hexdigest()
        log.info("judge chunk verdicts for %s: %s -> %s",
                 module_name, [v.verdict for v in chunk_verdicts], worst.verdict)
        return JudgeVerdict(
            verdict=worst.verdict,
            findings=findings,
            model=worst.model,
            prompt_hash=combined_hash,
            rubric_version=self.rubric_version,
            ts=time.time(),
        )

    def _review_one(self, source: str, module_name: str,
                    lint_findings: list[LintFinding]) -> JudgeVerdict:
        import time

        prompt = self._build_prompt(source, module_name, lint_findings)
        log.info("judging adapter %s with %s (%d chars)", module_name, self.model, len(prompt))
        log.debug("judge prompt for %s:\n%s", module_name, prompt)
        last_error: JudgeUnavailable | None = None
        for attempt in range(2):  # reasoning endpoints are stochastic: one parse retry
            try:
                try:
                    completion = self.client.chat(
                        self.model,
                        [
                            {"role": "system", "content": _SYSTEM_PROMPT},
                            {"role": "user", "content": prompt},
                        ],
                        max_tokens=32768,  # reasoning models spend budget on reasoning_content
                        response_format=_VERDICT_RESPONSE_FORMAT,
                    )
                except InferenceError as exc:
                    if "returned 400" not in str(exc):
                        raise
                    # Endpoint/model without structured-output support: fall
                    # back to the plain JSON prompt (still verified below).
                    log.warning("structured output rejected (%s) — plain JSON fallback",
                                fmt_obj(exc, limit=200))
                    completion = self.client.chat(
                        self.model,
                        [
                            {"role": "system", "content": _SYSTEM_PROMPT},
                            {"role": "user", "content": prompt},
                        ],
                        max_tokens=32768,
                    )
            except InferenceError as exc:
                log.error("judge unavailable for %s: %s", module_name, exc)
                raise JudgeUnavailable(str(exc)) from exc
            log.debug("judge raw response for %s: %s", module_name, fmt_obj(completion.content))
            try:
                verdict = self._parse_verdict(completion.content)
                break
            except JudgeUnavailable as exc:
                last_error = exc
                log.warning("judge response for %s unparseable (attempt %d): %s",
                            module_name, attempt + 1, exc)
        else:
            raise last_error or JudgeUnavailable("judge produced no verdict")
        verdict.model = completion.model
        verdict.prompt_hash = completion.prompt_hash
        verdict.rubric_version = self.rubric_version
        verdict.ts = time.time()
        log.info(
            "judge verdict for %s: %s (%d findings, model=%s)",
            module_name, verdict.verdict, len(verdict.findings), verdict.model,
        )
        return verdict

    def _build_prompt(self, source: str, module_name: str, lint: list[LintFinding]) -> str:
        rubric_text = yaml.safe_dump(self.rubric)
        lint_text = "\n".join(f"  line {f.line} [{f.rule}] {f.message}" for f in lint) or "  none"
        return (
            f"Review this generated MCP tool adapter against the rubric.\n\n"
            f"RUBRIC (inv-rubric v{self.rubric_version}):\n{rubric_text}\n"
            f"STATIC LINT FINDINGS (informational):\n{lint_text}\n\n"
            f"ADAPTER adapters/{module_name}.py:\n```python\n{source}\n```\n\n"
            "Respond with ONLY a JSON object: "
            '{"verdict": "pass"|"pass-with-warnings"|"blocked", '
            '"findings": [{"tool": "<tool name>", "line": <int>, '
            '"rule": "<rubric id>", "message": "<short>"}]}'
        )

    def _parse_verdict(self, content: str) -> JudgeVerdict:
        text = content.strip()
        if text.startswith("```"):
            text = text.strip("`").removeprefix("json").strip()
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            # Tolerate prose around the JSON object (reasoning models sometimes
            # leak rationale into content); fail closed if no object is found.
            start, end = text.find("{"), text.rfind("}")
            if start == -1 or end <= start:
                raise JudgeUnavailable(
                    f"judge returned unparseable verdict: {text[:120]!r}"
                ) from None
            try:
                data = json.loads(text[start : end + 1])
            except json.JSONDecodeError as exc:
                # Fail closed: an unparseable verdict is never a pass (INV-6)
                raise JudgeUnavailable(f"judge returned unparseable verdict: {exc}") from exc
        verdict = data.get("verdict")
        if verdict not in VERDICTS:
            raise JudgeUnavailable(f"judge returned invalid verdict: {verdict!r}")
        findings = data.get("findings") or []
        return JudgeVerdict(
            verdict=verdict,
            findings=[f for f in findings if isinstance(f, dict)],
        )


_SYSTEM_PROMPT = (
    "You are the Protocol-X security judge. You review generated MCP tool adapters "
    "for invariant violations (INV-1 no arbitrary code, INV-2 sandbox compatibility, "
    "INV-3 schema honesty, INV-4 structured errors, INV-5 statelessness). "
    "A single blocking violation means verdict=blocked. Cosmetic issues are warnings. "
    "Answer with JSON only."
)


def _split_into_tool_chunks(source: str) -> list[tuple[str, str]]:
    """Split a generated adapter into per-tool review chunks.

    Each chunk carries the import header, the tool's input model, its handler,
    and its ToolSpec entry — everything the rubric needs for that tool. Returns
    [] when the source doesn't match the generated shape (caller falls back to
    whole-file review). Splitting is the over-budget fallback only — the
    caller (Judge.review) decides via the token gate.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    lines = source.splitlines()

    def segment(node: ast.AST) -> str:
        return "\n".join(lines[node.lineno - 1 : node.end_lineno])  # type: ignore[attr-defined]

    header: list[str] = []
    classes: dict[str, ast.ClassDef] = {}
    handlers: dict[str, ast.FunctionDef] = {}
    tools_list: ast.List | None = None
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            header.append(segment(node))
        elif isinstance(node, ast.ClassDef):
            classes[node.name] = node
        elif isinstance(node, ast.FunctionDef):
            handlers[node.name] = node
        elif (
            isinstance(node, ast.Assign)
            and any(isinstance(t, ast.Name) and t.id == "TOOLS" for t in node.targets)
            and isinstance(node.value, ast.List)
        ):
            tools_list = node.value
    if tools_list is None or not handlers:
        return []

    header_text = "\n".join(header)
    chunks: list[tuple[str, str]] = []
    for elt in tools_list.elts:
        if not isinstance(elt, ast.Call):
            continue
        kw = {k.arg: k.value for k in elt.keywords if k.arg}
        handler_node = kw.get("handler")
        model_node = kw.get("input_model")
        if not isinstance(handler_node, ast.Name) or not isinstance(model_node, ast.Name):
            continue
        func = handlers.get(handler_node.id)
        model = classes.get(model_node.id)
        if func is None or model is None:
            continue
        parts = [header_text, segment(model), segment(func), segment(elt)]
        chunks.append((handler_node.id, "\n\n".join(parts)))
    return chunks
