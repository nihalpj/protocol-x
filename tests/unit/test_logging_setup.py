"""Logging setup: level resolution, debug enablement, object printing."""

from __future__ import annotations

import logging

from core.logging import fmt_obj, get_logger, setup_logging


def test_default_level_is_info(tmp_path):
    setup_logging("INFO", tmp_path / "logs" / "server.log")
    assert logging.getLogger().level == logging.INFO


def test_debug_level_enables_debug_logs(tmp_path):
    setup_logging("DEBUG", tmp_path / "logs" / "server.log")
    logger = get_logger("test")
    assert logger.isEnabledFor(logging.DEBUG)
    assert logging.getLogger().level == logging.DEBUG


def test_unknown_level_falls_back_to_info(tmp_path):
    setup_logging("CHATTY", tmp_path / "logs" / "server.log")
    assert logging.getLogger().level == logging.INFO


def test_log_file_written(tmp_path):
    log_file = tmp_path / "logs" / "server.log"
    setup_logging("DEBUG", log_file)
    get_logger("test").info("hello-log-line")
    for h in logging.getLogger().handlers:
        h.flush()
    assert "hello-log-line" in log_file.read_text()


def test_get_logger_namespaces():
    assert get_logger("sandbox").name == "protox.sandbox"


def test_fmt_obj_prints_objects_and_caps_length():
    assert fmt_obj({"a": 1}) == "{'a': 1}"
    long = fmt_obj("x" * 5000, limit=100)
    assert len(long) < 120 and "chars" in long


def test_fmt_obj_never_raises_on_broken_repr():
    class Broken:
        def __repr__(self):
            raise RuntimeError("boom")

    assert "repr failed" in fmt_obj(Broken())


def test_settings_log_level_env_override(monkeypatch, tmp_path):
    monkeypatch.setenv("PROTOX_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("PROTOX_LOG_LEVEL", "debug")
    from server.config import Settings

    assert Settings().log_level == "DEBUG"


def test_settings_log_level_invalid_fails_loudly(monkeypatch, tmp_path):
    import pytest

    monkeypatch.setenv("PROTOX_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("PROTOX_LOG_LEVEL", "chatty")
    from server.config import ConfigError, Settings

    with pytest.raises(ConfigError, match="log level"):
        Settings()
