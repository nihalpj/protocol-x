"""AC-P3-01…04: HTTP transport, bearer auth, ACL, rate limit, metrics, parity."""

from __future__ import annotations

import socket
import threading
import time

import pytest
import uvicorn
from fastmcp import Client
from fastmcp.client.transports import PythonStdioTransport, StreamableHttpTransport


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class RunningServer:
    def __init__(self, ctx, port: int):
        from server.app import build_http_app

        config = uvicorn.Config(build_http_app(ctx), host="127.0.0.1", port=port, log_level="error")
        self.server = uvicorn.Server(config)
        self.thread = threading.Thread(target=self.server.run, daemon=True)
        self.port = port

    def __enter__(self):
        self.thread.start()
        import httpx

        for _ in range(100):
            if self.server.started:
                try:
                    if httpx.get(f"http://127.0.0.1:{self.port}/health", timeout=1).status_code == 200:
                        break
                except Exception:
                    pass
            time.sleep(0.05)
        else:
            raise RuntimeError("server did not start")
        return self

    def __exit__(self, *exc):
        self.server.should_exit = True
        self.thread.join(timeout=5)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/mcp"


@pytest.fixture()
def auth_settings(tmp_env, monkeypatch):
    monkeypatch.setenv("PROTOX_TOKENS", "tok-full,tok-img")
    monkeypatch.setenv("PROTOX_TOOL_ACL", '{"tok-img": ["image_info"]}')
    monkeypatch.setenv("PROTOX_RATE_LIMIT", "5")
    from server.config import Settings

    return Settings()


@pytest.fixture()
def auth_ctx(auth_settings):
    from server.app import AppContext

    context = AppContext(auth_settings)
    yield context
    context.store.close()


async def test_no_token_401(auth_ctx):
    with RunningServer(auth_ctx, _free_port()) as srv:
        import httpx

        resp = httpx.post(f"{srv.url}/", json={"jsonrpc": "2.0", "id": 1, "method": "initialize"})
        assert resp.status_code == 401
        resp = httpx.post(
            f"{srv.url}/",
            json={"jsonrpc": "2.0", "id": 1, "method": "initialize"},
            headers={"Authorization": "Bearer wrong"},
        )
        assert resp.status_code == 401
        # health stays open
        assert httpx.get(f"http://127.0.0.1:{srv.port}/health").status_code == 200


async def test_query_token_bootstrap(auth_ctx):
    """Browser console flow: /?token=... authenticates like a Bearer header."""
    with RunningServer(auth_ctx, _free_port()) as srv:
        import httpx

        base = f"http://127.0.0.1:{srv.port}"
        assert httpx.get(f"{base}/api/status").status_code == 401
        assert httpx.get(f"{base}/api/status?token=wrong").status_code == 401
        resp = httpx.get(f"{base}/api/status?token=tok-full")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"
        # SPA shell reachable with the bootstrap token too
        assert httpx.get(f"{base}/?token=tok-full").status_code == 200


async def test_valid_token_call(auth_ctx, sample_png):
    with RunningServer(auth_ctx, _free_port()) as srv:
        async with Client(
            StreamableHttpTransport(srv.url, headers={"Authorization": "Bearer tok-full"})
        ) as client:
            result = await client.call_tool("image_info", {"path": sample_png})
            assert not result.is_error


async def test_acl_denies_other_tools(auth_ctx, sample_png):
    with RunningServer(auth_ctx, _free_port()) as srv:
        async with Client(
            StreamableHttpTransport(srv.url, headers={"Authorization": "Bearer tok-img"})
        ) as client:
            ok = await client.call_tool("image_info", {"path": sample_png})
            assert not ok.is_error
            denied = await client.call_tool(
                "http_get", {"url": "http://example.com/"}, raise_on_error=False
            )
            assert denied.is_error
            assert "TOOL_NOT_PERMITTED" in str(denied)


async def test_rate_limit(auth_ctx, sample_png):
    with RunningServer(auth_ctx, _free_port()) as srv:
        async with Client(
            StreamableHttpTransport(srv.url, headers={"Authorization": "Bearer tok-full"})
        ) as client:
            outcomes = []
            for _ in range(6):  # limit is 5/min in this fixture
                outcomes.append(
                    await client.call_tool(
                        "image_info", {"path": sample_png}, raise_on_error=False
                    )
                )
            assert not outcomes[4].is_error
            assert outcomes[5].is_error
            assert "RATE_LIMITED" in str(outcomes[5])


async def test_metrics_endpoint(auth_ctx, sample_png):
    with RunningServer(auth_ctx, _free_port()) as srv:
        import httpx

        async with Client(
            StreamableHttpTransport(srv.url, headers={"Authorization": "Bearer tok-full"})
        ) as client:
            await client.call_tool("image_info", {"path": sample_png})
        body = httpx.get(f"http://127.0.0.1:{srv.port}/metrics").text
        assert 'tool_calls_total{tool="image_info"}' in body
        assert "tool_call_duration_ms_bucket" in body


async def test_catalog_parity_stdio_http(auth_ctx, tmp_env):
    """AC-P3-04: stdio and HTTP serve the identical catalog."""
    from pathlib import Path

    import core

    root = Path(core.__file__).resolve().parent.parent
    stdio_transport = PythonStdioTransport(
        root / "server" / "__main__.py",
        args=["serve", "--transport", "stdio"],
        env={
            "PROTOX_DATA_DIR": str(tmp_env["data_dir"] / "stdio"),
            "PROTOX_WORKSPACE": str(tmp_env["workspace"] / "stdio"),
            "PYTHONPATH": str(root),
        },
        cwd=str(root),
    )
    async with Client(stdio_transport) as client:
        stdio_tools = await client.list_tools()
    with RunningServer(auth_ctx, _free_port()) as srv:
        async with Client(
            StreamableHttpTransport(srv.url, headers={"Authorization": "Bearer tok-full"})
        ) as client:
            http_tools = await client.list_tools()
    stdio_dump = [(t.name, t.description, t.input_schema) for t in stdio_tools]
    http_dump = [(t.name, t.description, t.input_schema) for t in http_tools]
    assert stdio_dump == http_dump
