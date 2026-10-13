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
    source = Path(source_path).read_text()
    assert static_gate(source) == []
    # generated code must stay repo-lint clean (contributing.md: ruff check .)
    assert "# ruff: noqa: E501" in source  # machine-length signatures/descriptions
    # stdlib packages import inside the stdlib block, before `from typing`
    assert source.index("import json") < source.index("from typing import Any")


def test_non_literal_defaults_are_skipped(builder):
    """Regression: repr() of sentinel/instance defaults (<object object at ...>)
    must never reach generated code — the param is omitted instead (INV-3)."""
    import json as json_mod

    # json.dumps has defaults like cls=None; craft a worse case via a fake module
    # would be overkill — assert on the general rule with a stdlib function whose
    # defaults are all literal, plus direct unit coverage of the helper.
    from agents.codegen import _literal_default

    assert _literal_default(5.0) == "5.0"
    assert _literal_default("x") == "'x'"
    assert _literal_default(None) == "None"
    assert _literal_default(object()) is None  # '<object object at 0x...>'
    assert _literal_default(json_mod.JSONEncoder()) is None

    candidates = introspect_package("json")
    dumps = next(c for c in candidates if c.name == "dumps")
    for arg in dumps.args:
        if arg["default"] is not None:
            import ast

            ast.literal_eval(arg["default"])  # every emitted default must be a literal


def test_default_type_inferred_from_literal(builder):
    """Un-annotated params get their schema type from a literal default (INV-3)."""
    candidates = introspect_package("json")
    dumps = next(c for c in candidates if c.name == "dumps")
    types = {a["name"]: a["type"] for a in dumps.args}
    # json.dumps(..., skipkeys=False, ensure_ascii=True, indent=None, ...)
    assert types["skipkeys"] == "bool"
    assert types["indent"].endswith("| None")


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
