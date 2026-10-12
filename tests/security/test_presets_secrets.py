"""AC-P4-04/05, AC-F032/F-033: presets, secrets boundary."""

from __future__ import annotations

import pytest

from adapters.base import SSRF_BLOCKED, ToolError
from core.secrets import SecretsBroker, contains_secret, no_secret_columns_guard
from server.config import ConfigError, Settings


def test_invalid_preset_fails_startup(tmp_path, monkeypatch):
    monkeypatch.setenv("PROTOX_POLICY_PRESET", "broken")
    import server.config as cfg

    (tmp_path / "broken.yaml").write_text("network: [unclosed")
    monkeypatch.setattr(cfg, "_POLICIES_DIR", tmp_path)
    with pytest.raises(ConfigError, match="broken.yaml"):
        Settings()


def test_unknown_preset_key_fails_startup(tmp_path, monkeypatch):
    monkeypatch.setenv("PROTOX_POLICY_PRESET", "weird")
    import server.config as cfg

    (tmp_path / "weird.yaml").write_text("teleport: yes")
    monkeypatch.setattr(cfg, "_POLICIES_DIR", tmp_path)
    with pytest.raises(ConfigError, match="unknown key 'teleport'"):
        Settings()


def test_strict_preset_disables_http_adapter(tmp_env, monkeypatch):
    monkeypatch.setenv("PROTOX_POLICY_PRESET", "strict")
    from server.app import AppContext

    ctx = AppContext(Settings())
    names = [e.name for e in ctx.registry.list_tools()]
    assert not any(n.startswith("http_") for n in names), names
    assert "image_info" in names
    ctx.store.close()


def test_network_egress_allowlist_enforced(tmp_env, monkeypatch):
    """AC-F032-03: allowlisted config reaches the worker; other hosts block."""
    monkeypatch.setenv("PROTOX_ALLOWED_HOSTS", "example.com")
    from server.app import AppContext

    ctx = AppContext(Settings())
    with pytest.raises(ToolError) as exc:
        ctx.dispatch("http_get", {"url": "http://1.1.1.1/"})
    assert exc.value.code == SSRF_BLOCKED
    ctx.store.close()


def test_worker_env_contains_no_secrets(tmp_env, monkeypatch):
    """AC-P4-05 / AC-F033-01: dump worker env, assert zero configured secrets."""
    monkeypatch.setenv("PROTOX_SECRETS", '{"MY_API_KEY": "s3cr3t-value-123"}')
    from server.app import AppContext

    ctx = AppContext(Settings())
    env = ctx.runner.worker_env({"ALLOWED_HOSTS": "example.com"})
    assert ctx.secrets.env_leak_check(env) == []
    assert not contains_secret(str(env), ctx.settings.secrets)
    ctx.store.close()


def test_secret_injection_and_no_leak(tmp_env, monkeypatch):
    """AC-F033-02: declared param injected per call; value in no log/audit."""
    monkeypatch.setenv("PROTOX_SECRETS", '{"MY_API_KEY": "s3cr3t-value-123"}')
    from adapters.base import ToolSpec
    from pydantic import BaseModel, ConfigDict

    from server.app import AppContext

    ctx = AppContext(Settings())

    class In(BaseModel):
        model_config = ConfigDict(extra="forbid")
        api_key: str | None = None

    spec = ToolSpec("t", "t", In, lambda ws, api_key=None: {}, secret_params={"api_key": "MY_API_KEY"})
    merged = ctx.secrets.inject(spec, {})
    assert merged["api_key"] == "s3cr3t-value-123"
    # the secret appears in no audit event and no log line
    audit_text = str(ctx.store.audit.query())
    log_text = ctx.settings.log_jsonl.read_text() if ctx.settings.log_jsonl.exists() else ""
    assert not contains_secret(audit_text, ctx.settings.secrets)
    assert not contains_secret(log_text, ctx.settings.secrets)
    ctx.store.close()


def test_no_secret_columns_in_schema():
    """INV-7: the DB schema has no secret columns (CI guard)."""
    from pathlib import Path

    sql = (Path(__file__).parents[2] / "migrations" / "m001_initial.sql").read_text()
    import re

    columns = re.findall(r"^\s{2}(\w+)\s+TEXT|^\s{2}(\w+)\s+INTEGER|^\s{2}(\w+)\s+REAL", sql, re.M)
    flat = [c for group in columns for c in group if c]
    assert no_secret_columns_guard(flat) == []
