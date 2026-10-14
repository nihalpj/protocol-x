"""Result serialization (F-005, INV-3).

JSON-serializable results inline; binaries as base64+MIME; results >1 MB
auto-persisted to the workspace as resource refs; hard cap 20 MB.
"""

from __future__ import annotations

import base64
import json
from typing import Any

from adapters.base import RESULT_TOO_LARGE, BinaryResult, ToolError
from core.workspace import Workspace

INLINE_CAP_BYTES = 1 * 1024 * 1024  # >1 MB -> persist as resource ref
HARD_CAP_BYTES = 20 * 1024 * 1024  # >20 MB -> RESULT_TOO_LARGE

BYTES_TAG = "__protox_bytes_b64__"


def encode_bytes_args(value: Any) -> Any:
    """Make validated call args JSON-safe: bytes become single-key tag dicts.

    The dispatcher validates against the tool's input model, which may coerce
    strings to `bytes`; plain `json.dumps` cannot carry those to the sandbox
    worker. Tagging preserves arbitrary (non-UTF-8) binary losslessly.
    """
    if isinstance(value, (bytes, bytearray)):
        return {BYTES_TAG: base64.b64encode(bytes(value)).decode()}
    if isinstance(value, dict):
        return {k: encode_bytes_args(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [encode_bytes_args(v) for v in value]
    return value


def decode_bytes_args(value: Any) -> Any:
    """Worker-side inverse of encode_bytes_args: tag dicts become bytes again."""
    if isinstance(value, dict):
        if set(value) == {BYTES_TAG}:
            return base64.b64decode(value[BYTES_TAG])
        return {k: decode_bytes_args(v) for k, v in value.items()}
    if isinstance(value, list):
        return [decode_bytes_args(v) for v in value]
    return value


def _json_size(value: Any) -> int:
    return len(json.dumps(value, default=str).encode())


def normalize_result(result: Any, workspace: Workspace) -> dict:
    """Normalize a worker-returned payload (already JSON-decoded) to the wire contract."""
    if isinstance(result, dict) and "__binary_persisted__" in result:
        # P10 streaming persistence: the worker already wrote the bytes to the
        # workspace (cap enforced worker-side) — just pass the resource ref on.
        resource = result.get("resource")
        if not isinstance(resource, dict) or "uri" not in resource:
            raise ToolError(RESULT_TOO_LARGE, "invalid persisted binary payload")
        return {"persisted": True, "resource": resource}
    if isinstance(result, dict) and "__binary__" in result:
        raw = base64.b64decode(result["data_b64"])
        if len(raw) > HARD_CAP_BYTES:
            raise ToolError(
                RESULT_TOO_LARGE,
                "binary result exceeds 20 MB hard cap",
                {"size_bytes": len(raw), "cap_bytes": HARD_CAP_BYTES},
            )
        if len(raw) > INLINE_CAP_BYTES:
            out = workspace.new_output_path(result.get("suggested_name") or "output.bin")
            out.write_bytes(raw)
            return {
                "persisted": True,
                "resource": workspace.resource_ref(out, result.get("mime")),
            }
        return {
            "data_b64": result["data_b64"],
            "mime": result.get("mime", "application/octet-stream"),
            "suggested_name": result.get("suggested_name", "output.bin"),
        }

    size = _json_size(result)
    if size > HARD_CAP_BYTES:
        raise ToolError(
            RESULT_TOO_LARGE,
            "result exceeds 20 MB hard cap",
            {"size_bytes": size, "cap_bytes": HARD_CAP_BYTES},
        )
    if size > INLINE_CAP_BYTES:
        out = workspace.new_output_path("result.json")
        out.write_text(json.dumps(result, default=str))
        return {"persisted": True, "resource": workspace.resource_ref(out, "application/json")}
    return {"data": result}


def binary_to_wire(value: BinaryResult) -> dict:
    """Worker-side: encode a BinaryResult for transport back to the server."""
    return {
        "__binary__": True,
        "data_b64": base64.b64encode(value.data).decode(),
        "mime": value.mime,
        "suggested_name": value.suggested_name,
    }
