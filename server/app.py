"""MCP application (F-001/F-013): FastMCP instance, dynamic tool registration,
stdio + Streamable HTTP wiring, health/metrics routes.
"""

from __future__ import annotations

import asyncio
import contextlib
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
from core.secrets import SecretsBroker
from core.serialization import normalize_result
from core.sizing import SizingProfile, profile_for_settings
from core.workspace import Workspace
from server.auth import BearerAuthMiddleware, TokenStore, current_token
from server.config import Settings
from server.errors import to_mcp_error
from server.ratelimit import RateLimiter
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
        # F-038: compute the host-derived profile once; print + audit-log it.
        self.sizing: SizingProfile = profile_for_settings(settings)
        if settings.pool_enabled:
            from core.sandbox.pool import PooledRunner

            size = int(settings.worker_pool_override or self.sizing.worker_pool)
            self.runner = PooledRunner(max_workers=max(1, size))
        else:
            self.runner = SubprocessRunner()
        self.logger = JsonlLogger(settings.log_jsonl)
        self.metrics = Metrics()
        self.token_store = TokenStore(settings.tokens, settings.tool_acl)
        self.rate_limiter = RateLimiter(settings.rate_limit_per_min)
        self.secrets = SecretsBroker(settings.secrets)
        self._apply_policy_overrides()
        from agents.repair_agent import RepairAgent

        self.repairer = RepairAgent(self)  # F-030 runtime self-repair
        self.audit.record(
            "config",
            "server",
            {
                "event": "startup",
                "preset": settings.preset_name,
                "sizing": self.sizing.to_dict(),
            },
        )

    def _apply_policy_overrides(self) -> None:
        """F-032: preset per-tool overrides (e.g. strict disables http_*)."""
        disabled = [
            name
            for name, flags in self.settings.preset_tool_overrides.items()
            if isinstance(flags, dict) and flags.get("enabled") is False
        ]
        for name in disabled:
            if self.registry.catalog.pop(name, None) is not None:
                self.registry.store.tools.delete(name)

    def dispatch(self, tool_name: str, args: dict[str, Any]) -> dict:
        """Validate -> sandbox -> normalize -> log/metrics. Raises ToolError."""
        call_id = new_call_id()
        start = time.perf_counter()
        try:
            token = current_token.get()
            self.token_store.check_acl(token, tool_name)
            self.rate_limiter.check(token)
            spec = self.registry.get_spec(tool_name)
            args = self.secrets.inject(spec, args)  # F-033 dispatcher-side only
            try:
                validated = spec.input_model.model_validate(args)
            except PydanticValidationError as exc:
                raise ToolError(
                    "VALIDATION_ERROR",
                    "arguments failed schema validation",
                    {"issues": exc.errors()},
                ) from exc
            extra_env: dict[str, str] = {}
            if spec.network and self.settings.allowed_hosts:
                # operator egress allowlist injected per call (never agent-settable)
                extra_env["ALLOWED_HOSTS"] = ",".join(self.settings.allowed_hosts)
            req = CallRequest(
                adapter_module=spec_adapter_module(self.registry, tool_name),
                tool_name=tool_name,
                args=validated.model_dump(),
                workspace=str(self.workspace.root),
                timeout_s=spec.timeout_s or self.settings.default_timeout_s,
                network=spec.network,
                backend=spec.backend,
                cpu_s=self.settings.limit_cpu_s,
                rss_mb=int(spec.limits.get("rss_mb", self.settings.limit_rss_mb)),
                fs_mb=self.settings.limit_fs_mb,
                extra_env=extra_env or None,
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
            with contextlib.suppress(Exception):
                # repair capture must never break dispatch
                self.repairer.maybe_capture(tool_name, exc, args)  # F-030
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

    from server.api import register_api_routes

    register_api_routes(mcp, ctx)

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


def build_http_app(ctx: AppContext):
    """ASGI app: MCP Streamable HTTP + custom routes behind bearer auth (F-013/14)."""
    mcp = create_mcp(ctx)
    return BearerAuthMiddleware(mcp.http_app(), ctx.token_store)


def serve_http(ctx: AppContext, host: str, port: int) -> None:
    import uvicorn

    uvicorn.run(build_http_app(ctx), host=host, port=port, log_level="warning")
