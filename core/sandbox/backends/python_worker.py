"""Python worker backend (F-036 seam): default execution runtime.

A backend knows how to build the spawn command for a worker process. The
runner passes the call JSON on stdin regardless of backend, so node/go/rust
backends (v1.1+) plug in without changing the runner protocol.
"""

from __future__ import annotations

import sys


class PythonWorkerBackend:
    name = "python"

    def command(self) -> list[str]:
        return [sys.executable, "-m", "core.sandbox.worker_main"]

    def network_forbidden(self, tool_network: bool) -> bool:
        return not tool_network


BACKEND = PythonWorkerBackend()
