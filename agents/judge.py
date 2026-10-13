"""AI-judge (F-019): Nemotron via Token Factory scores generated adapters
against inv-rubric v1.2. Fail closed (INV-6): an unreachable judge or an
unparseable verdict means nothing registers.

Every verdict is persisted as an audit_events(kind='verdict') row with the
model, prompt hash, and rubric version (AC-F019-05).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

import yaml

from agents.lint import LintFinding, lint_adapter_source
from agents.tokenfactory import InferenceError, TokenFactoryClient

RUBRIC_PATH = Path(__file__).resolve().parent.parent / "policies" / "inv-rubric-v1.2.yaml"

VERDICTS = ("pass", "pass-with-warnings", "blocked")


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
    def __init__(self, client: TokenFactoryClient, model: str, rubric_path: Path = RUBRIC_PATH):
        self.client = client
        self.model = model
        self.rubric = load_rubric(rubric_path)
        self.rubric_version = str(self.rubric.get("version", "unknown"))

    def review(self, adapter_source: str, module_name: str) -> JudgeVerdict:
        """Static lint first (deterministic blocks), then the model judge."""
        import time

        lint_findings = lint_adapter_source(adapter_source)
        hard_blocks = [f for f in lint_findings if f.rule in ("inv-1", "syntax", "contract")]
        if hard_blocks:
            # Deterministic block — no model call needed; nothing can override INV-1.
            return JudgeVerdict(
                verdict="blocked",
                findings=[f.__dict__ for f in lint_findings],
                model="static-linter",
                prompt_hash="",
                rubric_version=self.rubric_version,
                ts=time.time(),
            )

        prompt = self._build_prompt(adapter_source, module_name, lint_findings)
        try:
            completion = self.client.chat(
                self.model,
                [
                    {"role": "system", "content": _SYSTEM_PROMPT},
                    {"role": "user", "content": prompt},
                ],
            )
        except InferenceError as exc:
            raise JudgeUnavailable(str(exc)) from exc
        verdict = self._parse_verdict(completion.content)
        verdict.model = completion.model
        verdict.prompt_hash = completion.prompt_hash
        verdict.rubric_version = self.rubric_version
        verdict.ts = time.time()
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
            '"findings": [{"line": <int>, "rule": "<rubric id>", "message": "<short>"}]}'
        )

    def _parse_verdict(self, content: str) -> JudgeVerdict:
        text = content.strip()
        if text.startswith("```"):
            text = text.strip("`").removeprefix("json").strip()
        try:
            data = json.loads(text)
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
