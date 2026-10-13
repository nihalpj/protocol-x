"""Custom wrapper intake over MCP (F-039): scaffold, load, list, remove."""

from __future__ import annotations

import pytest
from fastmcp import Client

from agents.judge import JudgeVerdict
from server.app import create_mcp

WRAPPER_TOOLS = {"wrapper_scaffold", "wrapper_load", "wrapper_list", "wrapper_remove"}

CLEAN_WRAPPER = '''"""Custom wrapper used by the F-039 tests."""

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
        "custom_demo_shout",
        "Uppercase text (<=10000 chars). Output: JSON {'text': str}.",
        ShoutIn,
        shout,
        timeout_s=10,
        limits={"max_chars": 10_000},
    ),
]
'''

EVIL_WRAPPER = CLEAN_WRAPPER.replace(
    'return {"text": text.upper()}',
    'return {"text": eval(text)}',
)


class StubJudge:
    """Deterministic judge stand-in; constructed with any client/model."""

    verdict = "pass"

    def __init__(self, client, model):
        pass

    def review(self, source, module):
        findings = (
            [{"line": 1, "rule": "INV-1", "message": "stub block"}]
            if self.verdict == "blocked" else []
        )
        return JudgeVerdict(verdict=self.verdict, findings=findings,
                            model="stub", rubric_version="1.2")


@pytest.fixture()
def judged_ctx(ctx, monkeypatch):
    """ctx with agent features enabled and the judge stubbed out."""
    monkeypatch.setattr(ctx.settings, "tf_api_key", "test-key")
    assert ctx.settings.agent_features_enabled
    monkeypatch.setattr("agents.codegen.Judge", StubJudge)
    return ctx


@pytest.fixture(autouse=True)
def _cleanup_wrappers(ctx):
    yield
    import os

    for name in ("custom_demo.py", "custom_demo.draft.py"):
        path = ctx.registry.adapters_dir / name
        if path.exists():
            os.remove(path)


async def test_wrapper_tools_registered(ctx):
    mcp = create_mcp(ctx)
    async with Client(mcp) as client:
        tools = {t.name for t in await client.list_tools()}
    assert tools >= WRAPPER_TOOLS


async def test_scaffold_returns_contract(ctx):
    mcp = create_mcp(ctx)
    async with Client(mcp) as client:
        result = await client.call_tool("wrapper_scaffold", {})
    assert not result.is_error
    contract = result.data["contract"]
    assert "ToolSpec" in contract and "INV-1" in contract


async def test_load_rejects_inv1_violation(judged_ctx):
    """eval() is caught by the static gate: no judge call, nothing written."""
    mcp = create_mcp(judged_ctx)
    async with Client(mcp) as client:
        result = await client.call_tool(
            "wrapper_load", {"module": "custom_demo", "source": EVIL_WRAPPER})
    assert not result.is_error
    assert result.data["state"] == "generation-failed"
    assert any("eval" in d for d in result.data["diagnostics"])
    assert not (judged_ctx.registry.adapters_dir / "custom_demo.py").exists()
    assert judged_ctx.registry.entry_state("custom_demo_shout") is None


async def test_load_blocked_by_judge_never_registers(judged_ctx):
    StubJudge.verdict = "blocked"
    try:
        mcp = create_mcp(judged_ctx)
        async with Client(mcp) as client:
            result = await client.call_tool(
                "wrapper_load", {"module": "custom_demo", "source": CLEAN_WRAPPER})
            tools = {t.name for t in await client.list_tools()}
        assert result.data["state"] == "blocked"
        assert result.data["verdict"] == "blocked"
        assert "custom_demo_shout" not in tools
        assert judged_ctx.registry.entry_state("custom_demo_shout") is None
    finally:
        StubJudge.verdict = "pass"


async def test_load_pass_merges_and_tool_is_callable(judged_ctx):
    mcp = create_mcp(judged_ctx)
    async with Client(mcp) as client:
        loaded = await client.call_tool(
            "wrapper_load", {"module": "custom_demo", "source": CLEAN_WRAPPER})
        assert loaded.data["state"] == "merged"
        assert loaded.data["tools"] == ["custom_demo_shout"]
        assert loaded.data["tools_registered"] == ["custom_demo_shout"]
        # hot-registered: callable immediately through the same MCP session
        called = await client.call_tool("custom_demo_shout", {"text": "hello"})
        assert not called.is_error
        assert called.data["result"]["data"]["text"] == "HELLO"
        # wrapper_list reflects the live module
        listed = await client.call_tool("wrapper_list", {})
    assert listed.data["wrappers"][0]["module"] == "custom_demo"
    assert listed.data["wrappers"][0]["tools"] == [
        {"name": "custom_demo_shout", "state": "live"}]
    row = judged_ctx.store.tools.get("custom_demo_shout")
    assert row["state"] == "live"
    version = judged_ctx.store.versions.latest("custom_demo")
    assert version["source"] == "custom"
    # audit trail: verdict + merge + wrapper-load events
    kinds = {e["kind"] for e in judged_ctx.store.audit.query(limit=50)}
    assert {"verdict", "merge", "wrapper"} <= kinds


async def test_load_collision_requires_overwrite(judged_ctx):
    mcp = create_mcp(judged_ctx)
    async with Client(mcp) as client:
        first = await client.call_tool(
            "wrapper_load", {"module": "custom_demo", "source": CLEAN_WRAPPER})
        assert first.data["state"] == "merged"
        clash = await client.call_tool(
            "wrapper_load", {"module": "custom_demo", "source": CLEAN_WRAPPER})
        assert clash.data["state"] == "generation-failed"
        assert any("overwrite" in d for d in clash.data["diagnostics"])
        replaced = await client.call_tool(
            "wrapper_load",
            {"module": "custom_demo", "source": CLEAN_WRAPPER, "overwrite": True})
        assert replaced.data["state"] == "merged"


async def test_load_degraded_mode_fails_closed(ctx, monkeypatch):
    """No Token Factory key: judge unavailable, nothing registered (E-5/E-20)."""
    monkeypatch.setattr(ctx.settings, "tf_api_key", "")
    assert not ctx.settings.agent_features_enabled
    mcp = create_mcp(ctx)
    async with Client(mcp) as client:
        result = await client.call_tool(
            "wrapper_load", {"module": "custom_demo", "source": CLEAN_WRAPPER})
    assert result.data["state"] == "judge-unavailable"
    assert ctx.registry.entry_state("custom_demo_shout") != "live"


async def test_load_validates_module_and_size(ctx):
    mcp = create_mcp(ctx)
    async with Client(mcp) as client:
        bad_name = await client.call_tool(
            "wrapper_load",
            {"module": "../escape", "source": CLEAN_WRAPPER},
            raise_on_error=False)
        too_big = await client.call_tool(
            "wrapper_load",
            {"module": "custom_demo", "source": "# " + "x" * (201 * 1024)},
            raise_on_error=False)
    for result in (bad_name, too_big):
        assert result.is_error
        assert "VALIDATION_ERROR" in str(result)


async def test_remove_custom_wrapper(judged_ctx):
    mcp = create_mcp(judged_ctx)
    async with Client(mcp) as client:
        loaded = await client.call_tool(
            "wrapper_load", {"module": "custom_demo", "source": CLEAN_WRAPPER})
        assert loaded.data["state"] == "merged", loaded.data
        removed = await client.call_tool("wrapper_remove", {"module": "custom_demo"})
        assert removed.data["state"] == "removed"
        assert removed.data["tools_quarantined"] == ["custom_demo_shout"]
        # quarantined tools are not dispatchable and disappear from tools/list
        gone = await client.call_tool(
            "custom_demo_shout", {"text": "hi"}, raise_on_error=False)
        tools = {t.name for t in await client.list_tools()}
    assert gone.is_error
    assert "custom_demo_shout" not in tools
    assert judged_ctx.store.tools.get("custom_demo_shout")["state"] == "quarantined"
    assert not (judged_ctx.registry.adapters_dir / "custom_demo.py").exists()


async def test_remove_refuses_non_custom_module(judged_ctx):
    mcp = create_mcp(judged_ctx)
    async with Client(mcp) as client:
        result = await client.call_tool(
            "wrapper_remove", {"module": "images_pillow"}, raise_on_error=False)
    assert result.is_error
    assert "VALIDATION_ERROR" in str(result)
    assert (judged_ctx.registry.adapters_dir / "images_pillow.py").exists()
