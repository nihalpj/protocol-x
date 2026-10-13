"""Soak test (AC-P11-03): sustained pooled dispatch shows no leaks or decay.

50 sequential + 20 concurrent calls through the warm pool: all succeed, the
pool never spawns more workers than configured, and worker RSS stays bounded
(no per-call leak). Marked slow; deselect with `-m "not slow"`.
"""

from __future__ import annotations

import threading

import pytest

from core.sandbox.pool import PooledRunner

CALLS = 50
CONCURRENT = 20


def _pool_max_worker_rss_mb(pool: PooledRunner) -> int:
    peak = 0
    for bucket in pool._buckets.values():
        for w in bucket:
            try:
                with open(f"/proc/{w.proc.pid}/status") as fh:
                    for line in fh:
                        if line.startswith("VmRSS"):
                            peak = max(peak, int(line.split()[1]) // 1024)
            except OSError:
                pass
    return peak


@pytest.mark.slow()
def test_soak_pooled_dispatch(ctx, sample_png):
    assert isinstance(ctx.runner, PooledRunner)
    pool: PooledRunner = ctx.runner

    for i in range(CALLS):
        out = ctx.dispatch("image_info", {"path": sample_png})
        assert out["result"]["data"]["width"] == 4000, f"call {i} failed"
    stats = pool.stats()
    assert stats["spawned_total"] <= pool.max_workers
    rss_after_warm = _pool_max_worker_rss_mb(pool)

    errors: list = []

    def call():
        try:
            ctx.dispatch("image_info", {"path": sample_png})
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=call) for _ in range(CONCURRENT)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    assert pool.stats()["spawned_total"] <= pool.max_workers
    # no per-call leak: the busiest worker's RSS must stay flat under load
    assert _pool_max_worker_rss_mb(pool) < rss_after_warm + 50
