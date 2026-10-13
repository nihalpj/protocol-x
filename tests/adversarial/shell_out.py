"""Known-VIOLATING adapter — os.system shell-out."""
import os
from pydantic import BaseModel, ConfigDict
from adapters.base import ToolSpec


class In(BaseModel):
    model_config = ConfigDict(extra="forbid")
    host: str


def run(ws, host: str) -> dict:
    os.system(f"ping -c1 {host}")  # noqa: S605 — intentional violation
    return {"ok": True}


TOOLS = [ToolSpec("adv_shell", "ping a host", In, run)]
