"""Structured JSON logging (F-016): one line per tool call, self-hosted only.

Also home of the app's human-readable logging setup: `setup_logging()`
configures stdlib logging (level from PROTOX_LOG_LEVEL / server.yaml /
--log-level), `get_logger()` hands out `protox.<module>` loggers, and
`fmt_obj()` safely prints arbitrary objects (capped repr) into log lines.

Opt-in payload capture (F-016): `JsonlLogger(scrub=...)` +
`log_call(request=..., response=...)` append scrubbed, length-capped
request/response fields to the JSONL line; the console toggle gates it at
dispatch time and it never raises.
"""

from __future__ import annotations

import json
import logging
import sys
import threading
import time
import uuid
from collections import defaultdict
from collections.abc import Callable
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

_LOCK = threading.Lock()

LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")

# F-016 payload capture: serialized request/response fields share the same
# 4000-char budget the repair agent uses for captured inputs.
PAYLOAD_MAX_CHARS = 4000


def payload_field(obj: Any, scrub: Callable[[str], str] | None) -> str | None:
    """Serialize a payload for the JSONL line: dumps -> scrub -> capped length.

    Observability must never break dispatch: any serialization/scrub failure
    degrades to None (the field is omitted), matching the logging path's
    never-raise discipline.
    """
    try:
        text = json.dumps(obj, default=str)
        if scrub is not None:
            text = scrub(text)
        return text[:PAYLOAD_MAX_CHARS]
    except Exception:  # noqa: BLE001 - payload capture must never raise
        return None


def setup_logging(level: str = "INFO", log_file: str | Path | None = None) -> None:
    """Configure stdlib logging app-wide. Idempotent (force=True).

    Human-readable lines go to stderr (captured by scripts/start.sh into
    server.log) and, when log_file is given, to a rotating file under
    <data_dir>/logs/.
    """
    resolved = level.upper()
    if resolved not in LOG_LEVELS:
        resolved = "INFO"
    lvl = getattr(logging, resolved)
    fmt = "%(asctime)s %(levelname)-5s [%(name)s] %(message)s"
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    if log_file:
        path = Path(log_file)
        path.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(RotatingFileHandler(path, maxBytes=5_000_000, backupCount=3))
    logging.basicConfig(level=lvl, format=fmt, handlers=handlers, force=True)
    if lvl > logging.DEBUG:
        # keep chatty libraries quiet unless debugging
        for noisy in ("httpx", "httpcore", "uvicorn.access", "watchfiles"):
            logging.getLogger(noisy).setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    """App logger named protox.<name> (levels: debug/info/warning/error)."""
    return logging.getLogger(f"protox.{name}")


def fmt_obj(value: Any, limit: int = 2000) -> str:
    """Length-capped repr for printing objects in log lines — never raises."""
    try:
        text = repr(value)
    except Exception as exc:  # noqa: BLE001 - logging must not fail
        text = f"<repr failed: {type(exc).__name__}>"
    return text if len(text) <= limit else f"{text[:limit]}…(+{len(text) - limit} chars)"


def new_call_id() -> str:
    return uuid.uuid4().hex


class JsonlLogger:
    """JSON-lines tool-call log: tool_call_id, tool, duration_ms, exit, size.

    Optional payload capture (F-016): when `scrub` is provided it redacts
    registered secret values (SecretsBroker.scrub_text) from serialized
    request/response fields; `log_call(request=..., response=...)` then adds
    them to the line. Serialization never raises — failures omit the field.
    """

    def __init__(
        self,
        path: str | Path | None = None,
        also_stderr: bool = True,
        scrub: Callable[[str], str] | None = None,
    ):
        self.path = Path(path) if path else None
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self.also_stderr = also_stderr
        self.scrub = scrub
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
        request: Any = None,
        response: Any = None,
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
        if request is not None:
            request_text = payload_field(request, self.scrub)
            if request_text is not None:
                line["request"] = request_text
        if response is not None:
            response_text = payload_field(response, self.scrub)
            if response_text is not None:
                line["response"] = response_text
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
