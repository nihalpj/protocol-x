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
    return Judge(client, "nvidia/Nemotron-3-Ultra-550b-a55b")


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
    judge = Judge(client, "nvidia/Nemotron-3-Ultra-550b-a55b")
    with pytest.raises(JudgeUnavailable):
        judge.review(CLEAN_ADAPTER, "demo")


def test_judge_unparseable_verdict_fails_closed():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": [{"message": {"content": "looks good!"}}]})

    client = TokenFactoryClient(
        "test-key", http_client=httpx.Client(transport=httpx.MockTransport(handler))
    )
    judge = Judge(client, "nvidia/Nemotron-3-Ultra-550b-a55b")
    with pytest.raises(JudgeUnavailable):
        judge.review(CLEAN_ADAPTER, "demo")


def test_no_api_key_is_inference_error():
    with pytest.raises(InferenceError):
        TokenFactoryClient("")


def test_empty_completion_content_retries_then_fails_closed():
    """Reasoning models can exhaust max_tokens on reasoning_content and return
    empty content — the client must treat that as retryable inference failure."""
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(
            200, json={"choices": [{"message": {"content": ""}, "finish_reason": "length"}]}
        )

    client = TokenFactoryClient(
        "test-key", max_retries=2, http_client=httpx.Client(transport=httpx.MockTransport(handler))
    )
    judge = Judge(client, "nvidia/Nemotron-3-Ultra-550b-a55b")
    with pytest.raises(JudgeUnavailable, match="empty completion content"):
        judge.review(CLEAN_ADAPTER, "demo")
    assert len(calls) == 2  # retried, not parsed as a verdict


def test_judge_verdict_wrapped_in_prose_parses():
    """Reasoning models sometimes leak rationale around the JSON object."""
    content = 'Let me think. The adapter is clean.\n{"verdict": "pass", "findings": []}'

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": content}}]},
        )

    client = TokenFactoryClient(
        "test-key", http_client=httpx.Client(transport=httpx.MockTransport(handler))
    )
    judge = Judge(client, "nvidia/Nemotron-3-Ultra-550b-a55b")
    verdict = judge.review(CLEAN_ADAPTER, "demo")
    assert verdict.verdict == "pass"


def _big_adapter(tools: int, bad_tool: str | None = None) -> str:
    """Build a generated-shape adapter above the chunk threshold."""
    parts = [
        "from __future__ import annotations",
        "from typing import Any",
        "from pydantic import BaseModel, ConfigDict, Field",
        "from adapters.base import ADAPTER_ERROR, ToolError, ToolSpec",
    ]
    names = []
    for i in range(tools):
        name = f"demo_tool{i}"
        names.append(name)
        model = f"DemoTool{i}In"
        comment = '"""' + ("x" * 2500) + '"""'  # pad each block to reach the threshold
        parts.append(
            f"class {model}(BaseModel):\n"
            f'    model_config = ConfigDict(extra="forbid")\n'
            f'    path: str = Field(description="p")\n\n'
            f"def {name}(ws, *, path: str) -> dict:\n"
            f"    {comment}\n"
            + ('    eval(path)\n' if name == bad_tool else "    result = path\n")
            + '    return {"result": result}\n'
        )
    specs = ",\n".join(
        f'    ToolSpec(name="{n}", description="d", input_model=DemoTool{i}In, handler={n})'
        for i, n in enumerate(names)
    )
    parts.append(f"TOOLS = [\n{specs},\n]")
    return "\n\n".join(parts)


def test_split_into_tool_chunks_covers_every_tool():
    from agents.judge import _split_into_tool_chunks

    source = _big_adapter(6)
    assert len(source) > 12_000
    chunks = _split_into_tool_chunks(source)
    assert [label for label, _ in chunks] == [f"demo_tool{i}" for i in range(6)]
    for label, chunk in chunks:
        assert "from pydantic import" in chunk  # header carried into every chunk
        assert f"handler={label}" in chunk  # its ToolSpec entry
        assert f"def {label}(" in chunk  # its handler


def test_small_adapter_not_chunked():
    """Under the token budget the whole adapter goes to the judge in ONE
    request, however many tools it carries."""
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return httpx.Response(
            200, json={"choices": [{"message": {"content": '{"verdict": "pass", "findings": []}'}}]}
        )

    client = TokenFactoryClient(
        "test-key", http_client=httpx.Client(transport=httpx.MockTransport(handler))
    )
    judge = Judge(client, "nvidia/Nemotron-3-Ultra-550b-a55b")
    assert judge.review(CLEAN_ADAPTER, "demo").verdict == "pass"
    assert len(seen) == 1


def test_judge_requests_structured_output():
    """The judge asks for a json_schema response format so the verdict is
    machine-verifiable; _parse_verdict still validates the payload."""
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return httpx.Response(
            200, json={"choices": [{"message": {"content": '{"verdict": "pass", "findings": []}'}}]}
        )

    client = TokenFactoryClient(
        "test-key", http_client=httpx.Client(transport=httpx.MockTransport(handler))
    )
    judge = Judge(client, "nvidia/Nemotron-3-Ultra-550b-a55b")
    judge.review(CLEAN_ADAPTER, "demo")
    rf = seen[0].get("response_format")
    assert rf and rf["type"] == "json_schema"
    schema = rf["json_schema"]["schema"]
    assert schema["properties"]["verdict"]["enum"] == ["pass", "pass-with-warnings", "blocked"]
    assert "tool" in schema["properties"]["findings"]["items"]["properties"]


def test_structured_output_unsupported_falls_back_to_plain_json():
    """A 400 on response_format (model without structured output) retries
    without it instead of failing closed on a feature the endpoint lacks."""
    bodies: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        bodies.append(body)
        if "response_format" in body:
            return httpx.Response(400, text="response_format not supported")
        return httpx.Response(
            200, json={"choices": [{"message": {"content": '{"verdict": "pass", "findings": []}'}}]}
        )

    client = TokenFactoryClient(
        "test-key", max_retries=1, http_client=httpx.Client(transport=httpx.MockTransport(handler))
    )
    judge = Judge(client, "nvidia/Nemotron-3-Ultra-550b-a55b")
    assert judge.review(CLEAN_ADAPTER, "demo").verdict == "pass"
    assert len(bodies) == 2 and "response_format" not in bodies[-1]


def test_chunked_review_aggregates_worst_verdict():
    """A blocked chunk blocks the module; findings from all chunks merge."""
    from agents.judge import _split_into_tool_chunks

    source = _big_adapter(6)
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        prompt = body["messages"][1]["content"]
        seen.append(prompt)
        # block exactly the chunk that contains demo_tool2's handler
        if "def demo_tool2(" in prompt:
            doc = {"verdict": "blocked", "findings": [{"line": 1, "rule": "INV-1", "message": "bad"}]}
        else:
            doc = {"verdict": "pass", "findings": []}
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(doc)}}]})

    client = TokenFactoryClient(
        "test-key", http_client=httpx.Client(transport=httpx.MockTransport(handler))
    )
    # token gate forced to 1: exercises the over-budget per-tool chunk fallback
    judge = Judge(client, "nvidia/Nemotron-3-Ultra-550b-a55b", max_single_request_tokens=1)
    verdict = judge.review(source, "demo")
    assert verdict.verdict == "blocked"
    assert len(seen) == len(_split_into_tool_chunks(source)) == 6
    assert verdict.findings[0]["rule"] == "INV-1"
    assert verdict.prompt_hash  # combined hash present


def test_chunked_review_all_pass():
    source = _big_adapter(3)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"choices": [{"message": {"content": '{"verdict": "pass", "findings": []}'}}]}
        )

    client = TokenFactoryClient(
        "test-key", http_client=httpx.Client(transport=httpx.MockTransport(handler))
    )
    judge = Judge(client, "nvidia/Nemotron-3-Ultra-550b-a55b", max_single_request_tokens=1)
    assert judge.review(source, "demo").verdict == "pass"
