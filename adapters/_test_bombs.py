"""Test-only chaos adapters (not registered: underscore-prefixed, skipped by
registry discovery). The worker imports this module directly in sandbox tests."""

from __future__ import annotations

import time

from pydantic import BaseModel, ConfigDict

from adapters.base import ToolSpec


class _Empty(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SleepIn(_Empty):
    seconds: float


def sleeper(ws, seconds: float) -> dict:
    time.sleep(seconds)
    return {"slept": seconds}


def memory_hog(ws) -> dict:
    blob = bytearray()
    while True:
        blob.extend(b"\x00" * (256 * 1024 * 1024))


def crasher(ws) -> dict:
    import os

    os._exit(3)


TOOLS = [
    ToolSpec("test_sleeper", "sleep N seconds (chaos test)", SleepIn, sleeper, timeout_s=120),
    ToolSpec("test_memory_hog", "allocate until OOM (chaos test)", _Empty, memory_hog),
    ToolSpec("test_crasher", "exit non-zero (chaos test)", _Empty, crasher),
]
