"""In-worker entry point (F-003): run tool calls, write results.

Two modes:
  one-shot  — read one call on stdin (whole document), run, exit.
              Protocol: {"adapter_module", "tool_name", "args", "workspace"} in,
              {"ok": true, "result": <json>} | {"ok": false, "error": {...}} out.
  --serve   — warm-pool mode (P10): newline-delimited JSON loop, one request
              line in, one response line out, until stdin closes.

Binary streaming persistence (P10): a BinaryResult over the 1 MB inline cap is
written straight to the workspace inside the worker and returned as a
`__binary_persisted__` resource ref — no base64 round-trip through the pipe.
The 20 MB hard cap is enforced worker-side before any write (RLIMIT_FSIZE is
the backstop).

The worker imports adapter modules only (plus core.* helpers to build the
workspace object handed to the handler). Any unexpected exception becomes
ADAPTER_ERROR; the traceback stays here (it dies with the worker) — INV-4.
"""

from __future__ import annotations

import importlib
import json
import sys

from adapters.base import ADAPTER_ATTRS, ADAPTER_ERROR, UNKNOWN_TOOL, BinaryResult, ToolError
from core.workspace import Workspace


def _dotted(module: str) -> str:
    return module if "." in module else f"adapters.{module}"


def _find_tool(module_name: str, tool_name: str):
    module = importlib.import_module(_dotted(module_name))
    specs = None
    for attr in ADAPTER_ATTRS:
        candidate = getattr(module, attr, None)
        if candidate is None:
            continue
        specs = candidate() if callable(candidate) else candidate
        break
    if specs is None:
        raise ToolError(UNKNOWN_TOOL, f"adapter {module_name} exposes no tools")
    for spec in specs:
        if spec.name == tool_name:
            return spec
    raise ToolError(UNKNOWN_TOOL, f"tool {tool_name} not in {module_name}")


def _wire_result(result, ws: Workspace):
    """Encode a handler result for the wire; stream large binaries to disk."""
    if not isinstance(result, BinaryResult):
        return result
    from adapters.base import RESULT_TOO_LARGE
    from core.serialization import (
        HARD_CAP_BYTES,
        INLINE_CAP_BYTES,
        binary_to_wire,
    )

    data = result.data
    if len(data) > HARD_CAP_BYTES:
        raise ToolError(
            RESULT_TOO_LARGE,
            "binary result exceeds 20 MB hard cap",
            {"size_bytes": len(data), "cap_bytes": HARD_CAP_BYTES},
        )
    if len(data) > INLINE_CAP_BYTES:
        out = ws.new_output_path(result.suggested_name or "output.bin")
        out.write_bytes(data)  # streaming persistence: no base64 pipe tax
        return {"__binary_persisted__": True, "resource": ws.resource_ref(out, result.mime)}
    return binary_to_wire(result)


def _execute(call: dict) -> dict:
    from core.serialization import decode_bytes_args

    spec = _find_tool(call["adapter_module"], call["tool_name"])
    ws = Workspace(call["workspace"])
    # Per-call operator env (ALLOWED_HOSTS etc.) applies only for this call,
    # then the scrubbed baseline is restored (INV-7).
    import os

    extra = {k: str(v) for k, v in (call.get("extra_env") or {}).items()}
    previous = {k: os.environ.get(k) for k in extra}
    os.environ.update(extra)
    try:
        result = spec.handler(ws, **decode_bytes_args(call["args"]))
    finally:
        for k, old in previous.items():
            if old is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = old
    return {"ok": True, "result": _wire_result(result, ws)}


def _error_payload(exc: Exception) -> dict:
    if isinstance(exc, ToolError):
        return {"ok": False, "error": exc.to_dict()}
    # traceback stays in the worker/server logs (INV-4)
    return {
        "ok": False,
        "error": ToolError(
            ADAPTER_ERROR, f"{type(exc).__name__}: {exc}", {"exception": type(exc).__name__}
        ).to_dict(),
    }


def main() -> int:
    raw = sys.stdin.buffer.read()
    try:
        out = _execute(json.loads(raw))
    except MemoryError:
        # Die silently: the runner maps the non-zero exit to SANDBOX_CRASH (AC-P1-05).
        return 42
    except Exception as exc:
        out = _error_payload(exc)
    sys.stdout.write(json.dumps(out, default=str))
    sys.stdout.flush()
    return 0


def serve() -> int:
    """Warm-pool loop: one JSON request per stdin line, one per stdout line."""
    for line in sys.stdin.buffer:
        line = line.strip()
        if not line:
            continue
        try:
            out = _execute(json.loads(line))
        except MemoryError:
            return 42  # pool maps the dead worker to SANDBOX_CRASH
        except Exception as exc:
            out = _error_payload(exc)
        sys.stdout.write(json.dumps(out, default=str) + "\n")
        sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(serve() if "--serve" in sys.argv else main())
