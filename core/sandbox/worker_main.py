"""In-worker entry point (F-003): read one call on stdin, run, write result.

Protocol (newline-free JSON documents):
  stdin:  {"adapter_module", "tool_name", "args", "workspace"}
  stdout: {"ok": true, "result": <json>} | {"ok": false, "error": {code,message,details}}

The worker imports adapter modules only (plus core.workspace to build the
confinement object handed to the handler). Any unexpected exception becomes
ADAPTER_ERROR; the traceback stays here (it dies with the worker) — INV-4.
"""

from __future__ import annotations

import importlib
import json
import sys

from adapters.base import ADAPTER_ATTRS, ADAPTER_ERROR, UNKNOWN_TOOL, BinaryResult, ToolError
from core.serialization import binary_to_wire
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


def main() -> int:
    raw = sys.stdin.buffer.read()
    try:
        call = json.loads(raw)
        spec = _find_tool(call["adapter_module"], call["tool_name"])
        ws = Workspace(call["workspace"])
        result = spec.handler(ws, **call["args"])
        if isinstance(result, BinaryResult):
            payload = binary_to_wire(result)
        else:
            payload = result
        out = {"ok": True, "result": payload}
    except ToolError as exc:
        out = {"ok": False, "error": exc.to_dict()}
    except MemoryError:
        # Die silently: the runner maps the non-zero exit to SANDBOX_CRASH (AC-P1-05).
        return 42
    except Exception as exc:  # traceback stays in the worker/server logs (INV-4)
        out = {
            "ok": False,
            "error": ToolError(
                ADAPTER_ERROR, f"{type(exc).__name__}: {exc}", {"exception": type(exc).__name__}
            ).to_dict(),
        }
    sys.stdout.write(json.dumps(out, default=str))
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
