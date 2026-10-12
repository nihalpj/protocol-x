#!/usr/bin/env bash
# compute-env.sh — F-038: probe the host and emit compose sizing values.
# Usage: deploy/docker/compute-env.sh > .env && docker compose up -d
set -euo pipefail
cd "$(dirname "$0")/../.."

uv run python - <<'PY'
import json
from core.sizing import probe_host, compute_profile

cores, memory_mb = probe_host()
profile = compute_profile(cores, memory_mb)
for warning in profile.warnings:
    print(f"# WARNING: {warning}")
print(f"# tier={profile.tier} worker_pool={profile.worker_pool} per_call_rss_mb={profile.per_call_rss_mb}")
print(f"PROTOX_CPUS_LIMIT={profile.cpus:g}")
print(f"PROTOX_MEM_LIMIT={profile.memory_mb}m")
PY
