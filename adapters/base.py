"""Adapter contract: ToolSpec, ToolError, BinaryResult.

Bottom layer of the dependency rule (server/agents -> core -> adapters):
everything may import this module; it imports nothing from server/ or core/.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel


class ToolError(Exception):
    """Structured tool failure (INV-4). Tracebacks never leave the server."""

    def __init__(self, code: str, message: str, details: dict[str, Any] | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, "details": self.details}


# Canonical error codes (F-006 vocabulary)
VALIDATION_ERROR = "VALIDATION_ERROR"
UNKNOWN_TOOL = "UNKNOWN_TOOL"
SANDBOX_TIMEOUT = "SANDBOX_TIMEOUT"
SANDBOX_CRASH = "SANDBOX_CRASH"
ADAPTER_ERROR = "ADAPTER_ERROR"
RESULT_TOO_LARGE = "RESULT_TOO_LARGE"
INPUT_TOO_LARGE = "INPUT_TOO_LARGE"
RANGE_TOO_LARGE = "RANGE_TOO_LARGE"
CORRUPT_FILE = "CORRUPT_FILE"
SSRF_BLOCKED = "SSRF_BLOCKED"
PDF_AUTH_FAILED = "PDF_AUTH_FAILED"
TOOL_NOT_PERMITTED = "TOOL_NOT_PERMITTED"
TOOL_DISABLED = "TOOL_DISABLED"
RATE_LIMITED = "RATE_LIMITED"


@dataclass
class BinaryResult:
    """Binary tool output; serialized as base64+MIME (INV-3)."""

    data: bytes
    mime: str
    suggested_name: str


@dataclass
class ToolSpec:
    name: str  # "image_resize" — unique, snake_case
    description: str  # agent-facing, includes limits & units
    input_model: type[BaseModel]  # Pydantic v2 -> JSON Schema
    handler: Callable[..., Any]  # runs INSIDE the sandbox worker
    timeout_s: int = 30
    network: bool = False
    backend: str = "python"  # "python" | "node" | "go-bin" | "rust-bin" (F-036)
    # Declarative size guards surfaced in the description (F-020)
    limits: dict[str, Any] = field(default_factory=dict)
    # Credential needs (F-033): param name -> server-side secret name; the
    # dispatcher injects values per call — agents never see them in schema.
    secret_params: dict[str, str] = field(default_factory=dict)


# Adapter modules expose exactly one of:
#   TOOLS: list[ToolSpec]
#   def get_tools() -> list[ToolSpec]
ADAPTER_ATTRS = ("get_tools", "TOOLS")


def normalize_value(value: Any) -> Any:
    """Make a wrapped function's return value JSON-native and stable.

    bytes decode as UTF-8 when possible (agents read text), otherwise they are
    base64-tagged; tuples/sets/frozensets become lists; namedtuples and
    dataclasses become dicts. Anything else keeps a type label and a repr with
    the memory address stripped so output is stable between calls.
    """
    import base64
    import dataclasses
    import re

    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, (bytes, bytearray)):
        raw = bytes(value)
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError:
            return {"_bytes_b64": base64.b64encode(raw).decode()}
    if isinstance(value, dict):
        return {k: normalize_value(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [normalize_value(v) for v in value]
    if hasattr(value, "_asdict"):  # namedtuple
        return normalize_value(value._asdict())
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return normalize_value(dataclasses.asdict(value))
    return {
        "_type": f"{type(value).__module__}.{type(value).__qualname__}",
        "_repr": re.sub(r" at 0x[0-9a-fA-F]+", "", str(value)),
    }


def wrap_tool_result(result: Any) -> dict[str, Any]:
    """Uniform handler return contract for generated adapters.

    HTTP-response-like objects (httpx/requests) keep their dedicated shape,
    with the body capped so a huge page cannot blow up the tool result.
    """
    if hasattr(result, "status_code") and hasattr(result, "text"):
        return {
            "result": {
                "status_code": result.status_code,
                "headers": dict(result.headers),
                "text": result.text[:65536],
            }
        }
    return {"result": normalize_value(result)}
