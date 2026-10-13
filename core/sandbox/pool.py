"""Warm worker pool (P10): reuse sandboxed worker processes across calls.

Spawn-per-call pays a full Python interpreter start (~100 ms) for every tool
call. The pool keeps long-lived `--serve` workers (one JSONL request in, one
response out) and hands them out per call:

- buckets: workers are keyed by (rss_mb, fs_mb) so RLIMIT_AS / RLIMIT_FSIZE
  stay exactly as strict as spawn mode — each worker's hard limits match its
  bucket (INV-2). RLIMIT_CPU is replaced by the parent's wall-clock timeout
  (SIGKILL), because CPU rlimits are cumulative and a warm worker would
  exhaust them across calls (documented in limits.apply_pool_limits).
- fairness: one in-flight call per worker; extra calls wait on a condition
  until a worker frees or the pool can grow (max_workers from the F-038
  sizing profile).
- hygiene: a worker is recycled after max_calls_per_worker calls (leak
  guard); a dead worker (timeout, OOM exit 42, crash) is discarded and
  lazily respawned on the next call.
- PROTOX_POOL=0 disables pooling entirely (strict spawn-per-call mode).

The runner protocol is unchanged — CallRequest in, result or ToolError out.
"""

from __future__ import annotations

import atexit
import json
import select
import subprocess
import sys
import threading
from dataclasses import dataclass
from typing import Any

from adapters.base import SANDBOX_CRASH, SANDBOX_TIMEOUT, ToolError
from core.sandbox.limits import apply_pool_limits
from core.sandbox.runner import CallRequest, SubprocessRunner


@dataclass
class _Worker:
    proc: subprocess.Popen
    calls: int = 0
    busy: bool = False


class PooledRunner:
    """Drop-in replacement for SubprocessRunner backed by warm workers."""

    def __init__(
        self,
        project_root: str | None = None,
        max_workers: int = 2,
        max_calls_per_worker: int = 200,
    ):
        self._spawner = SubprocessRunner(project_root)
        self.project_root = self._spawner.project_root
        self.max_workers = max(1, max_workers)
        self.max_calls_per_worker = max(1, max_calls_per_worker)
        self._buckets: dict[tuple[int, int], list[_Worker]] = {}
        self._cond = threading.Condition()
        self._spawned_total = 0
        self._closed = False
        atexit.register(self.shutdown)

    # ------------------------------------------------------------ public
    def run(self, req: CallRequest) -> Any:
        if req.backend != "python":
            return self._spawner.run(req)  # foreign backends stay spawn-per-call
        key = (req.rss_mb, req.fs_mb)
        worker = self._acquire(key)
        try:
            return self._call(worker, req)
        finally:
            self._release(key, worker)

    def worker_env(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        return self._spawner.worker_env(extra)

    def shutdown(self) -> None:
        with self._cond:
            if self._closed:
                return
            self._closed = True
            workers = [w for bucket in self._buckets.values() for w in bucket]
            self._buckets.clear()
            self._cond.notify_all()
        for w in workers:
            with _suppress_all():
                w.proc.stdin.close()
            with _suppress_all():
                w.proc.terminate()
                w.proc.wait(timeout=2)

    def stats(self) -> dict[str, int]:
        with self._cond:
            return {
                "spawned_total": self._spawned_total,
                "live_workers": sum(len(b) for b in self._buckets.values()),
            }

    # ------------------------------------------------------- acquire/release
    def _acquire(self, key: tuple[int, int]) -> _Worker:
        with self._cond:
            while True:
                if self._closed:
                    raise ToolError(SANDBOX_CRASH, "worker pool is shut down")
                bucket = self._buckets.setdefault(key, [])
                for w in bucket:
                    if not w.busy and w.proc.poll() is None:
                        w.busy = True
                        return w
                total = sum(len(b) for b in self._buckets.values())
                if total < self.max_workers:
                    w = self._spawn(key)
                    bucket.append(w)
                    w.busy = True
                    self._spawned_total += 1
                    return w
                self._cond.wait(timeout=5.0)

    def _release(self, key: tuple[int, int], worker: _Worker) -> None:
        with self._cond:
            worker.busy = False
            if (worker.proc.poll() is not None
                    or worker.calls >= self.max_calls_per_worker):
                # dead or due for recycling — drop it; respawn is lazy
                with _suppress_all():
                    worker.proc.kill()
                if worker in self._buckets.get(key, []):
                    self._buckets[key].remove(worker)
            self._cond.notify()

    # ------------------------------------------------------------ plumbing
    def _spawn(self, key: tuple[int, int]) -> _Worker:
        rss_mb, fs_mb = key
        try:
            proc = subprocess.Popen(
                [sys.executable, "-m", "core.sandbox.worker_main", "--serve"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                env=self._spawner.worker_env(),
                cwd=self.project_root,
                preexec_fn=lambda: apply_pool_limits(rss_mb, fs_mb),
            )
        except OSError as exc:
            raise ToolError(SANDBOX_CRASH, f"failed to spawn pool worker: {exc}") from exc
        return _Worker(proc=proc)

    def _call(self, worker: _Worker, req: CallRequest) -> Any:
        payload = json.dumps(
            {
                "adapter_module": req.adapter_module,
                "tool_name": req.tool_name,
                "args": req.args,
                "workspace": req.workspace,
                "extra_env": req.extra_env,
            }
        ).encode() + b"\n"
        try:
            worker.proc.stdin.write(payload)
            worker.proc.stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            raise ToolError(
                SANDBOX_CRASH, "pool worker died before the call",
                {"exit_code": worker.proc.poll()}) from exc

        ready, _, _ = select.select([worker.proc.stdout], [], [], req.timeout_s)
        if not ready:
            worker.proc.kill()  # wall-clock budget (AC-F003-01)
            worker.proc.wait()
            raise ToolError(
                SANDBOX_TIMEOUT,
                f"tool exceeded wall-clock timeout of {req.timeout_s}s",
                {"timeout_s": req.timeout_s},
            )
        line = worker.proc.stdout.readline()
        worker.calls += 1
        if not line:
            raise ToolError(
                SANDBOX_CRASH,
                "sandbox worker died (crash or resource limit)",
                {"exit_code": worker.proc.poll()},
            )
        try:
            doc = json.loads(line)
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


class _suppress_all:
    def __enter__(self):
        return self

    def __exit__(self, *_):
        return True
