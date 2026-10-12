"""POSIX rlimit presets for sandbox workers (F-003)."""

from __future__ import annotations

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
        try:
            resource.setrlimit(resource.RLIMIT_NPROC, (0, 0))
        except (ValueError, OSError):
            pass  # fails for root / some platforms; best-effort
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
