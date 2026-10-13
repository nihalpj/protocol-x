"""Known-VIOLATING adapter — eval injection. Must score blocked (AC-P6-02)."""
from pydantic import BaseModel, ConfigDict
from adapters.base import ToolSpec


class In(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expression: str  # free-form code string


def run(ws, expression: str) -> dict:
    return {"result": eval(expression)}  # noqa: S307 — intentional violation


TOOLS = [ToolSpec("adv_eval", "run any expression", In, run)]
