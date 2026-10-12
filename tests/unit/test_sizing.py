"""AC-P4-02/03: sizing formula fixtures, undersized hosts, env overrides."""

from __future__ import annotations

import pytest

from core.sizing import _parse_memory_mb, compute_profile


def test_small_host_fixture():
    p = compute_profile(2, 4096)
    assert p.tier == "small"
    assert p.per_call_rss_mb == 256
    assert p.cpus == 1  # max(1, 2-1)
    assert p.memory_mb == int(4096 * 0.6)
    assert p.worker_pool == max(2, min(2, 4096 // 512))
    assert not p.warnings  # 2c/4GB is small but not undersized


def test_standard_host_fixture():
    p = compute_profile(8, 32768)
    assert p.tier == "standard"
    assert p.per_call_rss_mb == 512
    assert p.cpus == 7  # 8-1
    assert p.memory_mb == int(32768 * 0.6)
    assert p.worker_pool == min(8, 32768 // 1024)


def test_large_host_fixture():
    p = compute_profile(32, 131072)
    assert p.tier == "large"
    assert p.per_call_rss_mb == 1024
    assert p.cpus == 30  # reserve 2 when cores > 8
    assert p.memory_mb == int(131072 * 0.6)
    assert p.worker_pool == min(32, 131072 // 2048)


def test_undersized_host_warns_and_floors():
    p = compute_profile(1, 2048)
    assert p.tier == "small"
    assert p.warnings, "undersized host must warn explicitly (AC-F038-02)"
    assert p.worker_pool >= 2  # floor of 2 (OQ-5)


def test_env_overrides_win():
    p = compute_profile(8, 32768, cpus_override="4", memory_override="8g", worker_pool_override="6")
    assert p.cpus == 4
    assert p.memory_mb == 8192
    assert p.worker_pool == 6
    assert p.overrides == {"PROTOX_CPUS": "4", "PROTOX_MEMORY": "8g", "PROTOX_WORKER_POOL": "6"}


def test_parse_memory():
    assert _parse_memory_mb("4096") == 4096
    assert _parse_memory_mb("512m") == 512
    assert _parse_memory_mb("8G") == 8192
    with pytest.raises(ValueError):
        _parse_memory_mb("lots")


def test_profile_audit_logged(ctx):
    events = ctx.store.audit.query(kind="config")
    startup = [e for e in events if e["payload"].get("event") == "startup"]
    assert startup, "startup sizing profile must be audit-logged (AC-P4-02)"
    sizing = startup[0]["payload"]["sizing"]
    assert sizing["tier"] in ("small", "standard", "large")
    assert sizing["worker_pool"] >= 2
