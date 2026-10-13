"""POSIX rlimit presets for sandbox workers (F-003)."""

from __future__ import annotations

import contextlib

CPU_S = 25
RSS_MB = 512
FS_MB = 100


def apply_limits(
    cpu_s: int = CPU_S, rss_mb: int = RSS_MB, fs_mb: int = FS_MB, nproc_zero: bool = True
) -> None:
    """Apply resource limits. Called in the child (preexec) before exec."""
    import resource

    resource.setrlimit(resource.RLIMIT_CPU, (cpu_s, cpu_s + 5))
    rss = rss_mb * 1024 * 1024
    resource.setrlimit(resource.RLIMIT_AS, (rss, rss))
    fs = fs_mb * 1024 * 1024
    resource.setrlimit(resource.RLIMIT_FSIZE, (fs, fs))
    if nproc_zero:
        # fails for root / some platforms; best-effort
        with contextlib.suppress(ValueError, OSError):
            resource.setrlimit(resource.RLIMIT_NPROC, (0, 0))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))


def apply_pool_limits(rss_mb: int = RSS_MB, fs_mb: int = FS_MB) -> None:
    """Hard limits for a warm-pool worker (F-038/P10), bucketed by (rss, fs).

    RLIMIT_CPU is deliberately absent: it budgets *cumulative* process CPU,
    which a long-lived worker would exhaust across calls. CPU abuse in pooled
    mode is bounded by the parent's wall-clock timeout (SIGKILL) — spawn mode
    (PROTOX_POOL=0) keeps the strict per-call RLIMIT_CPU.
    """
    import resource

    rss = rss_mb * 1024 * 1024
    resource.setrlimit(resource.RLIMIT_AS, (rss, rss))
    fs = fs_mb * 1024 * 1024
    resource.setrlimit(resource.RLIMIT_FSIZE, (fs, fs))
    with contextlib.suppress(ValueError, OSError):
        resource.setrlimit(resource.RLIMIT_NPROC, (0, 0))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
