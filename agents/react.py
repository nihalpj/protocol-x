#!/usr/bin/env python3
"""
Protocol-X Tool MCP — ReAct plumbing (PRD F-029 · Implementation Plan P7)
=========================================================================

Shared Thought → Action → Observation machinery for the converter (F-029)
and repair (F-030) agents:

  * `Step`        — one ReAct triple (+ kind: "step" | "fix", timestamp)
  * `ReActTracer` — records triples, optionally streams them live, persists
                    them as JSONL for the audit log (AC-F029-05), and writes
                    pipeline checkpoints so an interrupted run can resume
                    from the last completed stage (E-13).

No model calls live here — this is the deterministic skeleton that the
agents hang their reasoning on. Streaming format mirrors the console's
ReAct pane (THOUGHT / ACTION / OBSERVE / AUTO-FIX).
"""
from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, cast

FIRST_LABEL = {"step": "THOUGHT", "fix": "AUTO-FIX"}


@dataclass
class Step:
    thought: str
    action: str
    observation: str
    kind: str = "step"                      # "step" | "fix" (auto-fix cycle)
    ts: float = field(default_factory=time.time)

    def as_json(self) -> dict[str, Any]:
        return asdict(self)


class ReActTracer:
    """Record / stream / persist / checkpoint a ReAct run."""

    def __init__(self,
                 trace_file: str | Path | None = None,
                 checkpoint_file: str | Path | None = None,
                 stream: bool = False):
        self.steps: list[Step] = []
        self.trace_file = Path(trace_file) if trace_file else None
        self.checkpoint_file = Path(checkpoint_file) if checkpoint_file else None
        self.stream = stream
        if self.trace_file:
            self.trace_file.parent.mkdir(parents=True, exist_ok=True)

    # ---------------------------------------------------------- recording
    def act(self, thought: str, action: str, fn: Callable[[], Any]) -> Any:
        """Run one ReAct triple: execute `fn`, record its result verbatim."""
        result = fn()
        self.record(thought, action, str(result))
        return result

    def record(self, thought: str, action: str, observation: str,
               kind: str = "step") -> Step:
        step = Step(thought=thought, action=action,
                    observation=observation, kind=kind)
        self.steps.append(step)
        if self.trace_file:
            with self.trace_file.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(step.as_json(), ensure_ascii=False) + "\n")
        if self.stream:
            self.print_step(step, len(self.steps))
        return step

    # -------------------------------------------------------- checkpointing
    def checkpoint(self, stage: str, payload: dict[str, Any] | None = None) -> None:
        """Mark `stage` complete; a --resume run skips up to this point (E-13)."""
        if self.checkpoint_file:
            self.checkpoint_file.write_text(json.dumps(
                {"stage": stage, "payload": payload or {}, "ts": time.time()},
                indent=2))

    def completed_stage(self) -> str | None:
        if self.checkpoint_file and self.checkpoint_file.exists():
            try:
                return cast("str | None", json.loads(self.checkpoint_file.read_text()).get("stage"))
            except (json.JSONDecodeError, OSError):
                return None
        return None

    # ------------------------------------------------------------ printing
    @staticmethod
    def print_step(step: Step, index: int) -> None:
        first = FIRST_LABEL.get(step.kind, "THOUGHT")
        print(f"[{index}] {first:<8} {step.thought}")
        print(f"    {'ACTION':<8} {step.action}")
        print(f"    {'OBSERVE':<8} {step.observation}")

    def print_all(self) -> None:
        print("\n===== ReAct trace =====")
        for i, step in enumerate(self.steps, 1):
            self.print_step(step, i)
