"""Operator tools over MCP: PyPI install + autonomous package conversion.

Exposes the operator surfaces that previously existed only on the REST
console (/api/builder/*) and the CLI (`protocol-x-tool-mcp convert`) as
first-class MCP tools, so an MCP client can go from "I need package X" to
live sandboxed tools without leaving the protocol:

    package_install -> package_introspect -> package_convert
    -> package_convert_status (poll) -> newly live tools, hot-registered

`package_convert_bulk` converts up to 10 packages in one background batch
(sequential; per-package failure isolation), polled via
`package_convert_status(batch_id=...)`.

These are server-layer orchestration tools, NOT sandboxed adapter tools:
like the REST console, they run in the server process (the converter writes
adapters/ and calls the judge LLM). Bearer auth at the transport is the
access boundary; handlers still enforce the per-token ACL and rate limit
so they behave like every other tool under dispatch guardrails.
"""

from __future__ import annotations

import asyncio
import re
import threading
from collections.abc import Callable
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from fastmcp.tools.function_tool import FunctionTool

from adapters.base import VALIDATION_ERROR, ToolError
from core.logging import get_logger
from server.auth import current_token
from server.errors import to_mcp_error
from server.installer import InstallError, install_package, installed_version

if TYPE_CHECKING:
    from fastmcp import FastMCP

    from server.app import AppContext

log = get_logger("operator_tools")

_MAX_FUNCTIONS_IN_RESPONSE = 50
_RUN_OPEN_TIMEOUT_S = 15

# run_id -> {"state": "running"|"finished"|"error", "exit_code": int|None,
#            "detail": str} — in-process view complementing the DB row.
_runs: dict[int, dict[str, Any]] = {}
_runs_lock = threading.Lock()

# batch_id -> {"state": "running"|"finished", "packages": [...],
#              "runs": {package: run_id | None}, "errors": {package: str}}
_batches: dict[str, dict[str, Any]] = {}

MAX_BATCH_PACKAGES = 10


def _guard(ctx: AppContext, tool_name: str) -> None:
    """Same ACL + rate-limit guardrails as dispatch (server/app.py)."""
    token = current_token.get()
    ctx.token_store.check_acl(token, tool_name)
    ctx.rate_limiter.check(token)


def _module_name(package: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", package.lower()).strip("_")


def _importable(package: str) -> bool:
    """True when the package (or stdlib module) can already be imported —
    no download needed."""
    import importlib.util

    try:
        return importlib.util.find_spec(package) is not None
    except (ImportError, ValueError):
        return False


def _set_run(run_id: int, **fields: Any) -> None:
    with _runs_lock:
        _runs.setdefault(run_id, {}).update(fields)


def _hot_register(mcp: FastMCP, ctx: AppContext, module: str,
                  known: set[str]) -> list[str]:
    """Register freshly merged tools on the live FastMCP instance so they are
    callable without a server restart (registry/DB were updated by the
    converter's merger already)."""
    from server.app import _make_tool

    added: list[str] = []
    for entry in ctx.registry.list_tools():
        if entry.adapter_module != module or entry.name in known:
            continue
        try:
            mcp.add_tool(_make_tool(ctx, entry))
        except Exception:
            log.exception("hot-registration of %s failed", entry.name)
            continue
        known.add(entry.name)
        added.append(entry.name)
    if added:
        log.info("hot-registered %d tools for %s: %s", len(added), module, added)
    return added


def _convert_one(mcp: FastMCP, ctx: AppContext, package: str,
                 focus: str | None, known: set[str],
                 on_run_opened: Callable[[int], None]) -> int | None:
    """Run one conversion to completion (blocking). Returns the exit code
    (0 = merged, 2 = rejected/error), None when the run row never opened.
    Shared by the single-package and batch paths so they cannot diverge."""
    from server.convert_cli import run_convert

    box: dict[str, int] = {}

    def _on_opened(run_id: int) -> None:
        box["run_id"] = run_id
        _set_run(run_id, state="running", exit_code=None, detail="")
        on_run_opened(run_id)

    try:
        code = run_convert(package, ctx.settings, focus=focus, ctx=ctx,
                           on_run_opened=_on_opened)
    except Exception as exc:
        log.exception("conversion of %s crashed", package)
        run_id = box.get("run_id")
        if run_id is not None:
            _set_run(run_id, state="error", detail=f"{type(exc).__name__}: {exc}")
        return 2 if run_id is not None else None
    run_id = box.get("run_id")
    if run_id is None:
        return None
    _set_run(run_id, state="finished", exit_code=code)
    if code == 0:
        added = _hot_register(mcp, ctx, _module_name(package), known)
        _set_run(run_id, tools_registered=added)
    return code


def _run_conversion(mcp: FastMCP, ctx: AppContext, package: str,
                    focus: str | None, opened: threading.Event,
                    box: dict[str, int], known: set[str]) -> None:
    def _on_opened(run_id: int) -> None:
        box["run_id"] = run_id
        opened.set()

    try:
        _convert_one(mcp, ctx, package, focus, known, _on_opened)
    finally:
        opened.set()  # unblock the caller even on early failure


def _run_batch(mcp: FastMCP, ctx: AppContext, batch_id: str,
               packages: list[str], focus: str | None, known: set[str]) -> None:
    """Sequential batch: one package's failure never aborts the rest."""
    for package in packages:
        try:
            if not _importable(package):
                install_package(package)
        except InstallError as exc:
            log.warning("batch %s: install of %s failed: %s", batch_id, package, exc)
            with _runs_lock:
                _batches[batch_id]["errors"][package] = str(exc)
            continue
        except Exception as exc:  # never let one package kill the batch
            log.exception("batch %s: unexpected install failure for %s",
                          batch_id, package)
            with _runs_lock:
                _batches[batch_id]["errors"][package] = (
                    f"{type(exc).__name__}: {exc}")
            continue

        def _on_opened(run_id: int, _pkg: str = package) -> None:
            with _runs_lock:
                _batches[batch_id]["runs"][_pkg] = run_id

        _convert_one(mcp, ctx, package, focus, known, _on_opened)
    with _runs_lock:
        _batches[batch_id]["state"] = "finished"
    log.info("batch %s finished", batch_id)


def register_operator_tools(mcp: FastMCP, ctx: AppContext) -> None:
    """Attach the operator tools to the FastMCP instance (called by create_mcp)."""
    # Names already registered at startup; hot-registration skips these.
    known: set[str] = {e.name for e in ctx.registry.list_tools()}

    async def package_install(name: str) -> dict:
        """Download and install a PyPI package into the server environment so it
        can be introspected and converted into tools. Plain PEP 508 package
        names only (no version specifiers, extras or URLs). Installs are
        audited. Returns name, version and whether it was already installed."""
        try:
            _guard(ctx, "package_install")
            result = await asyncio.to_thread(install_package, name)
        except InstallError as exc:
            raise to_mcp_error(
                ToolError(VALIDATION_ERROR, str(exc), {"detail": exc.detail})
            ) from exc
        except Exception as exc:
            raise to_mcp_error(exc) from exc
        ctx.audit.record("install", "mcp-operator", {
            "package": result.name, "version": result.version,
            "command": result.command,
            "already_installed": result.already_installed,
        })
        return {
            "state": "installed",
            "name": result.name,
            "version": result.version,
            "already_installed": result.already_installed,
        }

    async def package_introspect(package: str) -> dict:
        """List the convertable functions of an installed package (name,
        signature, doc) — the candidates package_convert chooses its tool
        surface from. The package must be installed first (package_install)."""
        from agents.codegen import IntrospectionError, introspect_package

        try:
            _guard(ctx, "package_introspect")
            candidates = await asyncio.to_thread(introspect_package, package)
        except IntrospectionError as exc:
            raise to_mcp_error(
                ToolError(VALIDATION_ERROR, str(exc),
                          {"hint": "run package_install first"})
            ) from exc
        except Exception as exc:
            raise to_mcp_error(exc) from exc
        return {
            "package": package,
            "function_count": len(candidates),
            "functions": [
                {"name": c.name, "signature": c.signature, "doc": c.doc}
                for c in candidates[:_MAX_FUNCTIONS_IN_RESPONSE]
            ],
        }

    async def package_convert(package: str, focus: str | None = None) -> dict:
        """Install (if needed) and autonomously convert a PyPI package into
        sandboxed Protocol-X tools: introspect the installed package, propose a
        focused tool surface, codegen the adapter, pass the AI-judge gate, then
        auto-merge — merged tools are hot-registered and callable immediately.
        Runs in the background (LLM judge cycles take minutes); returns a
        run_id to poll with package_convert_status. `focus` optionally steers
        the tool surface (e.g. "only date parsing"). Without a Token Factory
        key the judge fails closed and the run ends rejected."""
        try:
            _guard(ctx, "package_convert")
            # Download only when required: stdlib/installed packages import
            # directly; anything missing comes from PyPI via the installer.
            if await asyncio.to_thread(_importable, package):
                install = None
            else:
                install = await asyncio.to_thread(install_package, package)
        except InstallError as exc:
            raise to_mcp_error(
                ToolError(VALIDATION_ERROR, str(exc), {"detail": exc.detail})
            ) from exc
        except Exception as exc:
            raise to_mcp_error(exc) from exc
        # The conversion_runs row opened by run_convert is the audit trail for
        # the run itself (the fixed audit-kind vocabulary has no "convert").
        opened = threading.Event()
        box: dict[str, int] = {}
        thread = threading.Thread(
            target=_run_conversion,
            args=(mcp, ctx, package, focus, opened, box, known),
            daemon=True,
        )
        thread.start()
        version = install.version if install else installed_version(package)
        if not await asyncio.to_thread(opened.wait, _RUN_OPEN_TIMEOUT_S):
            return {
                "state": "starting",
                "package": package,
                "installed_version": version,
                "note": "run row not opened yet; poll package_convert_status",
            }
        run_id = box["run_id"]
        row = ctx.store.runs.get(run_id) or {}
        return {
            "state": "started",
            "run_id": run_id,
            "package": package,
            "workdir": row.get("workdir"),
            "installed_version": version,
            "note": "poll package_convert_status with this run_id",
        }

    async def package_convert_bulk(packages: list[str],
                                   focus: str | None = None) -> dict:
        """Convert several packages in one call (max 10). Each package is
        installed if missing, then run through the same autonomous pipeline as
        package_convert — sequentially, in one background batch, so one
        package's failure never aborts the rest. Returns a batch_id
        immediately; poll package_convert_status with batch_id for per-package
        outcomes. Merged tools are hot-registered as each run completes."""
        try:
            _guard(ctx, "package_convert_bulk")
        except Exception as exc:
            raise to_mcp_error(exc) from exc
        if (not isinstance(packages, list) or not packages
                or len(packages) > MAX_BATCH_PACKAGES
                or any(not isinstance(p, str) or not p.strip() for p in packages)):
            raise to_mcp_error(ToolError(
                VALIDATION_ERROR,
                f"packages must be a list of 1-{MAX_BATCH_PACKAGES} "
                "non-empty package names"))
        packages = [p.strip() for p in packages]
        batch_id = uuid4().hex[:12]
        with _runs_lock:
            _batches[batch_id] = {
                "state": "running",
                "packages": list(packages),
                "runs": {p: None for p in packages},
                "errors": {},
            }
        threading.Thread(
            target=_run_batch,
            args=(mcp, ctx, batch_id, packages, focus, known),
            daemon=True,
        ).start()
        return {
            "state": "started",
            "batch_id": batch_id,
            "packages": packages,
            "note": "poll package_convert_status with this batch_id",
        }

    async def package_convert_status(run_id: int | None = None,
                                     batch_id: str | None = None) -> dict:
        """Report conversion status. With run_id: one run (DB row, live
        progress, tools on merge). With batch_id: per-package results for a
        package_convert_bulk batch. With neither: the 20 most recent runs.
        outcome is null while running, then merged | rejected | error."""
        try:
            _guard(ctx, "package_convert_status")
        except Exception as exc:
            raise to_mcp_error(exc) from exc
        if batch_id is not None:
            with _runs_lock:
                batch = dict(_batches.get(batch_id) or {})
            if not batch:
                raise to_mcp_error(
                    ToolError(VALIDATION_ERROR, f"unknown batch_id {batch_id}"))
            results = []
            for package in batch["packages"]:
                entry: dict[str, Any] = {
                    "package": package,
                    "run_id": batch["runs"].get(package),
                }
                if package in batch["errors"]:
                    entry["outcome"] = "error"
                    entry["reason_code"] = "install-failed"
                    entry["detail"] = batch["errors"][package]
                elif entry["run_id"] is not None:
                    run_row = ctx.store.runs.get(entry["run_id"]) or {}
                    entry["outcome"] = run_row.get("outcome")
                    entry["reason_code"] = run_row.get("reason_code")
                    if run_row.get("outcome") == "merged":
                        module = _module_name(package)
                        entry["tools_live"] = [
                            e.name for e in ctx.registry.list_tools()
                            if e.adapter_module == module
                        ]
                else:
                    entry["outcome"] = None  # queued
                results.append(entry)
            return {
                "batch_id": batch_id,
                "state": batch["state"],
                "results": results,
            }
        if run_id is None:
            return {"runs": ctx.store.runs.query(limit=20)}
        row = ctx.store.runs.get(run_id)
        if row is None:
            raise to_mcp_error(
                ToolError(VALIDATION_ERROR, f"unknown run_id {run_id}"))
        result: dict[str, Any] = {"run": row}
        with _runs_lock:
            live = dict(_runs.get(run_id) or {})
        if live:
            result["live"] = live
        ckpt = ctx.store.checkpoints.load(run_id)
        if ckpt:
            result["stage"] = ckpt["stage"]
        if row.get("outcome") == "merged":
            module = _module_name(str(row["package"]))
            result["tools_live"] = [
                e.name for e in ctx.registry.list_tools()
                if e.adapter_module == module
            ]
        return result

    schemas: dict[str, dict] = {
        "package_install": {
            "type": "object",
            "properties": {
                "name": {"type": "string",
                         "description": "PyPI package name (plain name only)"},
            },
            "required": ["name"],
        },
        "package_introspect": {
            "type": "object",
            "properties": {
                "package": {"type": "string",
                            "description": "installed package to introspect"},
            },
            "required": ["package"],
        },
        "package_convert": {
            "type": "object",
            "properties": {
                "package": {"type": "string",
                            "description": "PyPI package to install + convert"},
                "focus": {"type": "string",
                          "description": "optional steer for the tool surface"},
            },
            "required": ["package"],
        },
        "package_convert_bulk": {
            "type": "object",
            "properties": {
                "packages": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "1-10 PyPI package names to install + "
                                   "convert sequentially",
                },
                "focus": {"type": "string",
                          "description": "optional steer for the tool surface "
                                         "(applied to every package)"},
            },
            "required": ["packages"],
        },
        "package_convert_status": {
            "type": "object",
            "properties": {
                "run_id": {"type": "integer",
                           "description": "run id from package_convert"},
                "batch_id": {"type": "string",
                             "description": "batch id from "
                                            "package_convert_bulk"},
            },
        },
    }
    for name, fn in (
        ("package_install", package_install),
        ("package_introspect", package_introspect),
        ("package_convert", package_convert),
        ("package_convert_bulk", package_convert_bulk),
        ("package_convert_status", package_convert_status),
    ):
        mcp.add_tool(FunctionTool(
            name=name,
            description=(fn.__doc__ or "").strip(),
            parameters=schemas[name],
            fn=fn,
        ))
    log.debug("operator tools registered: %s", sorted(schemas))
