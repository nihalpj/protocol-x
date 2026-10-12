"""AC-P1-01/02/06/07: stdio boot, tools/list schemas, health, lazy-load seam."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from fastmcp import Client
from fastmcp.client.transports import PythonStdioTransport


def _transport(tmp_env) -> PythonStdioTransport:
    import core

    root = Path(core.__file__).resolve().parent.parent
    return PythonStdioTransport(
        root / "server" / "__main__.py",
        args=["serve", "--transport", "stdio"],
        env={
            "PROTOX_DATA_DIR": str(tmp_env["data_dir"]),
            "PROTOX_WORKSPACE": str(tmp_env["workspace"]),
            "PYTHONPATH": str(root),
        },
        cwd=str(root),
    )


async def test_stdio_boot_and_catalog(tmp_env):
    transport = _transport(tmp_env)
    start = time.monotonic()
    async with Client(transport) as client:
        tools = await client.list_tools()
        boot_s = time.monotonic() - start
    names = sorted(t.name for t in tools)
    assert names == sorted(
        [
            "image_info", "image_resize", "image_crop", "image_rotate",
            "image_convert_format", "image_thumbnail", "image_compress_jpeg",
            "image_strip_metadata",
        ]
    )
    assert boot_s <= 5.0  # generous CI bound; AC-F001-01 target is 2 s warm
    from jsonschema.validators import validator_for

    for tool in tools:
        validator_for(tool.input_schema).check_schema(tool.input_schema)


async def test_stdio_call_unknown_tool(tmp_env):
    transport = _transport(tmp_env)
    async with Client(transport) as client:
        from fastmcp.exceptions import ToolError as ClientToolError

        with pytest.raises(Exception):
            await client.call_tool("no_such_tool", {})


async def test_health_endpoint(ctx):
    from starlette.testclient import TestClient

    from server.app import create_mcp

    mcp = create_mcp(ctx)
    app = mcp.http_app()
    with TestClient(app) as client:
        resp = client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["tools"] == 8


def test_image_resize_benchmark(ctx, sample_png):
    start = time.monotonic()
    out = ctx.dispatch("image_resize", {"path": sample_png, "width": 800, "height": 600})
    elapsed = time.monotonic() - start
    assert elapsed <= 3.0  # AC-P1-02
    ref = out["result"]["data"]["output"]
    from PIL import Image

    img = Image.open(ctx.workspace.root / ref["path"])
    assert img.size == (800, 600)


def test_lazy_load_with_200_stub_adapters(tmp_path, tmp_env):
    """AC-P1-07: catalog assembly stays fast as the folder grows (lazy seam)."""
    import core

    root = Path(core.__file__).resolve().parent.parent
    adapters_dir = tmp_path / "adapters"
    adapters_dir.mkdir()
    (adapters_dir / "base.py").write_text((root / "adapters" / "base.py").read_text())
    stub = (
        "from pydantic import BaseModel, ConfigDict\n"
        "from adapters.base import ToolSpec\n"
        "class In(BaseModel):\n    model_config = ConfigDict(extra='forbid')\n"
        "def h(ws):\n    return dict()\n"
        "TOOLS = [ToolSpec('stub_{i}', 'stub tool {i}', In, h)]\n"
    )
    for i in range(200):
        (adapters_dir / f"stub_{i:03d}.py").write_text(stub.replace("{i}", str(i)))

    from server.config import Settings
    from server.registry import ToolRegistry
    from core.db import Store

    settings = Settings()
    store = Store(settings.db_path)
    registry = ToolRegistry(store, settings.data_dir, adapters_dir=adapters_dir)

    start = time.monotonic()
    registry.load()
    cold_s = time.monotonic() - start
    assert len(registry.catalog) == 200

    start = time.monotonic()
    registry2 = ToolRegistry(store, settings.data_dir, adapters_dir=adapters_dir)
    registry2.load()
    warm_list = registry2.list_tools()
    warm_s = time.monotonic() - start
    assert len(warm_list) == 200
    assert cold_s <= 2.0, f"cold catalog assembly took {cold_s:.2f}s"
    assert warm_s <= 0.5, f"warm catalog load took {warm_s:.2f}s"  # cache path (tools/list ≤100ms server-side)
    store.close()
