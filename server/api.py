"""Console + operator REST API (F-017/F-018/F-023): /api/* routes and static
serving of the built console SPA. All state comes from core/db.py DAOs, so the
console and server never disagree across restarts.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import yaml
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from core.logging import fmt_obj, get_logger
from server.app import VERSION, AppContext

log = get_logger("api")

CONSOLE_DIST = Path(__file__).resolve().parent.parent / "console" / "dist"


def register_api_routes(mcp, ctx: AppContext) -> None:
    @mcp.custom_route("/api/status", methods=["GET"])
    async def api_status(_req: Request) -> JSONResponse:
        return JSONResponse(
            {
                "status": "ok",
                "version": VERSION,
                "tools": len(ctx.registry.list_tools()),
                "preset": ctx.settings.preset_name,
                "sizing": ctx.sizing.to_dict(),
                "agent_features_enabled": ctx.settings.agent_features_enabled,
                "judge_model": ctx.settings.tf_judge_model,
                "fast_model": ctx.settings.tf_fast_model,
                "tf_endpoint": ctx.settings.tf_endpoint,
                "workspace": str(ctx.workspace.root),
                "host": ctx.settings.host,
                "port": ctx.settings.port,
            }
        )

    @mcp.custom_route("/api/tools", methods=["GET"])
    async def api_tools(_req: Request) -> JSONResponse:
        rows = []
        versions = {v["adapter_module"]: v for v in ctx.store.versions.list()}
        for entry in ctx.registry.list_tools(include_non_live=True):
            version = versions.get(entry.adapter_module)
            rows.append(
                {
                    "name": entry.name,
                    "description": entry.description,
                    "schema": entry.schema,
                    "adapter_module": entry.adapter_module,
                    "backend": entry.backend,
                    "network": entry.network,
                    "timeout_s": entry.timeout_s,
                    "limits": entry.limits,
                    "state": entry.state,
                    "version_hash": version["version_hash"][:12] if version else None,
                    "source": version["source"] if version else None,
                }
            )
        return JSONResponse({"tools": rows})

    @mcp.custom_route("/api/logs", methods=["GET"])
    async def api_logs(req: Request) -> JSONResponse:
        limit = int(req.query_params.get("limit", "200"))
        lines: list[dict] = []
        path = ctx.settings.log_jsonl
        if path.exists():
            raw = path.read_text().strip().splitlines()[-limit:]
            lines = [json.loads(line) for line in raw if line.strip()]
        return JSONResponse({"logs": lines})

    @mcp.custom_route("/api/config", methods=["GET"])
    async def api_config_get(_req: Request) -> JSONResponse:
        s = ctx.settings
        return JSONResponse(
            {
                "preset": {"value": s.preset_name, "source": "env/config"},
                "limits": {
                    "cpu_s": {"value": s.limit_cpu_s, "source": "preset+override"},
                    "rss_mb": {"value": s.limit_rss_mb, "source": "preset+override"},
                    "fs_mb": {"value": s.limit_fs_mb, "source": "preset+override"},
                },
                "network": {"value": s.preset_network, "source": "preset"},
                "allowed_hosts": {"value": s.allowed_hosts, "source": "env/config"},
                "rate_limit_per_min": {"value": s.rate_limit_per_min, "source": "env/config"},
                "models": {
                    "endpoint": {"value": s.tf_endpoint, "source": "env/config"},
                    "judge": {"value": s.tf_judge_model, "source": "env/config"},
                    "fast": {"value": s.tf_fast_model, "source": "env/config"},
                },
                "agent_features_enabled": s.agent_features_enabled,
                "log_payloads": s.log_payloads,
                "api_key_set": bool(s.tf_api_key),
                "sizing_overrides": ctx.sizing.overrides,
            }
        )

    @mcp.custom_route("/api/config", methods=["POST"])
    async def api_config_post(req: Request) -> JSONResponse:
        body = await req.json()
        allowed = {
            "tf_endpoint", "tf_judge_model", "tf_fast_model", "preset",
            "log_payloads", "tf_api_key",
        }
        updates = {k: v for k, v in body.items() if k in allowed}
        if not updates:
            return JSONResponse({"error": "no recognized keys", "allowed": sorted(allowed)}, status_code=400)
        if "log_payloads" in updates and not isinstance(updates["log_payloads"], bool):
            return JSONResponse({"error": "log_payloads must be a boolean"}, status_code=400)
        if "tf_api_key" in updates:
            if not isinstance(updates["tf_api_key"], str):
                return JSONResponse({"error": "tf_api_key must be a string"}, status_code=400)
            if not updates["tf_api_key"]:
                updates.pop("tf_api_key")  # empty string = keep the existing key
        s = ctx.settings
        if "tf_endpoint" in updates:
            s.tf_endpoint = str(updates["tf_endpoint"])
        if "tf_judge_model" in updates:
            s.tf_judge_model = str(updates["tf_judge_model"])
        if "tf_fast_model" in updates:
            s.tf_fast_model = str(updates["tf_fast_model"])
        if "log_payloads" in updates:
            s.log_payloads = updates["log_payloads"]
        key_rotated = False
        if "tf_api_key" in updates:
            s.tf_api_key = updates["tf_api_key"]
            key_rotated = True
            ctx.rebuild_repairer()  # RepairAgent snapshots the key at construction
        before = {"preset": s.preset_name, **updates}
        if key_rotated:
            # the key value itself never enters the audit trail (INV-7)
            before["tf_api_key"] = "***updated***"
        # persist under data_dir; env still wins on restart (documented).
        # Merge into the existing file so wizard-written keys (api_key,
        # tokens, workspace) survive a console settings update.
        cfg_path = s.data_dir / "server.yaml"
        existing: dict = {}
        if cfg_path.exists():
            try:
                existing = yaml.safe_load(cfg_path.read_text()) or {}
            except yaml.YAMLError:
                existing = {}
        models = dict(existing.get("models") or {})
        models.update({
            "endpoint": s.tf_endpoint, "judge": s.tf_judge_model, "fast": s.tf_fast_model,
        })
        if key_rotated:
            # write-only: persisted once at rotation, never echoed back
            models["api_key"] = s.tf_api_key
        existing["models"] = models
        logging_cfg = dict(existing.get("logging") or {})
        logging_cfg["log_payloads"] = s.log_payloads
        existing["logging"] = logging_cfg
        if updates:  # a body of only {"tf_api_key": ""} is a recognized no-op
            cfg_path.write_text(yaml.safe_dump(existing, sort_keys=False))
            ctx.audit.record("config", "operator", {"event": "settings-update", "changes": before})
        return JSONResponse({"ok": True, "applied": sorted(updates), "persisted_to": str(cfg_path)})

    @mcp.custom_route("/api/governance", methods=["GET"])
    async def api_governance(_req: Request) -> JSONResponse:
        tools = ctx.registry.list_tools(include_non_live=True)
        verdicts = ctx.store.audit.query(kind="verdict", limit=50)
        rejections = ctx.store.audit.query(kind="rejection", limit=50)
        runs = ctx.store.runs.query(limit=50)
        incidents = ctx.store.incidents.list(limit=50)
        return JSONResponse(
            {
                "drafts": [
                    {"name": e.name, "module": e.adapter_module}
                    for e in tools
                    if e.state == "draft"
                ],
                "quarantined": [e.name for e in tools if e.state == "quarantined"],
                "verdicts": verdicts,
                "rejections": rejections,
                "conversion_runs": runs,
                "repair_incidents": incidents,
                "agent_features_enabled": ctx.settings.agent_features_enabled,
            }
        )

    @mcp.custom_route("/api/repair/incidents", methods=["GET"])
    async def api_repair_incidents(req: Request) -> JSONResponse:
        tool = req.query_params.get("tool") or None
        state = req.query_params.get("state") or None
        return JSONResponse(
            {"incidents": ctx.store.incidents.list(tool=tool, state=state)})

    @mcp.custom_route("/api/repair/reenable", methods=["POST"])
    async def api_repair_reenable(req: Request) -> JSONResponse:
        body = await req.json()
        tool = body.get("tool")
        if not tool or ctx.registry.entry_state(tool) is None:
            return JSONResponse({"error": f"unknown tool: {tool}"}, status_code=404)
        return JSONResponse(ctx.repairer.reenable(tool))

    @mcp.custom_route("/api/pypi/{name}", methods=["GET"])
    async def api_pypi(req: Request) -> JSONResponse:
        name = req.path_params["name"]
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.get(f"https://pypi.org/pypi/{name}/json")
        except httpx.HTTPError:
            return JSONResponse(
                {"state": "offline", "message": "PyPI is unreachable — check the network and retry"},
                status_code=502,
            )
        if resp.status_code == 404:
            return JSONResponse(
                {"state": "not-found", "message": f"package '{name}' was not found on PyPI"},
                status_code=404,
            )
        info = resp.json()["info"]
        return JSONResponse(
            {
                "state": "ok",
                "name": info.get("name"),
                "version": info.get("version"),
                "summary": info.get("summary"),
                "license": info.get("license") or info.get("license_expression"),
                "requires_python": info.get("requires_python"),
                "home_page": info.get("home_page") or info.get("project_url"),
            }
        )

    @mcp.custom_route("/api/snippets", methods=["GET"])
    async def api_snippets(_req: Request) -> JSONResponse:
        s = ctx.settings
        return JSONResponse(
            {
                "claude_desktop": {
                    "mcpServers": {
                        "protocol-x": {
                            "command": "uv",
                            "args": ["run", "protocol-x-tool-mcp", "serve", "--transport", "stdio"],
                        }
                    }
                },
                "generic_http": {
                    "url": f"http://{s.host}:{s.port}/mcp",
                    "transport": "streamable-http",
                    "headers": {"Authorization": "Bearer <token>"} if s.tokens else {},
                },
            }
        )

    # -- agent builder (F-019, UF-01) --------------------------------------

    @mcp.custom_route("/api/introspect/{package}", methods=["GET"])
    async def api_introspect(req: Request) -> JSONResponse:
        from agents.codegen import IntrospectionError, introspect_package

        package = req.path_params["package"]
        try:
            candidates = introspect_package(package)
        except IntrospectionError as exc:
            return JSONResponse({"state": "not-installed", "message": str(exc)}, status_code=400)
        return JSONResponse(
            {
                "package": package,
                "functions": [
                    {"name": c.name, "signature": c.signature, "doc": c.doc} for c in candidates
                ],
            }
        )

    @mcp.custom_route("/api/builder/install", methods=["POST"])
    async def api_builder_install(req: Request) -> JSONResponse:
        """Install a PyPI package into the server environment (E-3 resolution)."""
        import asyncio

        from server.installer import InstallError, install_package

        try:
            body = await req.json()
        except Exception:
            return JSONResponse({"state": "error", "message": "invalid JSON body"}, status_code=400)
        package = str(body.get("package", ""))
        log.info("operator requested install of package %s", package)
        try:
            result = await asyncio.to_thread(install_package, package)
        except InstallError as exc:
            log.warning("install of %s failed: %s", package, exc)
            return JSONResponse(
                {"state": "failed", "message": str(exc), "detail": exc.detail},
                status_code=400,
            )
        ctx.audit.record(
            "install",
            "operator",
            {
                "package": result.name,
                "version": result.version,
                "command": result.command,
                "already_installed": result.already_installed,
            },
        )
        return JSONResponse(
            {
                "state": "installed",
                "package": result.name,
                "version": result.version,
                "already_installed": result.already_installed,
            }
        )

    @mcp.custom_route("/api/builder/generate", methods=["POST"])
    async def api_generate(req: Request) -> JSONResponse:
        from agents.codegen import Builder, IntrospectionError

        body = await req.json()
        log.info(
            "operator requested adapter generation: package=%s functions=%s",
            body.get("package"), body.get("functions") or [],
        )
        log.debug("generate request body: %s", fmt_obj(body))
        try:
            result = Builder(ctx).generate(
                package=body["package"],
                selected=list(body.get("functions") or []),
                agent_name=body.get("agent_name"),
                timeout_s=int(body.get("timeout_s", 30)),
                network=bool(body.get("network", False)),
            )
        except (IntrospectionError, KeyError) as exc:
            log.warning("generation failed for %s: %s", body.get("package"), exc)
            return JSONResponse({"state": "error", "message": str(exc)}, status_code=400)
        log.info("generation result for %s: %s", body.get("package"), result.get("state"))
        return JSONResponse(result)

    @mcp.custom_route("/api/builder/judge", methods=["POST"])
    async def api_judge(req: Request) -> JSONResponse:
        from agents.codegen import Builder

        body = await req.json()
        log.info("operator requested judge+merge for module %s", body.get("module"))
        result = Builder(ctx).judge_and_merge(str(body["module"]))
        log.info("judge+merge for %s: %s", body.get("module"), result.state)
        return JSONResponse(
            {
                "state": result.state,
                "tools": result.tools,
                "diagnostics": result.diagnostics,
                "verdict": result.verdict.to_dict() if result.verdict else None,
            }
        )

    @mcp.custom_route("/api/adapter/{module}/source", methods=["GET", "PUT"])
    async def api_adapter_source(req: Request) -> JSONResponse:
        module = req.path_params["module"]
        path = (ctx.registry.adapters_dir / f"{module}.py").resolve()
        if not str(path).startswith(str(ctx.registry.adapters_dir.resolve())):
            return JSONResponse({"error": "forbidden"}, status_code=403)
        if req.method == "GET":
            if not path.exists():
                return JSONResponse({"error": "no such adapter"}, status_code=404)
            versions = ctx.store.versions.list(module)
            return JSONResponse(
                {
                    "module": module,
                    "source": path.read_text(),
                    "source_kind": versions[-1]["source"] if versions else None,
                }
            )
        from agents.codegen import Builder

        body = await req.json()
        result = Builder(ctx).save_manual_edit(module, str(body["source"]))
        return JSONResponse(
            {
                "state": result.state,
                "diagnostics": result.diagnostics,
                "verdict": result.verdict.to_dict() if result.verdict else None,
            }
        )

    @mcp.custom_route("/api/tool-format/{module}", methods=["GET"])
    async def api_tool_format(req: Request) -> JSONResponse:
        from agents.codegen import render_tool_format

        module = req.path_params["module"]
        tools = [
            {"name": e.name, "description": e.description, "input_schema": e.schema}
            for e in ctx.registry.list_tools(include_non_live=True)
            if e.adapter_module == module
        ]
        if not tools:
            return JSONResponse({"error": "no tools for module"}, status_code=404)
        return JSONResponse(
            {"module": module, "format": render_tool_format(tools, str(ctx.workspace.root))}
        )

    @mcp.custom_route("/{path:path}", methods=["GET"])
    async def console_spa(req: Request) -> Response:
        """Serve the built console SPA (console/dist); fallback to index.html."""
        rel = req.path_params["path"] or "index.html"
        candidate = (CONSOLE_DIST / rel).resolve()
        if not str(candidate).startswith(str(CONSOLE_DIST.resolve())):
            return JSONResponse({"error": "forbidden"}, status_code=403)
        if not candidate.is_file():
            candidate = CONSOLE_DIST / "index.html"
        if not candidate.is_file():
            return JSONResponse(
                {"error": "console not built", "hint": "cd console && npm ci && npm run build"},
                status_code=404,
            )
        content_type = {
            ".html": "text/html",
            ".js": "text/javascript",
            ".css": "text/css",
            ".svg": "image/svg+xml",
            ".png": "image/png",
            ".woff2": "font/woff2",
            ".json": "application/json",
        }.get(candidate.suffix, "application/octet-stream")
        return Response(candidate.read_bytes(), media_type=content_type)
