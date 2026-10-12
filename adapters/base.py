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


# Adapter modules expose exactly one of:
#   TOOLS: list[ToolSpec]
#   def get_tools() -> list[ToolSpec]
ADAPTER_ATTRS = ("get_tools", "TOOLS")
