"""F-016 payload capture: log_payloads toggle, scrub + truncate, dispatch wiring.

Covers the JsonlLogger request/response fields (never-raise serialization),
the PROTOX_LOG_PAYLOADS / logging.log_payloads config resolution, and the
AppContext.dispatch hot-applied toggle (off = byte-identical lines; error
path logs request but never response).
"""

from __future__ import annotations

import json

import pytest

from adapters.base import ToolError
from core.logging import PAYLOAD_MAX_CHARS

SECRET = "sk-supersecret-token-value-1234"


def _read_lines(settings) -> list[dict]:
    path = settings.log_jsonl
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().strip().splitlines() if line.strip()]


# ------------------------------------------------------------- log_call


def test_log_call_without_payloads_has_no_request_response_keys(ctx):
    line = ctx.logger.log_call("c1", "some_tool", 1.0, "ok", result_size=3)
    assert "request" not in line and "response" not in line


def test_log_call_payloads_scrubbed_and_truncated(ctx):
    ctx.secrets.secrets["llm_key"] = SECRET
    line = ctx.logger.log_call(
        "c2", "some_tool", 1.0, "ok", request={"key": SECRET}, response={"blob": "x" * 6000}
    )
    assert '***REDACTED***' in line["request"]
    assert SECRET not in line["request"]
    assert len(line["response"]) == PAYLOAD_MAX_CHARS  # repair_agent-style hard slice
    assert SECRET not in line["response"]


def test_log_call_unserializable_payload_omits_field_and_never_raises(ctx):
    class Boom:
        def __repr__(self):
            raise RuntimeError("boom")

        def __str__(self):
            raise RuntimeError("boom")

    line = ctx.logger.log_call("c3", "some_tool", 1.0, "ok", request={"b": Boom()}, response=1)
    assert "request" not in line
    assert line["response"] == "1"


# ------------------------------------------------------- config resolution


def test_settings_log_payloads_env_and_file_precedence(monkeypatch, tmp_path):
    monkeypatch.setenv("PROTOX_DATA_DIR", str(tmp_path))
    from server.config import Settings

    monkeypatch.delenv("PROTOX_LOG_PAYLOADS", raising=False)
    assert Settings().log_payloads is False  # default off

    (tmp_path / "server.yaml").write_text("logging:\n  log_payloads: true\n")
    assert Settings().log_payloads is True  # file value

    monkeypatch.setenv("PROTOX_LOG_PAYLOADS", "0")
    assert Settings().log_payloads is False  # env wins on restart


@pytest.mark.parametrize("raw", ["1", "yes", "TRUE", "On", "0", "no", "false", "OFF"])
def test_settings_log_payloads_env_truthy_parsing(monkeypatch, tmp_path, raw):
    monkeypatch.setenv("PROTOX_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("PROTOX_LOG_PAYLOADS", raw)
    from server.config import Settings

    assert Settings().log_payloads is (raw.lower() in ("1", "true", "yes", "on"))


def test_settings_log_payloads_invalid_fails_loudly(monkeypatch, tmp_path):
    monkeypatch.setenv("PROTOX_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("PROTOX_LOG_PAYLOADS", "maybe")
    from server.config import ConfigError, Settings

    with pytest.raises(ConfigError, match="PROTOX_LOG_PAYLOADS"):
        Settings()


# -------------------------------------------------------- dispatch wiring


def test_dispatch_toggle_off_logs_no_payload_keys(ctx, sample_png):
    ctx.settings.log_payloads = False
    ctx.dispatch("image_info", {"path": sample_png})
    line = _read_lines(ctx.settings)[-1]
    assert line["tool"] == "image_info" and line["outcome"] == "ok"
    assert "request" not in line and "response" not in line  # byte-identical to before


def test_dispatch_toggle_on_logs_request_and_response(ctx, sample_png):
    ctx.secrets.secrets["llm_key"] = SECRET
    ctx.settings.log_payloads = True  # hot-applied: no restart, no rebuild
    ctx.dispatch("image_info", {"path": sample_png})
    line = _read_lines(ctx.settings)[-1]
    assert line["outcome"] == "ok"
    assert sample_png in line["request"]
    assert "response" in line
    response = json.loads(line["response"])
    assert isinstance(response, dict) and "data" in response  # normalize_result shape


def test_dispatch_toggle_on_error_path_request_without_response(ctx):
    ctx.secrets.secrets["llm_key"] = SECRET
    ctx.settings.log_payloads = True
    with pytest.raises(ToolError):
        ctx.dispatch("no_such_tool", {"marker": SECRET})
    line = _read_lines(ctx.settings)[-1]
    assert line["outcome"] == "error" and line["error_code"] == "UNKNOWN_TOOL"
    assert '***REDACTED***' in line["request"] and SECRET not in line["request"]
    assert "response" not in line


def test_dispatch_toggle_off_error_path_has_no_request(ctx):
    ctx.settings.log_payloads = False
    with pytest.raises(ToolError):
        ctx.dispatch("no_such_tool", {"marker": "value"})
    line = _read_lines(ctx.settings)[-1]
    assert line["outcome"] == "error"
    assert "request" not in line and "response" not in line
