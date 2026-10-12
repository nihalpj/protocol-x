"""MCP application (F-001/F-013): FastMCP instance, dynamic tool registration,
stdio + Streamable HTTP wiring, health/metrics routes.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from fastmcp import FastMCP
from fastmcp.tools.function_tool import FunctionTool
from pydantic import ValidationError as PydanticValidationError
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse

from adapters.base import ToolError
from core.audit import AuditLedger
from core.db import Store
from core.logging import JsonlLogger, Metrics, new_call_id
from core.sandbox.runner import CallRequest, SubprocessRunner
from core.serialization import normalize_result
from core.workspace import Workspace
from server.config import Settings
from server.errors import to_mcp_error
from server.registry import ToolRegistry

VERSION = "1.0.0"


class AppContext:
    """Everything the dispatcher needs, shared by transports and /api routes."""

    def __init__(self, settings: Settings):
        self.settings = settings
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        self.store = Store(settings.db_path)
        self.audit = AuditLedger(self.store.audit, settings.audit_jsonl)
        self.registry = ToolRegistry(self.store, settings.data_dir)
        self.registry.load()
        self.workspace = Workspace(settings.workspace_root)
        self.runner = SubprocessRunner()
        self.logger = JsonlLogger(settings.log_jsonl)
        self.metrics = Metrics()

    def dispatch(self, tool_name: str, args: dict[str, Any]) -> dict:
        """Validate -> sandbox -> normalize -> log/metrics. Raises ToolError."""
        call_id = new_call_id()
        start = time.perf_counter()
        try:
            spec = self.registry.get_spec(tool_name)
            try:
                validated = spec.input_model.model_validate(args)
            except PydanticValidationError as exc:
                raise ToolError(
                    "VALIDATION_ERROR",
                    "arguments failed schema validation",
                    {"issues": exc.errors()},
                ) from exc
            req = CallRequest(
                adapter_module=spec_adapter_module(self.registry, tool_name),
                tool_name=tool_name,
                args=validated.model_dump(),
                workspace=str(self.workspace.root),
                timeout_s=spec.timeout_s or self.settings.default_timeout_s,
                network=spec.network,
                backend=spec.backend,
                cpu_s=self.settings.limit_cpu_s,
                rss_mb=self.settings.limit_rss_mb,
                fs_mb=self.settings.limit_fs_mb,
            )
            raw = self.runner.run(req)
            result = normalize_result(raw, self.workspace)
            duration = (time.perf_counter() - start) * 1000
            self.logger.log_call(
                call_id, tool_name, duration, "ok", result_size=len(str(result))
            )
            self.metrics.observe(tool_name, duration)
            return {"tool_call_id": call_id, "tool": tool_name, "result": result}
        except ToolError as exc:
            duration = (time.perf_counter() - start) * 1000
            self.logger.log_call(
                call_id, tool_name, duration, "error", exit_code=1, error_code=exc.code
            )
            self.metrics.observe(tool_name, duration, exc.code)
            raise


def spec_adapter_module(registry: ToolRegistry, tool_name: str) -> str:
    entry = registry.catalog.get(tool_name)
    return entry.adapter_module if entry else tool_name


def create_mcp(ctx: AppContext) -> FastMCP:
    mcp = FastMCP("protocol-x-tool-mcp", version=VERSION)

    for entry in ctx.registry.list_tools():
        mcp.add_tool(_make_tool(ctx, entry))

    @mcp.custom_route("/health", methods=["GET"])
    async def health(_request: Request) -> JSONResponse:
        return JSONResponse(
            {
                "status": "ok",
                "version": VERSION,
                "tools": len(ctx.registry.list_tools()),
            }
        )

    @mcp.custom_route("/metrics", methods=["GET"])
    async def metrics(_request: Request) -> PlainTextResponse:
        return PlainTextResponse(ctx.metrics.render_prometheus())

    return mcp


def _make_tool(ctx: AppContext, entry) -> FunctionTool:
    async def fn(**kwargs) -> dict:
        try:
            return await asyncio.to_thread(ctx.dispatch, entry.name, kwargs)
        except Exception as exc:
            raise to_mcp_error(exc) from exc

    return FunctionTool(
        name=entry.name,
        description=entry.description,
        parameters=entry.schema,
        fn=fn,
    )


def serve_stdio(ctx: AppContext) -> None:
    create_mcp(ctx).run(transport="stdio", show_banner=False)


def serve_http(ctx: AppContext, host: str, port: int) -> None:
    create_mcp(ctx).run(transport="http", host=host, port=port, show_banner=False)
