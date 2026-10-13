"""AC-P6-02/03, AC-F019-03/05/06: judge behavior, adversarial corpus, fail-closed."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from agents.judge import Judge, JudgeUnavailable
from agents.lint import lint_adapter_source
from agents.tokenfactory import InferenceError, TokenFactoryClient

CORPUS = Path(__file__).parent


def _judge_with_response(verdict_doc: dict) -> Judge:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "model": "nemotron-test",
                "choices": [{"message": {"content": json.dumps(verdict_doc)}}],
            },
        )

    client = TokenFactoryClient(
        "test-key", http_client=httpx.Client(transport=httpx.MockTransport(handler))
    )
    return Judge(client, "nvidia/nemotron-3-ultra")


CLEAN_ADAPTER = '''
from pydantic import BaseModel, ConfigDict, Field
from adapters.base import ADAPTER_ERROR, ToolError, ToolSpec

class In(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: str = Field(description="workspace-relative file path")

def demo_read(ws, path: str) -> dict:
    """Read a file from the workspace. Limits: 1 MB, 30s timeout."""
    resolved = ws.resolve(path, must_exist=True)
    return {"content": resolved.read_text()[:1024]}

TOOLS = [
    ToolSpec(
        name="demo_read",
        description="Read a workspace file (limits: 1MB, 30s)",
        input_model=In,
        handler=demo_read,
    )
]
'''


def test_adversarial_corpus_all_blocked_by_lint():
    """Every known-violating adapter in the corpus must be caught."""
    corpus = list(CORPUS.glob("*.py"))
    assert len(corpus) >= 4
    for file in corpus:
        findings = lint_adapter_source(file.read_text())
        assert findings, f"{file.name}: expected lint findings, got none"


def test_linter_blocks_before_model_call():
    """INV-1 hard blocks short-circuit: no model call, verdict=blocked."""
    judge = _judge_with_response({"verdict": "pass", "findings": []})  # would pass if asked
    verdict = judge.review((CORPUS / "eval_injection.py").read_text(), "eval_injection")
    assert verdict.verdict == "blocked"
    assert verdict.model == "static-linter"


def test_judge_pass_parses_and_carries_metadata():
    judge = _judge_with_response({"verdict": "pass", "findings": []})
    verdict = judge.review(CLEAN_ADAPTER, "demo")
    assert verdict.verdict == "pass"
    assert verdict.model == "nemotron-test"
    assert verdict.prompt_hash
    assert verdict.rubric_version == "1.2"


def test_judge_blocked_with_findings():
    judge = _judge_with_response(
        {"verdict": "blocked", "findings": [{"line": 9, "rule": "INV-1", "message": "eval"}]}
    )
    verdict = judge.review(CLEAN_ADAPTER, "demo")
    assert verdict.verdict == "blocked"
    assert verdict.findings[0]["line"] == 9


def test_judge_outage_fails_closed():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"error": "down"})

    client = TokenFactoryClient(
        "test-key", max_retries=1, http_client=httpx.Client(transport=httpx.MockTransport(handler))
    )
    judge = Judge(client, "nvidia/nemotron-3-ultra")
    with pytest.raises(JudgeUnavailable):
        judge.review(CLEAN_ADAPTER, "demo")


def test_judge_unparseable_verdict_fails_closed():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": [{"message": {"content": "looks good!"}}]})

    client = TokenFactoryClient(
        "test-key", http_client=httpx.Client(transport=httpx.MockTransport(handler))
    )
    judge = Judge(client, "nvidia/nemotron-3-ultra")
    with pytest.raises(JudgeUnavailable):
        judge.review(CLEAN_ADAPTER, "demo")


def test_no_api_key_is_inference_error():
    with pytest.raises(InferenceError):
        TokenFactoryClient("")
