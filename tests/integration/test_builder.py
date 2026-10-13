"""AC-P6-01/03/04 (offline parts): codegen, static gate, auto-merge, fail-closed."""

from __future__ import annotations

from pathlib import Path

import pytest

from agents.codegen import Builder, introspect_package, render_tool_format, static_gate


@pytest.fixture()
def builder(ctx):
    return Builder(ctx)


def test_introspect_installed_package():
    candidates = introspect_package("json")  # stdlib, always installed
    names = [c.name for c in candidates]
    assert "loads" in names and "dumps" in names


def test_introspect_not_installed():
    from agents.codegen import IntrospectionError

    with pytest.raises(IntrospectionError, match="install"):
        introspect_package("definitely_not_installed_xyz_123")


def test_generate_static_gate_merge(builder, ctx, tmp_env):
    """Happy path without a judge key: generation + static gate; judge fails closed."""
    result = builder.generate("json", ["loads", "dumps"], agent_name="json-agent")
    assert result["state"] == "generated"
    assert set(result["tools"]) == {"json_loads", "json_dumps"}
    # draft rows registered pre-judge
    assert ctx.store.tools.get("json_loads")["state"] == "draft"

    # degraded mode: no TF key -> judge unavailable, stays draft (AC-P6-03)
    merge = builder.judge_and_merge("json")
    assert merge.state == "judge-unavailable"
    assert ctx.store.tools.get("json_loads")["state"] == "draft"


def test_generated_adapter_passes_static_gate(builder):
    result = builder.generate("json", ["loads"])
    source_path = result["path"]
    assert static_gate(Path(source_path).read_text()) == []


def test_regeneration_writes_draft(builder):
    builder.generate("json", ["loads"])
    module_path = builder.ctx.registry.adapters_dir / "json.py"
    module_path.write_text("# manual edit\n" + module_path.read_text())
    result = builder.generate("json", ["dumps"])
    assert result["path"].endswith(".draft.py")
    assert module_path.read_text().startswith("# manual edit")


def test_tool_format_preview(builder, ctx):
    builder.generate("json", ["loads"])
    ctx.registry.refresh_module("json")
    tools = [
        {"name": e.name, "description": e.description, "input_schema": e.schema}
        for e in ctx.registry.list_tools(include_non_live=True)
        if e.adapter_module == "json"
    ]
    fmt = render_tool_format(tools, "/tmp/ws")
    assert "<tools>" in fmt and "json_loads" in fmt
    assert "additionalProperties" in fmt


@pytest.fixture(autouse=True)
def _cleanup_generated(ctx):
    yield
    import os

    for name in ("json.py", "json.draft.py"):
        path = ctx.registry.adapters_dir / name
        if path.exists():
            os.remove(path)
