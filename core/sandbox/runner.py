"""SubprocessRunner (F-003, INV-2): one isolated worker process per tool call.

- spawn per call; call JSON on stdin; result/error JSON on stdout
- rlimits applied in the child (preexec); wall-clock timeout -> SIGKILL
- non-zero exit / unparseable output / OOM -> SANDBOX_CRASH
- scrubbed child environment (INV-7 seam; secrets are injected per call
  into tool args by the dispatcher, never via env)
"""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass
from typing import Any

from adapters.base import SANDBOX_CRASH, SANDBOX_TIMEOUT, ToolError
from core.sandbox.backends.python_worker import BACKEND as PYTHON_BACKEND
from core.sandbox.limits import FS_MB, RSS_MB, CPU_S, apply_limits

_BACKENDS = {"python": PYTHON_BACKEND}

# Minimal scrubbed worker environment (INV-7): workers are keyless by construction.
_SCRUBBED_ENV = {
    "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
    "PYTHONPATH": None,  # filled with the project root at spawn
    "PYTHONUNBUFFERED": "1",
    "PYTHONDONTWRITEBYTECODE": "1",
    "OPENBLAS_NUM_THREADS": "1",  # OpenBLAS thread pool cannot start under RLIMIT_NPROC=0
    "OMP_NUM_THREADS": "1",
    "HOME": os.environ.get("HOME", "/tmp"),
    "LANG": "C.UTF-8",
}


@dataclass
class CallRequest:
    adapter_module: str
    tool_name: str
    args: dict[str, Any]
    workspace: str
    timeout_s: int = 30
    network: bool = False
    backend: str = "python"
    cpu_s: int = CPU_S
    rss_mb: int = RSS_MB
    fs_mb: int = FS_MB
    # Operator-controlled per-call env additions (dispatcher-side only, F-033
    # seam): config values like ALLOWED_HOSTS — never agent input, never secrets.
    extra_env: dict[str, str] | None = None


def _project_root() -> str:
    import core

    return str(os.path.dirname(os.path.dirname(os.path.abspath(core.__file__))))


class SubprocessRunner:
    def __init__(self, project_root: str | None = None):
        self.project_root = project_root or _project_root()

    def worker_env(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        env = {k: v for k, v in _SCRUBBED_ENV.items() if v is not None}
        env["PYTHONPATH"] = self.project_root
        if extra:
            env.update(extra)
        return env

    def run(self, req: CallRequest) -> Any:
        backend = _BACKENDS.get(req.backend)
        if backend is None:
            raise ToolError(SANDBOX_CRASH, f"unknown worker backend: {req.backend}")
        payload = json.dumps(
            {
                "adapter_module": req.adapter_module,
                "tool_name": req.tool_name,
                "args": req.args,
                "workspace": req.workspace,
            }
        ).encode()
        try:
            proc = subprocess.Popen(
                backend.command(),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                env=self.worker_env(req.extra_env),
                cwd=self.project_root,
                preexec_fn=lambda: apply_limits(req.cpu_s, req.rss_mb, req.fs_mb),
            )
        except OSError as exc:
            raise ToolError(SANDBOX_CRASH, f"failed to spawn worker: {exc}") from exc
        try:
            stdout, _ = proc.communicate(payload, timeout=req.timeout_s)
        except subprocess.TimeoutExpired:
            proc.kill()  # SIGKILL; wall-clock budget (AC-F003-01)
            proc.wait()
            raise ToolError(
                SANDBOX_TIMEOUT,
                f"tool exceeded wall-clock timeout of {req.timeout_s}s",
                {"timeout_s": req.timeout_s},
            ) from None
        if proc.returncode != 0:
            raise ToolError(
                SANDBOX_CRASH,
                "sandbox worker died (crash or resource limit)",
                {"exit_code": proc.returncode},
            )
        try:
            doc = json.loads(stdout)
        except (json.JSONDecodeError, ValueError) as exc:
            raise ToolError(SANDBOX_CRASH, f"worker returned unparseable output: {exc}") from exc
        if not doc.get("ok"):
            err = doc.get("error") or {}
            raise ToolError(
                err.get("code", SANDBOX_CRASH),
                err.get("message", "tool failed"),
                err.get("details") or {},
            )
        return doc.get("result")
