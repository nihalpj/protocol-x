"""Dynamic compute allocation (F-038): probe the host, compute the profile.

Formula (documented, tested against fixtures):
  tier:  small (<4 cores or <8 GB) | standard (4–16 cores, 8–32 GB) | large
  cpus        = max(1, cores − 1)           (reserve 2 when cores > 8)
  memory      = 60% of RAM
  worker_pool = min(cores, RAM ÷ (2 × per-call RSS)); floor of 2 (OQ-5)
  per-call RSS: 256 MB small / 512 MB standard / 1024 MB large

Undersized hosts (<2 cores or <4 GB) get the small tier plus an explicit
warning — never silent oversubscription (AC-F038-02). Env overrides
(PROTOX_CPUS / PROTOX_MEMORY / PROTOX_WORKER_POOL) win and are reported
as overrides (AC-F038-03).
"""

from __future__ import annotations

import os
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path


@dataclass
class SizingProfile:
    tier: str  # small | standard | large
    cpus: float
    memory_mb: int
    worker_pool: int
    per_call_rss_mb: int
    warnings: list[str] = field(default_factory=list)
    overrides: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


def _read_cgroup_v2(base: Path) -> tuple[int | None, int | None]:
    """Return (cpu_quota_cores, memory_max_bytes) from a cgroup v2 root."""
    cores = None
    memory = None
    cpu_max = base / "cpu.max"
    if cpu_max.exists():
        parts = cpu_max.read_text().split()
        if parts and parts[0] != "max":
            quota, period = int(parts[0]), int(parts[1])
            cores = max(1, quota // period)
    mem_max = base / "memory.max"
    if mem_max.exists():
        raw = mem_max.read_text().strip()
        if raw != "max":
            memory = int(raw)
    return cores, memory


def probe_host(cgroup_root: Path = Path("/sys/fs/cgroup")) -> tuple[int, int]:
    """(cores, memory_mb) honoring cgroup limits when present."""
    cores = os.cpu_count() or 1
    memory_mb = 0
    try:
        pages = os.sysconf("SC_PHYS_PAGES")
        page_size = os.sysconf("SC_PAGE_SIZE")
        memory_mb = int(pages * page_size / (1024 * 1024))
    except (ValueError, OSError, AttributeError):
        memory_mb = 4096
    try:
        cg_cores, cg_mem = _read_cgroup_v2(cgroup_root)
        if cg_cores:
            cores = min(cores, cg_cores)
        if cg_mem:
            memory_mb = min(memory_mb, cg_mem // (1024 * 1024))
    except OSError:
        pass
    return cores, memory_mb


def compute_profile(
    cores: int,
    memory_mb: int,
    cpus_override: str | None = None,
    memory_override: str | None = None,
    worker_pool_override: str | None = None,
) -> SizingProfile:
    if cores < 2 or memory_mb < 4096:
        tier = "small"
        warnings = [
            f"undersized host ({cores} cores / {memory_mb} MB): applying small tier with reduced limits"
        ]
    elif cores < 4 or memory_mb < 8192:
        tier = "small"
        warnings = []
    elif cores <= 16 and memory_mb <= 32768:
        tier = "standard"
        warnings = []
    else:
        tier = "large"
        warnings = []

    rss = {"small": 256, "standard": 512, "large": 1024}[tier]
    reserve = 2 if cores > 8 else 1
    cpus = max(1, cores - reserve)
    memory = int(memory_mb * 0.6)
    pool = max(2, min(cores, memory_mb // (2 * rss)))

    overrides: dict[str, str] = {}
    if cpus_override:
        cpus = float(cpus_override)
        overrides["PROTOX_CPUS"] = cpus_override
    if memory_override:
        memory = _parse_memory_mb(memory_override)
        overrides["PROTOX_MEMORY"] = memory_override
    if worker_pool_override:
        pool = int(worker_pool_override)
        overrides["PROTOX_WORKER_POOL"] = worker_pool_override

    return SizingProfile(
        tier=tier,
        cpus=cpus,
        memory_mb=memory,
        worker_pool=pool,
        per_call_rss_mb=rss,
        warnings=warnings,
        overrides=overrides,
    )


def _parse_memory_mb(raw: str) -> int:
    match = re.fullmatch(r"(\d+)\s*([mMgG]?)[bB]?", raw.strip())
    if not match:
        raise ValueError(f"invalid memory value: {raw!r} (use e.g. 4096, 4096m, 4g)")
    value = int(match.group(1))
    return value * 1024 if match.group(2).lower() == "g" else value


def profile_for_settings(settings) -> SizingProfile:
    cores, memory_mb = probe_host()
    return compute_profile(
        cores,
        memory_mb,
        cpus_override=settings.cpus_override,
        memory_override=settings.memory_override,
        worker_pool_override=settings.worker_pool_override,
    )
