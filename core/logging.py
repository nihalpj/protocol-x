"""Structured JSON logging (F-016): one line per tool call, self-hosted only."""

from __future__ import annotations

import json
import logging
import sys
import threading
import time
import uuid
from collections import defaultdict
from pathlib import Path
from typing import Any

_LOCK = threading.Lock()


def new_call_id() -> str:
    return uuid.uuid4().hex


class JsonlLogger:
    """JSON-lines tool-call log: tool_call_id, tool, duration_ms, exit, size."""

    def __init__(self, path: str | Path | None = None, also_stderr: bool = True):
        self.path = Path(path) if path else None
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self.also_stderr = also_stderr
        self._pylogger = logging.getLogger("protox")

    def log_call(
        self,
        tool_call_id: str,
        tool: str,
        duration_ms: float,
        outcome: str,
        exit_code: int = 0,
        result_size: int = 0,
        error_code: str | None = None,
        extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        line: dict[str, Any] = {
            "ts": time.time(),
            "tool_call_id": tool_call_id,
            "tool": tool,
            "duration_ms": round(duration_ms, 2),
            "outcome": outcome,
            "exit_code": exit_code,
            "result_size": result_size,
        }
        if error_code:
            line["error_code"] = error_code
        if extra:
            line.update(extra)
        encoded = json.dumps(line, default=str)
        with _LOCK:
            if self.path:
                with self.path.open("a") as fh:
                    fh.write(encoded + "\n")
            if self.also_stderr:
                sys.stderr.write(encoded + "\n")
        return line


class Metrics:
    """In-process counters backing GET /metrics (Prometheus text format)."""

    def __init__(self):
        self._lock = threading.Lock()
        self.calls_total: dict[str, int] = defaultdict(int)
        self.errors_total: dict[str, int] = defaultdict(int)
        self.durations_ms: dict[str, list[float]] = defaultdict(list)

    def observe(self, tool: str, duration_ms: float, error_code: str | None = None) -> None:
        with self._lock:
            self.calls_total[tool] += 1
            self.durations_ms[tool].append(duration_ms)
            if len(self.durations_ms[tool]) > 1024:
                self.durations_ms[tool] = self.durations_ms[tool][-1024:]
            if error_code:
                self.errors_total[error_code] += 1

    def render_prometheus(self) -> str:
        lines = ["# HELP tool_calls_total Total tool calls", "# TYPE tool_calls_total counter"]
        with self._lock:
            for tool, n in sorted(self.calls_total.items()):
                lines.append(f'tool_calls_total{{tool="{tool}"}} {n}')
            lines += [
                "# HELP tool_errors_total Tool errors by code",
                "# TYPE tool_errors_total counter",
            ]
            for code, n in sorted(self.errors_total.items()):
                lines.append(f'tool_errors_total{{code="{code}"}} {n}')
            lines += [
                "# HELP tool_call_duration_ms Tool call duration histogram (ms)",
                "# TYPE tool_call_duration_ms histogram",
            ]
            buckets = (50, 100, 250, 500, 1000, 2500, 5000, 10000, 30000)
            for tool, ds in sorted(self.durations_ms.items()):
                for b in buckets:
                    count = sum(1 for d in ds if d <= b)
                    lines.append(f'tool_call_duration_ms_bucket{{tool="{tool}",le="{b}"}} {count}')
                lines.append(f'tool_call_duration_ms_bucket{{tool="{tool}",le="+Inf"}} {len(ds)}')
                lines.append(f'tool_call_duration_ms_sum{{tool="{tool}"}} {sum(ds):.2f}')
                lines.append(f'tool_call_duration_ms_count{{tool="{tool}"}} {len(ds)}')
        return "\n".join(lines) + "\n"
