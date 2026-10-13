"""P10 warm worker pool: correctness, timeout, crash recovery, limits, recycle.

AC-P10-01 pooled results identical to spawn mode · AC-P10-02 wall-clock
timeout kills and the pool recovers · AC-P10-03 OOM/crash -> SANDBOX_CRASH and
lazy respawn · AC-P10-04 per-bucket rlimits preserved (INV-2) ·
AC-P10-05 worker recycling after max_calls.
"""

from __future__ import annotations

import threading
import time

import pytest

from adapters.base import SANDBOX_CRASH, SANDBOX_TIMEOUT, ToolError
from core.sandbox.pool import PooledRunner
from core.sandbox.runner import CallRequest, SubprocessRunner

POOLTEST_ADAPTER = '''from __future__ import annotations

import time
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from adapters.base import ToolSpec


class EchoIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str = Field(description="text to echo")


class SleepIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    seconds: float = Field(description="seconds to sleep")


class BignumIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mb: int = Field(description="megabytes to allocate")


def pooltest_echo(ws, text: str) -> dict[str, Any]:
    return {"result": f"echo:{text}"}


def pooltest_sleep(ws, seconds: float) -> dict[str, Any]:
    time.sleep(seconds)
    return {"result": "slept"}


def pooltest_crash(ws) -> dict[str, Any]:
    import os

    os._exit(42)


def pooltest_bignum(ws, mb: int) -> dict[str, Any]:
    buf = bytearray(mb * 1024 * 1024)
    return {"result": len(buf)}


TOOLS = [
    ToolSpec(name="pooltest_echo", description="echo", input_model=EchoIn,
             handler=pooltest_echo, timeout_s=30, network=False),
    ToolSpec(name="pooltest_sleep", description="sleep", input_model=SleepIn,
             handler=pooltest_sleep, timeout_s=30, network=False),
    ToolSpec(name="pooltest_crash", description="crash", input_model=EchoIn,
             handler=lambda ws, text: pooltest_crash(ws), timeout_s=30, network=False),
    ToolSpec(name="pooltest_bignum", description="allocate", input_model=BignumIn,
             handler=pooltest_bignum, timeout_s=30, network=False),
]
'''


@pytest.fixture()
def adapters_dir(ctx):
    yield ctx.registry.adapters_dir
    (ctx.registry.adapters_dir / "pooltest.py").unlink(missing_ok=True)


@pytest.fixture()
def pooltest(adapters_dir):
    (adapters_dir / "pooltest.py").write_text(POOLTEST_ADAPTER)
    return adapters_dir


def _req(tool, args, workspace, rss_mb=256, timeout_s=5):
    return CallRequest(
        adapter_module="pooltest", tool_name=tool, args=args,
        workspace=str(workspace), timeout_s=timeout_s, rss_mb=rss_mb)


def test_pooled_matches_spawn(pooltest, settings):
    req = _req("pooltest_echo", {"text": "hi"}, settings.workspace_root)
    spawned = SubprocessRunner().run(req)
    pool = PooledRunner(max_workers=2)
    try:
        pooled = pool.run(req)
        assert pooled == spawned == {"result": "echo:hi"}
        # warm reuse: second call hits the same worker (no respawn)
        pool.run(req)
        assert pool.stats()["spawned_total"] == 1
    finally:
        pool.shutdown()


def test_timeout_kills_and_pool_recovers(pooltest, settings):
    pool = PooledRunner(max_workers=2)
    try:
        with pytest.raises(ToolError) as ei:
            pool.run(_req("pooltest_sleep", {"seconds": 5.0}, settings.workspace_root,
                          timeout_s=1))
        assert ei.value.code == SANDBOX_TIMEOUT
        # the dead worker was discarded; the next call respawns and works
        assert pool.run(_req("pooltest_echo", {"text": "x"}, settings.workspace_root)) == {
            "result": "echo:x"}
    finally:
        pool.shutdown()


def test_crash_maps_to_sandbox_crash_and_recovers(pooltest, settings):
    pool = PooledRunner(max_workers=2)
    try:
        with pytest.raises(ToolError) as ei:
            pool.run(_req("pooltest_crash", {"text": "boom"}, settings.workspace_root))
        assert ei.value.code == SANDBOX_CRASH
        assert pool.run(_req("pooltest_echo", {"text": "y"}, settings.workspace_root)) == {
            "result": "echo:y"}
    finally:
        pool.shutdown()


def test_bucket_rss_limit_still_enforced(pooltest, settings):
    """INV-2: a 96 MB bucket worker cannot allocate 300 MB (kernel-enforced)."""
    pool = PooledRunner(max_workers=2)
    try:
        with pytest.raises(ToolError) as ei:
            pool.run(_req("pooltest_bignum", {"mb": 300}, settings.workspace_root,
                          rss_mb=96, timeout_s=15))
        assert ei.value.code == SANDBOX_CRASH
    finally:
        pool.shutdown()


def test_concurrent_calls_use_distinct_workers(pooltest, settings):
    pool = PooledRunner(max_workers=2)
    results: list = []
    errors: list = []

    def call():
        try:
            results.append(pool.run(
                _req("pooltest_sleep", {"seconds": 0.6}, settings.workspace_root,
                     timeout_s=10)))
        except Exception as exc:
            errors.append(exc)

    start = time.perf_counter()
    threads = [threading.Thread(target=call) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    elapsed = time.perf_counter() - start
    try:
        assert not errors and len(results) == 2
        assert elapsed < 1.1  # serial execution would take >= 1.2 s
        assert pool.stats()["spawned_total"] == 2
    finally:
        pool.shutdown()


def test_worker_recycled_after_max_calls(pooltest, settings):
    pool = PooledRunner(max_workers=2, max_calls_per_worker=2)
    try:
        req = _req("pooltest_echo", {"text": "r"}, settings.workspace_root)
        for _ in range(3):
            pool.run(req)
        stats = pool.stats()
        assert stats["spawned_total"] == 2  # first worker recycled after 2 calls
        assert stats["live_workers"] == 1
    finally:
        pool.shutdown()


def test_dispatch_uses_pool_by_default(ctx, sample_png):
    from core.sandbox.pool import PooledRunner

    assert isinstance(ctx.runner, PooledRunner)
    out = ctx.dispatch("image_info", {"path": sample_png})
    assert out["result"]["data"]["width"] == 4000


def test_pool_disabled_by_env(tmp_env, monkeypatch):
    monkeypatch.setenv("PROTOX_POOL", "0")
    from server.app import AppContext
    from server.config import Settings

    context = AppContext(Settings())
    try:
        assert isinstance(context.runner, SubprocessRunner)
    finally:
        context.store.close()
