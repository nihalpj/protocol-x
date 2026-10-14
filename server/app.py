"""MCP application (F-001/F-013): FastMCP instance, dynamic tool registration,
stdio + Streamable HTTP wiring, health/metrics routes.
"""

from __future__ import annotations

import asyncio
import contextlib
import threading
import time
from typing import TYPE_CHECKING, Any

from fastmcp import FastMCP
from fastmcp.tools.function_tool import FunctionTool
from pydantic import ValidationError as PydanticValidationError
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse

from adapters.base import ToolError
from core.audit import AuditLedger
from core.db import Store
from core.logging import JsonlLogger, Metrics, fmt_obj, get_logger, new_call_id
from core.sandbox.runner import CallRequest, SubprocessRunner
from core.secrets import SecretsBroker
from core.serialization import encode_bytes_args, normalize_result
from core.sizing import SizingProfile, profile_for_settings
from core.workspace import Workspace
from server.auth import BearerAuthMiddleware, TokenStore, current_token
from server.config import Settings
from server.errors import to_mcp_error
from server.ratelimit import RateLimiter
from server.registry import ToolRegistry

if TYPE_CHECKING:
    from core.sandbox.pool import PooledRunner

VERSION = "1.0.0"

log = get_logger("app")


class AppContext:
    """Everything the dispatcher needs, shared by transports and /api routes."""

    def __init__(self, settings: Settings):
        self.settings = settings
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        self.store = Store(settings.db_path)
        self.audit = AuditLedger(self.store.audit, settings.audit_jsonl)
        self.registry = ToolRegistry(self.store, settings.data_dir)
        self.registry.load()
        # F-040: semantic search index. Sync runs off the boot path so a slow
        # embedding endpoint cannot blow the cold-start budget (F-037); a
        # failed sync leaves the index dirty and search retries lazily.
        from agents.embeddings import select_provider
        from agents.search import ToolSearchIndex

        self.search = ToolSearchIndex(self.store, select_provider(settings))
        threading.Thread(
            target=self._sync_search_index, daemon=True, name="protox-search-sync"
        ).start()
        self.workspace = Workspace(settings.workspace_root)
        # F-038: compute the host-derived profile once; print + audit-log it.
        self.sizing: SizingProfile = profile_for_settings(settings)
        self.runner: SubprocessRunner | PooledRunner
        if settings.pool_enabled:
            from core.sandbox.pool import PooledRunner

            size = int(settings.worker_pool_override or self.sizing.worker_pool)
            self.runner = PooledRunner(max_workers=max(1, size))
        else:
            self.runner = SubprocessRunner()
        self.secrets = SecretsBroker(settings.secrets)
        # F-016: tool-call log scrubs registered secret values from payloads.
        self.logger = JsonlLogger(settings.log_jsonl, scrub=self.secrets.scrub_text)
        self.metrics = Metrics()
        self.token_store = TokenStore(settings.tokens, settings.tool_acl)
        self.rate_limiter = RateLimiter(settings.rate_limit_per_min)
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
        log.info(
            "context ready: preset=%s tools=%d pool=%s workspace=%s",
            settings.preset_name,
            len(self.registry.list_tools()),
            f"{self.runner.max_workers} workers" if hasattr(self.runner, "max_workers") else "off",
            self.workspace.root,
        )
        log.debug("sizing profile: %s", fmt_obj(self.sizing.to_dict()))

    def _sync_search_index(self) -> None:
        try:
            self.search.sync(self.registry.list_tools(include_non_live=True))
        except Exception:
            log.warning("background search sync failed", exc_info=True)

    def mark_search_dirty(self) -> None:
        """F-040: call after any registry mutation (merge/remove/clear-drafts)."""
        self.search.mark_dirty()

    def kick_search_sync(self) -> None:
        """F-040: mark dirty and re-embed in the background so newly merged
        tools land in tool_embeddings immediately (never blocks the merge)."""
        self.search.mark_dirty()
        threading.Thread(
            target=self._sync_search_index, daemon=True, name="protox-search-sync"
        ).start()

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

    def rebuild_repairer(self) -> None:
        """Re-create the repair agent after an operator TF key rotation.

        RepairAgent snapshots the Token Factory key at construction
        (agents/repair_agent.py), so a console-side key change must rebuild
        it to hot-apply without a restart. A repairer that is absent stays
        absent (disabled-mode consistency)."""
        if self.repairer is None:
            return
        from agents.repair_agent import RepairAgent

        self.repairer = RepairAgent(self)

    def dispatch(self, tool_name: str, args: dict[str, Any]) -> dict:
        """Validate -> sandbox -> normalize -> log/metrics. Raises ToolError."""
        call_id = new_call_id()
        start = time.perf_counter()
        log.debug("dispatch %s call_id=%s args=%s", tool_name, call_id, fmt_obj(args))
        # F-016 payload capture: read live so the console toggle hot-applies,
        # and keep the caller-supplied args pre-injection (secrets never logged).
        payloads_on = self.settings.log_payloads
        request_args = args
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
                args=encode_bytes_args(validated.model_dump()),
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
            if payloads_on:
                self.logger.log_call(
                    call_id,
                    tool_name,
                    duration,
                    "ok",
                    result_size=len(str(result)),
                    request=request_args,
                    response=result,
                )
            else:
                self.logger.log_call(call_id, tool_name, duration, "ok", result_size=len(str(result)))
            self.metrics.observe(tool_name, duration)
            log.info("tool %s ok in %.1fms (call_id=%s)", tool_name, duration, call_id)
            log.debug("tool %s result: %s", tool_name, fmt_obj(result))
            return {"tool_call_id": call_id, "tool": tool_name, "result": result}
        except ToolError as exc:
            duration = (time.perf_counter() - start) * 1000
            if payloads_on:
                self.logger.log_call(
                    call_id,
                    tool_name,
                    duration,
                    "error",
                    exit_code=1,
                    error_code=exc.code,
                    request=request_args,
                )
            else:
                self.logger.log_call(
                    call_id, tool_name, duration, "error", exit_code=1, error_code=exc.code
                )
            self.metrics.observe(tool_name, duration, exc.code)
            log.warning(
                "tool %s failed [%s] in %.1fms: %s", tool_name, exc.code, duration, exc.message
            )
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

    from server.operator_tools import register_operator_tools

    register_operator_tools(mcp, ctx)

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
