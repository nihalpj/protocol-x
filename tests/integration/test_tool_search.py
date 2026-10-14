"""F-040 integration: tool_search over MCP — semantic ranking with a stubbed
provider, degraded lexical mode, dirty-sync after a wrapper merge, ACL guard."""

from __future__ import annotations

import asyncio

import pytest
from fastmcp import Client

from adapters.base import ToolError
from agents.embeddings import EmbeddingUnavailable
from server.app import create_mcp

VOCAB = ["image", "resize", "shout", "uppercase", "text", "json", "pdf"]

CLEAN_WRAPPER = '''"""Custom wrapper used by the F-040 tests."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from adapters.base import ToolSpec


class ShoutIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(max_length=10_000, description="Text to uppercase (<=10000 chars)")


def shout(ws, text: str) -> dict:
    return {"text": text.upper()}


TOOLS = [
    ToolSpec(
        "search_demo_shout",
        "Uppercase text (<=10000 chars). Output: JSON {'text': str}.",
        ShoutIn,
        shout,
        timeout_s=10,
        limits={"max_chars": 10_000},
    ),
]
'''


class FakeProvider:
    """Keyword-bag embeddings: vec[i] = count of VOCAB[i] in the text."""

    def __init__(self, name: str = "fake:v1", fail: bool = False):
        self.name = name
        self.dims = len(VOCAB)
        self.fail = fail

    def embed(self, texts: list[str]) -> list[list[float]]:
        if self.fail:
            raise EmbeddingUnavailable("fake outage")
        return [[float(t.lower().count(w)) for w in VOCAB] for t in texts]


@pytest.fixture()
def search_ctx(ctx):
    ctx.search.provider = FakeProvider()
    ctx.search.mark_dirty()
    return ctx


@pytest.fixture(autouse=True)
def _cleanup_wrappers(ctx):
    yield
    import os

    for name in ("search_demo.py", "search_demo.draft.py"):
        path = ctx.registry.adapters_dir / name
        if path.exists():
            os.remove(path)


async def test_tool_search_registered(ctx):
    mcp = create_mcp(ctx)
    async with Client(mcp) as client:
        tools = {t.name for t in await client.list_tools()}
    assert "tool_search" in tools


async def test_tool_search_semantic_ranking(search_ctx):
    mcp = create_mcp(search_ctx)
    async with Client(mcp) as client:
        result = await client.call_tool("tool_search", {"query": "resize an image"})
    assert not result.is_error
    data = result.data
    assert data["backend"] == "fake"
    names = [r["name"] for r in data["results"]]
    assert names[0] == "image_resize"
    hit = data["results"][0]
    assert {"name", "description", "adapter_module", "state", "score"} <= set(hit)


async def test_tool_search_lexical_when_provider_fails(ctx):
    """Provider outage must degrade to lexical search, never an error."""
    ctx.search.provider = FakeProvider(fail=True)
    ctx.search.mark_dirty()
    mcp = create_mcp(ctx)
    async with Client(mcp) as client:
        result = await client.call_tool("tool_search", {"query": "resize image"})
    assert not result.is_error
    assert result.data["backend"] == "lexical"
    assert any("image" in r["name"] for r in result.data["results"])


async def test_tool_search_lexical_when_backend_off(ctx):
    """conftest forces search.backend=off: provider is None, lexical answers."""
    assert ctx.search.provider is None
    mcp = create_mcp(ctx)
    async with Client(mcp) as client:
        result = await client.call_tool("tool_search", {"query": "image resize"})
    assert not result.is_error
    assert result.data["backend"] == "lexical"
    assert any(r["name"] == "image_resize" for r in result.data["results"])


async def test_tool_search_rejects_empty_query(ctx):
    mcp = create_mcp(ctx)
    async with Client(mcp) as client:
        result = await client.call_tool(
            "tool_search", {"query": "   "}, raise_on_error=False
        )
    assert result.is_error


async def test_merged_wrapper_becomes_findable(search_ctx, monkeypatch):
    """AC-F040-05: after a wrapper_load merge the new tool's embedding is
    inserted eagerly (background kick — no search call needed) and the tool
    is findable in the same session without restart."""
    import time

    from agents.judge import JudgeVerdict

    class StubJudge:
        def __init__(self, client, model): pass

        def review(self, source, module):
            return JudgeVerdict(verdict="pass", findings=[], model="stub",
                                rubric_version="1.2")

    monkeypatch.setattr(search_ctx.settings, "tf_api_key", "test-key")
    monkeypatch.setattr("agents.codegen.Judge", StubJudge)

    mcp = create_mcp(search_ctx)
    async with Client(mcp) as client:
        loaded = await client.call_tool(
            "wrapper_load", {"module": "search_demo", "source": CLEAN_WRAPPER})
        assert loaded.data["state"] == "merged"
        # eager insert: the merge kicked a background sync; the embedding row
        # lands without any search call. The embedded text carries the schema
        # parameter detail (name + description).
        row = None
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            row = await asyncio.to_thread(
                search_ctx.store.embeddings.get, "search_demo_shout")
            if row is not None:
                break
            await asyncio.sleep(0.05)
        assert row is not None, "merged tool was never embedded"
        assert "text to uppercase" in row["text"].lower()
        result = await client.call_tool(
            "tool_search", {"query": "shout uppercase text", "limit": 50})
    names = [r["name"] for r in result.data["results"]]
    assert "search_demo_shout" in names
    assert result.data["backend"] == "fake"


def test_tool_search_acl_guard(ctx):
    """tool_search runs under the same ACL guard as every operator tool."""
    from server.auth import current_token, hash_token
    from server.operator_tools import _guard

    ctx.token_store.acl[hash_token("tok-restricted")] = {"image_info"}
    token = current_token.set("tok-restricted")
    try:
        with pytest.raises(ToolError):
            _guard(ctx, "tool_search")
        _guard(ctx, "image_info")  # allowed tool passes
    finally:
        current_token.reset(token)
