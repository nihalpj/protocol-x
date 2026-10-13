"""Known-VIOLATING adapter — input model without extra="forbid"."""
from pydantic import BaseModel
from adapters.base import ToolSpec


class In(BaseModel):
    path: str


def run(ws, path: str) -> dict:
    return {"path": path}


TOOLS = [ToolSpec("adv_open_schema", "missing additionalProperties:false", In, run)]
