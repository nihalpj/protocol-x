"""Error mapping (INV-4): adapter/validation/sandbox failures -> MCP errors.

Clients receive isError=true whose message is a JSON ToolError document
{code,message,details}; tracebacks stay in the server logs.
"""

from __future__ import annotations

import json

from fastmcp.exceptions import ToolError as McpToolError
from pydantic import ValidationError as PydanticValidationError

from adapters.base import ADAPTER_ERROR, VALIDATION_ERROR, ToolError


def to_mcp_error(exc: BaseException) -> McpToolError:
    if isinstance(exc, ToolError):
        doc = exc.to_dict()
    elif isinstance(exc, PydanticValidationError):
        doc = ToolError(
            VALIDATION_ERROR, "arguments failed schema validation", {"issues": exc.errors()}
        ).to_dict()
    else:
        doc = ToolError(ADAPTER_ERROR, f"{type(exc).__name__}: {exc}").to_dict()
    return McpToolError(json.dumps(doc))
