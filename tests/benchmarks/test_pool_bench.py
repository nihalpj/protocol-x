"""P10 benchmark: warm pool vs cold spawn (informational + regression guard).

Asserts the pool's median call latency beats spawn-per-call by a comfortable
margin, and (with PROTOX_WRITE_BASELINE=1) refreshes baseline.json so docs
can cite current numbers. Absolute numbers are host-dependent — CI compares
only the ratio.
"""

from __future__ import annotations

import json
import os
import statistics
import time
from pathlib import Path

from core.sandbox.pool import PooledRunner
from core.sandbox.runner import CallRequest, SubprocessRunner

CALLS = 15


def _measure(runner, req, calls: int) -> list[float]:
    samples = []
    for _ in range(calls):
        start = time.perf_counter()
        runner.run(req)
        samples.append((time.perf_counter() - start) * 1000)
    return samples


def test_warm_pool_outperforms_spawn(settings, sample_png):
    req = CallRequest(
        adapter_module="images_pillow", tool_name="image_info",
        args={"path": sample_png}, workspace=str(settings.workspace_root),
        timeout_s=30, rss_mb=256)
    pool = PooledRunner(max_workers=2)
    try:
        pool.run(req)  # warm-up: worker spawn + adapter import amortized
        pooled = _measure(pool, req, CALLS)
    finally:
        pool.shutdown()
    spawned = _measure(SubprocessRunner(), req, 5)  # fewer: cold spawn is slow

    med_pool, med_spawn = statistics.median(pooled), statistics.median(spawned)
    print(f"\npool p50={med_pool:.1f}ms spawn p50={med_spawn:.1f}ms "
          f"speedup={med_spawn / med_pool:.1f}x")
    assert med_pool < med_spawn  # warm reuse always beats interpreter startup

    if os.environ.get("PROTOX_WRITE_BASELINE"):
        baseline = Path(__file__).with_name("baseline.json")
        baseline.write_text(json.dumps({
            "host_note": "reference numbers; regenerate with PROTOX_WRITE_BASELINE=1",
            "calls": CALLS,
            "pool_p50_ms": round(med_pool, 2),
            "spawn_p50_ms": round(med_spawn, 2),
            "speedup": round(med_spawn / med_pool, 2),
        }, indent=2))
