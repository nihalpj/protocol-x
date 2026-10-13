"""Operator tools over MCP: install, introspect, convert, status."""

from __future__ import annotations

import asyncio

import pytest
from fastmcp import Client

from server.app import create_mcp

OPERATOR_TOOLS = {
    "package_install",
    "package_introspect",
    "package_convert",
    "package_convert_bulk",
    "package_convert_status",
}


async def test_operator_tools_registered(ctx):
    mcp = create_mcp(ctx)
    async with Client(mcp) as client:
        tools = {t.name for t in await client.list_tools()}
    assert tools >= OPERATOR_TOOLS


def test_importable_check_skips_stdlib_download():
    from server.operator_tools import _importable

    assert _importable("textwrap") is True  # stdlib: never downloaded
    assert _importable("pydantic") is True  # installed dep
    assert _importable("definitely_not_installed_xyz_123") is False


async def test_install_rejects_invalid_name(ctx):
    mcp = create_mcp(ctx)
    async with Client(mcp) as client:
        result = await client.call_tool(
            "package_install", {"name": "foo;rm -rf /"}, raise_on_error=False)
    assert result.is_error
    assert "VALIDATION_ERROR" in str(result)


async def test_install_already_installed(ctx):
    mcp = create_mcp(ctx)
    async with Client(mcp) as client:
        result = await client.call_tool("package_install", {"name": "pydantic"})
    assert not result.is_error
    assert result.data["state"] == "installed"
    assert result.data["already_installed"] is True
    assert ctx.store.audit.query(kind="install", limit=1)


async def test_introspect_installed_package(ctx):
    mcp = create_mcp(ctx)
    async with Client(mcp) as client:
        result = await client.call_tool("package_introspect", {"package": "json"})
    assert not result.is_error
    names = {f["name"] for f in result.data["functions"]}
    assert "loads" in names and "dumps" in names


async def test_introspect_not_installed(ctx):
    mcp = create_mcp(ctx)
    async with Client(mcp) as client:
        result = await client.call_tool(
            "package_introspect", {"package": "definitely_not_installed_xyz_123"},
            raise_on_error=False)
    assert result.is_error
    assert "package_install" in str(result)


async def test_status_unknown_run(ctx):
    mcp = create_mcp(ctx)
    async with Client(mcp) as client:
        result = await client.call_tool(
            "package_convert_status", {"run_id": 99999}, raise_on_error=False)
    assert result.is_error
    assert "unknown run_id" in str(result)


async def test_status_lists_recent_runs(ctx):
    run_id = ctx.store.runs.open("demo-pkg", "/tmp/x", "/tmp/x/trace.jsonl")
    ctx.store.runs.close(run_id, "rejected", reason_code="judge-blocked-exhausted")
    mcp = create_mcp(ctx)
    async with Client(mcp) as client:
        listed = await client.call_tool("package_convert_status", {})
        one = await client.call_tool("package_convert_status", {"run_id": run_id})
    assert any(r["id"] == run_id for r in listed.data["runs"])
    assert one.data["run"]["outcome"] == "rejected"
    assert one.data["run"]["reason_code"] == "judge-blocked-exhausted"


async def test_convert_degraded_mode_fails_closed(ctx, monkeypatch):
    """No Token Factory key: the judge fails closed (E-20), so the run starts,
    gets a run_id immediately, and ends rejected — never merged."""
    monkeypatch.setattr(ctx.settings, "tf_api_key", "")
    assert not ctx.settings.agent_features_enabled
    mcp = create_mcp(ctx)
    async with Client(mcp) as client:
        started = await client.call_tool(
            "package_convert", {"package": "pydantic", "focus": "validation helpers"})
        assert not started.is_error
        assert started.data["state"] == "started"
        run_id = started.data["run_id"]
        row = None
        for _ in range(120):
            await asyncio.sleep(1)
            status = await client.call_tool(
                "package_convert_status", {"run_id": run_id})
            row = status.data["run"]
            if row["outcome"] is not None:
                break
    assert row is not None and row["outcome"] == "rejected"
    # fail-closed: nothing merged into adapters/
    assert not (ctx.registry.adapters_dir / "pydantic.py").exists()


async def test_bulk_rejects_invalid_input(ctx):
    mcp = create_mcp(ctx)
    async with Client(mcp) as client:
        empty = await client.call_tool(
            "package_convert_bulk", {"packages": []}, raise_on_error=False)
        too_many = await client.call_tool(
            "package_convert_bulk",
            {"packages": [f"pkg{i}" for i in range(11)]}, raise_on_error=False)
        bad_item = await client.call_tool(
            "package_convert_bulk", {"packages": ["json", 42]},
            raise_on_error=False)
    for result in (empty, too_many):
        assert result.is_error
        assert "VALIDATION_ERROR" in str(result)
    # non-string items are rejected at the MCP schema layer, before the handler
    assert bad_item.is_error


async def test_status_unknown_batch(ctx):
    mcp = create_mcp(ctx)
    async with Client(mcp) as client:
        result = await client.call_tool(
            "package_convert_status", {"batch_id": "nope"}, raise_on_error=False)
    assert result.is_error
    assert "unknown batch_id" in str(result)


async def test_bulk_degraded_mode_fails_closed(ctx, monkeypatch):
    """Bulk batch without a judge key: every run starts, gets a run_id, and
    ends rejected; batch state reaches finished with per-package rows."""
    monkeypatch.setattr(ctx.settings, "tf_api_key", "")
    mcp = create_mcp(ctx)
    async with Client(mcp) as client:
        started = await client.call_tool(
            "package_convert_bulk", {"packages": ["string", "colorsys"]})
        assert not started.is_error
        assert started.data["state"] == "started"
        batch_id = started.data["batch_id"]
        batch = None
        for _ in range(120):
            await asyncio.sleep(1)
            status = await client.call_tool(
                "package_convert_status", {"batch_id": batch_id})
            batch = status.data
            if batch["state"] == "finished":
                break
    assert batch is not None and batch["state"] == "finished"
    assert {r["package"] for r in batch["results"]} == {"string", "colorsys"}
    for entry in batch["results"]:
        assert entry["run_id"] is not None
        assert entry["outcome"] == "rejected"
    for mod in ("string", "colorsys"):
        assert not (ctx.registry.adapters_dir / f"{mod}.py").exists()


@pytest.fixture(autouse=True)
def _cleanup_generated(ctx):
    yield
    import os

    for name in ("pydantic.py", "pydantic.draft.py",
                 "string.py", "string.draft.py",
                 "colorsys.py", "colorsys.draft.py"):
        path = ctx.registry.adapters_dir / name
        if path.exists():
            os.remove(path)
